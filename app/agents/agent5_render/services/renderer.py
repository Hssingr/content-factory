"""Remotion renderer — calls the Remotion CLI via subprocess to render video files.

Each render is saved to:
  {media_path}/video/{content_id}/{language}_main.mp4
  {media_path}/video/{content_id}/{language}_short_{n}.mp4

After a successful render the function returns a dict with file_path, duration_seconds,
and render_time_seconds for the caller (orchestrator) to persist to DB.

Remotion is invoked with:
  npx remotion render <composition> <output_path> --props <props_json>

Crash recovery strategy
-----------------------
1. Normal render   — uses settings.render_concurrency (default 4).
2. Safe retry      — triggered on "Page crashed!"; uses concurrency=1 plus defensive
                     Chromium flags.  Logged as REMOTION_SAFE_RETRY.
3. Binary-search debug — if safe retry also crashes; renders section subsets to isolate
                     the bad section.  Logs REMOTION_DEBUG_SECTION.  Always re-raises.

The function raises ``RemotionCrashError`` (Page crashed) or
``RemotionRenderError`` (any other non-zero exit) so the caller can log a
distinct REMOTION_FAILED status.
"""

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_COMP_MAIN  = "MainVideo"
_COMP_SHORT = "Short"

# Chromium flags for the safe-retry pass
_SAFE_CHROME_FLAGS = (
    "--disable-dev-shm-usage "
    "--disable-gpu "
    "--no-sandbox "
    "--disable-software-rasterizer"
)

# Explicit final-encode quality target (roadmap Tier 2 R5/R7, deep-audit Q1/Q3).
# Matches @remotion/renderer's own current default (h264 CRF 18) — pinning it
# explicitly makes the target a deliberate, version-stable choice instead of
# an implicit library default that could silently change on a dependency
# bump, and gives the chunk-concat re-encode fallback (which has no Remotion
# default to inherit) the same quality target every individually-rendered
# chunk was already produced at.
_TARGET_CRF = 18

# Subprocess wall-clock timeouts (roadmap Tier 2 R6, deep-audit Finding 2) —
# nothing above this layer recovers a genuine hang: Celery sets no
# task_time_limit, and retry logic only fires from a raised exception, which
# a hung subprocess never raises. `_run_remotion`'s own 600s sits comfortably
# above the `--timeout 300000` (300s) Remotion is itself already told to
# target internally — that flag bounds Remotion's per-frame/per-asset wait,
# not the wall-clock ceiling on the whole CLI process.
_REMOTION_TIMEOUT_SEC       = 600
_BUNDLE_TIMEOUT_SEC         = 300
_AUDIO_SLICE_TIMEOUT_SEC    = 120
_CONCAT_TIMEOUT_SEC         = 600

# Beat-density concurrency safeguard (roadmap Tier 2 R10, deep-audit
# Finding 5) — documented in app/config.py's render_concurrency comment but
# previously unimplemented anywhere. Chunking-by-duration bounds how long a
# single Remotion composition plays for, not how many simultaneously-decoded
# images it may ask Chromium to hold in memory at once; a beat-dense
# render/chunk can still pressure Chromium the way a concurrency reduction
# would relieve.
_HIGH_SECTION_COUNT_THRESHOLD = 40


def _capped_concurrency(base_concurrency: int, n_sections: int) -> int:
    """Halve concurrency (floor 1) for renders/chunks above the section-count threshold."""
    if n_sections > _HIGH_SECTION_COUNT_THRESHOLD:
        return max(1, base_concurrency // 2)
    return base_concurrency


def _count_props_sections(props_path: str) -> int:
    """Return len(props["sections"]) for concurrency capping; 0 on any read failure."""
    try:
        with open(props_path, encoding="utf-8") as fh:
            return len(json.load(fh).get("sections", []))
    except Exception as exc:
        logger.warning("Could not read section count from %s for concurrency capping: %s", props_path, exc)
        return 0


# ── Pre-bundling ─────────────────────────────────────────────────────────────
# Enabled via REMOTION_PRE_BUNDLE=true in .env.
# Bundle is stored under remotion/bundles/{hash}/ and reused across renders.
# Max 2 bundles kept on disk (oldest pruned).
_MAX_BUNDLES_KEPT = 2


def ensure_bundle() -> str | None:
    """Bundle the Remotion project once and return the bundle directory path.

    Computes a SHA-256 of the ``src/`` tree and ``package.json``/``package-lock.json``
    to detect source changes.  If a bundle for the current hash already exists under
    ``remotion/bundles/{hash}/``, it is returned immediately.  Otherwise ``npx remotion
    bundle`` is run once, old bundles are pruned to keep the last
    ``_MAX_BUNDLES_KEPT``, and the new bundle path is returned.

    Called at the start of any render when ``settings.remotion_pre_bundle`` is True.
    Returns ``None`` on failure (caller falls back to src/index.ts direct mode).

    Concurrency (roadmap Tier 2 R9, deep-audit Finding 4): ``remotion bundle``
    ignores ``--out`` and always writes to the single shared ``remotion/build/``
    directory. Two workers hitting a cold cache for the same source hash at
    once — the normal situation right after a code deploy — would otherwise
    both bundle into, and then both try to move, that same path. The
    check-build-move sequence below is serialized with an advisory file lock
    per tree hash so concurrent callers queue instead of racing; the bundle
    existence check is repeated once the lock is held, since another worker
    may have already finished bundling this exact hash while this call was
    waiting to acquire it.

    Returns:
        Absolute path to the bundle directory, or None if bundling failed or disabled.
    """
    if not settings.remotion_pre_bundle:
        return None

    remotion_dir = Path(settings.remotion_path).resolve()
    src_dir      = remotion_dir / "src"
    bundles_dir  = remotion_dir / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    # ── Hash the Remotion source tree ─────────────────────────────────────────
    h = hashlib.sha256()
    for extra in ("package.json", "package-lock.json"):
        p = remotion_dir / extra
        if p.exists():
            h.update(p.read_bytes())
    if src_dir.exists():
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                h.update(path.read_bytes())
    tree_hash = h.hexdigest()[:16]

    bundle_dir = bundles_dir / tree_hash
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        logger.info("Remotion [BUNDLE_HIT] hash=%s bundle=%s", tree_hash, bundle_dir)
        return str(bundle_dir)

    lock_path = bundles_dir / f"{tree_hash}.lock"
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            # Re-check inside the lock: another worker may have finished
            # bundling this exact hash while we were waiting.
            if bundle_dir.exists() and any(bundle_dir.iterdir()):
                logger.info(
                    "Remotion [BUNDLE_HIT] hash=%s bundle=%s (built while waiting for lock)",
                    tree_hash, bundle_dir,
                )
                return str(bundle_dir)
            return _build_and_move_bundle(remotion_dir, bundles_dir, bundle_dir, tree_hash)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _build_and_move_bundle(
    remotion_dir: Path, bundles_dir: Path, bundle_dir: Path, tree_hash: str,
) -> str | None:
    """Run ``npx remotion bundle`` and move its output into ``bundle_dir``.

    Must be called with the per-hash bundle lock already held — see
    ``ensure_bundle()``.
    """
    # NOTE: `remotion bundle` ignores --out and always writes to remotion/build/.
    # We run without --out, then move the build/ output to bundles/{hash}/.
    logger.info("Remotion [BUNDLE_MISS] hash=%s — bundling now", tree_hash)
    build_dir = remotion_dir / "build"

    remotion_bin = str(remotion_dir / "node_modules" / ".bin" / "remotion")
    cmd = [
        settings.node_bin, remotion_bin,
        "bundle", "src/index.ts",
    ]
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd, cwd=str(remotion_dir),
            capture_output=True, text=True, check=False,
            timeout=_BUNDLE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logger.error("Remotion bundle timed out after %ds", _BUNDLE_TIMEOUT_SEC)
        return None
    except Exception as exc:
        logger.error("Remotion bundle failed: %s", exc)
        return None

    if result.returncode != 0:
        logger.error(
            "Remotion [BUNDLE_FAILED] exit=%d stderr=%s",
            result.returncode, result.stderr[-500:],
        )
        return None

    # Move remotion/build/ → bundles/{hash}/
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir, ignore_errors=True)
    try:
        shutil.move(str(build_dir), str(bundle_dir))
    except Exception as exc:
        logger.error("Remotion bundle move failed: %s", exc)
        return None

    elapsed = time.monotonic() - t0
    logger.info(
        "Remotion [BUNDLE_OK] hash=%s elapsed=%.1fs bundle=%s",
        tree_hash, elapsed, bundle_dir,
    )

    # ── Prune old bundles — keep last _MAX_BUNDLES_KEPT ─────────────────────
    existing = sorted(
        (d for d in bundles_dir.iterdir() if d.is_dir() and d != bundle_dir),
        key=lambda d: d.stat().st_mtime,
    )
    to_prune = existing[: max(0, len(existing) - (_MAX_BUNDLES_KEPT - 1))]
    for old_bundle in to_prune:
        logger.info("Remotion [BUNDLE_PRUNE] removing old bundle=%s", old_bundle)
        try:
            shutil.rmtree(old_bundle, ignore_errors=True)
        except Exception as exc:
            logger.warning("Bundle prune failed for %s: %s", old_bundle, exc)

    return str(bundle_dir)


# ── Exception types ───────────────────────────────────────────────────────────

class RemotionCrashError(RuntimeError):
    """Chromium crashed during render (Page crashed!)."""


class RemotionRenderError(RuntimeError):
    """Remotion CLI exited non-zero for a non-crash reason."""


# ── Public API ────────────────────────────────────────────────────────────────

def render_main_video(
    content_id: str,
    language: str,
    props_path: str,
    duration_ms: int,
    concurrency: int | None = None,
    bundle_dir: str | None = None,
) -> dict:
    """Render the main 16:9 video using Remotion with crash recovery.

    Attempts a normal render first.  If Chromium crashes ("Page crashed!"),
    retries once with safe Chromium flags and concurrency=1.  If that also
    crashes, runs a binary-search debug pass to identify the bad section and
    then re-raises ``RemotionCrashError``.

    Args:
        content_id:  UUID of the content record (as string).
        language:    Language code (e.g. "fr").
        props_path:  Absolute path to the main props JSON file.
        duration_ms: Expected video duration (recorded in DB; not passed to Remotion).
        concurrency: Chromium tab concurrency.  Defaults to settings.render_concurrency.

    Returns:
        Dict with file_path, duration_seconds, render_time_seconds.

    Raises:
        RemotionCrashError: Chromium crashed on both the normal and safe-retry pass.
        RemotionRenderError: Remotion exited non-zero for any other reason.
    """
    output_path = _ensure_output_path(content_id, f"{language}_main.mp4")
    conc = concurrency if concurrency is not None else settings.render_concurrency
    conc = _capped_concurrency(conc, _count_props_sections(props_path))

    try:
        render_time = _run_remotion(_COMP_MAIN, output_path, props_path, conc, bundle_dir=bundle_dir)
    except RemotionCrashError as exc:
        logger.warning(
            "Remotion [REMOTION_SAFE_RETRY] composition=%s language=%s — "
            "Page crashed on normal render; retrying with concurrency=1 + safe flags",
            _COMP_MAIN, language,
        )
        try:
            render_time = _run_remotion(
                _COMP_MAIN, output_path, props_path,
                concurrency=1,
                chrome_flags=_SAFE_CHROME_FLAGS,
                bundle_dir=bundle_dir,
            )
            logger.info(
                "Remotion [REMOTION_SAFE_RETRY] composition=%s language=%s — "
                "safe retry SUCCEEDED",
                _COMP_MAIN, language,
            )
        except RemotionCrashError:
            logger.error(
                "Remotion [REMOTION_SAFE_RETRY] composition=%s language=%s — "
                "safe retry also crashed; running binary-search debug",
                _COMP_MAIN, language,
            )
            _debug_find_crashing_section(_COMP_MAIN, props_path, language)
            raise  # re-raise after diagnostics so caller logs REMOTION_FAILED

    logger.info("Main video rendered: %s (%.1fs)", output_path, render_time)
    return {
        "file_path":           str(output_path),
        "duration_seconds":    duration_ms / 1000,
        "render_time_seconds": render_time,
    }


def render_short(
    content_id: str,
    language: str,
    short_index: int,
    props_path: str,
    duration_ms: int,
    concurrency: int | None = None,
    bundle_dir: str | None = None,
) -> dict:
    """Render a single Short (9:16) using Remotion with crash recovery.

    Args:
        content_id:    UUID of the content record.
        language:      Language code.
        short_index:   0-based index of the Short.
        props_path:    Absolute path to this Short's props JSON file.
        duration_ms:   Duration of this Short in ms.
        concurrency:   Chromium tab concurrency.  Defaults to settings.render_concurrency.

    Returns:
        Dict with file_path, duration_seconds, render_time_seconds.

    Raises:
        RemotionCrashError: Chromium crashed on both the normal and safe-retry pass.
        RemotionRenderError: Remotion exited non-zero for any other reason.
    """
    file_name   = f"{language}_short_{short_index}.mp4"
    output_path = _ensure_output_path(content_id, file_name)
    conc = concurrency if concurrency is not None else settings.render_concurrency
    conc = _capped_concurrency(conc, _count_props_sections(props_path))

    try:
        render_time = _run_remotion(_COMP_SHORT, output_path, props_path, conc, bundle_dir=bundle_dir)
    except RemotionCrashError:
        logger.warning(
            "Remotion [REMOTION_SAFE_RETRY] composition=%s short=%d language=%s — "
            "Page crashed; retrying with concurrency=1 + safe flags",
            _COMP_SHORT, short_index, language,
        )
        render_time = _run_remotion(
            _COMP_SHORT, output_path, props_path,
            concurrency=1,
            chrome_flags=_SAFE_CHROME_FLAGS,
            bundle_dir=bundle_dir,
        )
        logger.info(
            "Remotion [REMOTION_SAFE_RETRY] short=%d language=%s — safe retry SUCCEEDED",
            short_index, language,
        )

    logger.info("Short %d rendered: %s (%.1fs)", short_index, output_path, render_time)
    return {
        "file_path":           str(output_path),
        "duration_seconds":    duration_ms / 1000,
        "render_time_seconds": render_time,
    }


def _compute_chunk_boundaries(
    all_sections: list[dict], duration_ms: int, chunk_sec: int,
) -> list[tuple[int, int]]:
    """Return (start_ms, end_ms) pairs for each chunk, with every internal
    boundary snapped to a real section edge so no VideoSection beat is ever
    split — and therefore duplicated — across two chunks.

    Root cause this replaces (live-canary fix): the previous fixed
    chunk_idx * chunk_sec boundary landed inside whichever beat happened to
    be playing at that exact millisecond, on every single internal boundary
    of a real production render (8/8 in a 9-chunk video). Each straddling
    beat's image was independently clipped into BOTH adjacent chunks — full
    playback at the tail of one chunk, then an immediate replay from its own
    progress=0 for a short fragment (as little as 240ms) at the head of the
    next. Because each chunk is an entirely separate Remotion composition,
    MainVideo.tsx's own idx===0 first-section case also always treated that
    fragment's arrival as having no incoming transition, discarding whatever
    crossfade/whip_pan/match_cut was actually authored there. Together these
    produced exactly the reported symptoms: missing/inconsistent transitions
    and short flash-like image fragments at every chunk seam.

    Sections are assumed contiguous (section[i].audio_end_ms ==
    section[i+1].audio_start_ms) — true by construction for every persisted
    VideoSection row (map_storyboard_beats_to_timestamps() never leaves a
    gap) — so any section's audio_end_ms is safe to use as a hard split
    point: nothing on either side of it belongs to a different beat.

    A nominal boundary with no unused section-edge candidate left near it
    (or no candidates at all) is simply dropped, producing fewer/longer
    chunks than the nominal target rather than falling back to a
    beat-splitting cut — a few seconds of drift around the configured
    chunk_duration_sec is harmless to the crash-avoidance goal chunking
    exists for.
    """
    nominal_ms = chunk_sec * 1000
    n_nominal  = max(1, -(-duration_ms // nominal_ms))
    if n_nominal <= 1:
        return [(0, duration_ms)]

    candidates = sorted({
        int(s["audio_end_ms"])
        for s in all_sections
        if 0 < s.get("audio_end_ms", 0) < duration_ms
    })
    if not candidates:
        return [(0, duration_ms)]

    boundaries: list[int] = []
    used: set[int] = set()
    for i in range(1, n_nominal):
        nominal   = i * nominal_ms
        available = [c for c in candidates if c not in used]
        if not available:
            break
        best = min(available, key=lambda c: abs(c - nominal))
        used.add(best)
        boundaries.append(best)

    edges = [0, *sorted(boundaries), duration_ms]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1) if edges[i + 1] > edges[i]]


def render_main_video_chunked(
    content_id: str,
    language: str,
    props_path: str,
    duration_ms: int,
    audio_file_path: str,
    concurrency: int | None = None,
    bundle_dir: str | None = None,
) -> dict:
    """Render the main video in time-based chunks and concatenate with ffmpeg.

    Splits the MainVideo render into segments of ``settings.chunk_duration_sec``
    seconds, renders each chunk independently, then concatenates the output MP4s
    using the ffmpeg concat demuxer (stream-copy, no re-encode).

    This eliminates "Page crashed!" failures caused by Chromium holding many
    remote video streams simultaneously in a single long render.

    Args:
        content_id:       UUID of the content record (as string).
        language:         Language code (e.g. "fr").
        props_path:       Absolute path to the main props JSON file.
        duration_ms:      Total video duration in milliseconds.
        audio_file_path:  Absolute path to the original audio file (for slicing).
        concurrency:      Chromium tab concurrency per chunk.

    Returns:
        Dict with file_path, duration_seconds, render_time_seconds.

    Raises:
        RemotionCrashError: A chunk crashed on both the normal and safe-retry pass.
        RemotionRenderError: Any chunk or the ffmpeg concat step failed non-zero.
    """
    chunk_sec       = settings.chunk_duration_sec
    n_chunks_nominal = max(1, -(-duration_ms // (chunk_sec * 1000)))   # ceiling division
    conc            = concurrency if concurrency is not None else settings.render_concurrency

    if n_chunks_nominal == 1:
        return render_main_video(content_id, language, props_path, duration_ms, concurrency)

    with open(props_path, encoding="utf-8") as fh:
        full_props = json.load(fh)

    all_sections  = sorted(full_props.get("sections", []), key=lambda s: s.get("audio_start_ms", 0))
    all_captions  = (full_props.get("subtitles") or {}).get("captions", [])
    media_root    = Path(settings.media_path).resolve()
    audio_ext     = Path(audio_file_path).suffix or ".mp3"

    chunk_boundaries = _compute_chunk_boundaries(all_sections, duration_ms, chunk_sec)
    n_chunks = len(chunk_boundaries)

    if n_chunks == 1:
        return render_main_video(content_id, language, props_path, duration_ms, concurrency)

    logger.debug(
        "Chunked render enabled: duration=%ds chunks=%d chunk_duration=%ds",
        duration_ms // 1000, n_chunks, chunk_sec,
    )

    # transition_to_next of whichever section ends exactly at a given chunk
    # boundary — every internal boundary in chunk_boundaries is, by
    # construction, some section's real audio_end_ms.
    end_to_transition = {
        s["audio_end_ms"]: s.get("transition_to_next")
        for s in all_sections
        if "audio_end_ms" in s
    }

    chunk_dir = media_root / "video" / content_id / "chunks" / language
    # Recreate (not just mkdir) so a re-render whose new chunk count differs
    # from a prior run never leaves that prior run's now-unused chunk files
    # behind (roadmap Tier 2 R8, deep-audit Finding 3) — _concatenate_chunks()
    # below only ever reads this run's own final_chunk_paths, so an orphaned
    # e.g. chunk_008.mp4 from a prior 9-chunk run would otherwise silently
    # persist even after this run produces only 8 chunks.
    shutil.rmtree(chunk_dir, ignore_errors=True)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = chunk_dir / "audio"
    audio_dir.mkdir(exist_ok=True)

    t0          = time.monotonic()
    chunk_paths: list[str | None] = [None] * n_chunks   # indexed by chunk_idx

    # ── Prepare all chunk data (audio slice + props) sequentially ─────────────
    chunk_specs: list[dict] = []
    for chunk_idx, (chunk_start_ms, chunk_end_ms) in enumerate(chunk_boundaries):
        chunk_dur_ms    = chunk_end_ms - chunk_start_ms
        chunk_start_sec = chunk_start_ms / 1000
        chunk_dur_sec   = chunk_dur_ms / 1000

        # ── Slice audio ───────────────────────────────────────────────────────
        chunk_audio_abs = str(audio_dir / f"chunk_{chunk_idx:03d}{audio_ext}")
        if not _slice_audio_for_chunk(audio_file_path, chunk_start_sec, chunk_dur_sec, chunk_audio_abs):
            raise RemotionRenderError(
                f"ffmpeg audio slice failed for chunk {chunk_idx} "
                f"(offset={chunk_start_sec:.1f}s dur={chunk_dur_sec:.1f}s)"
            )
        chunk_audio_rel = str(Path(chunk_audio_abs).relative_to(media_root))

        # ── Filter and re-offset sections ───────────────────────────────────────
        # Exclusive, start-based membership (live-canary fix): chunk_boundaries
        # snaps every internal cut to a real section edge, so a section's own
        # start now falls in exactly one [chunk_start_ms, chunk_end_ms) window —
        # the old overlap test (s_start < chunk_end and s_end > chunk_start)
        # could and did match the same straddling section in two consecutive
        # chunks, each independently (and differently) clipping and replaying it.
        chunk_sections: list[dict] = []
        for sec in all_sections:
            s_start = sec.get("audio_start_ms", 0)
            if chunk_start_ms <= s_start < chunk_end_ms:
                s = dict(sec)
                s["audio_start_ms"] = s_start - chunk_start_ms
                s["audio_end_ms"]   = sec.get("audio_end_ms", 0) - chunk_start_ms
                chunk_sections.append(s)

        if not chunk_sections:
            logger.warning(
                "Chunk %d/%d: no sections in [%d, %d)ms — skipping",
                chunk_idx + 1, n_chunks, chunk_start_ms, chunk_end_ms,
            )
            continue

        # ── Re-offset subtitle captions ───────────────────────────────────────
        chunk_captions = [
            {
                **c,
                "start_ms": c["start_ms"] - chunk_start_ms,
                "end_ms":   c["end_ms"]   - chunk_start_ms,
            }
            for c in all_captions
            if c.get("start_ms", 0) >= chunk_start_ms and c.get("end_ms", 0) <= chunk_end_ms
        ]

        # ── Leading transition ──────────────────────────────────────────────────
        # Each chunk is rendered as its own independent Remotion composition, so
        # MainVideo.tsx's own idx===0 case has no prior local section to read a
        # transition_to_next off of. Without this, the transition actually
        # authored into this chunk's first beat (crossfade/whip_pan/match_cut)
        # was silently discarded as a hard cut on every chunk boundary.
        leading_transition = end_to_transition.get(chunk_start_ms) if chunk_idx > 0 else None

        # ── Write chunk props ─────────────────────────────────────────────────
        chunk_props = {
            **full_props,
            "audio_file":          chunk_audio_rel,
            "duration_ms":         chunk_dur_ms,
            "sections":            chunk_sections,
            "subtitles":           {"style": "standard", "captions": chunk_captions},
            "leading_transition":  leading_transition,
        }
        chunk_props_path = str(chunk_dir / f"chunk_{chunk_idx:03d}.json")
        with open(chunk_props_path, "w", encoding="utf-8") as fh:
            json.dump(chunk_props, fh, ensure_ascii=False, indent=2)

        chunk_output = str(chunk_dir / f"chunk_{chunk_idx:03d}.mp4")
        chunk_specs.append({
            "chunk_idx":       chunk_idx,
            "chunk_output":    chunk_output,
            "chunk_props_path": chunk_props_path,
            "chunk_dur_sec":   chunk_dur_sec,
            "chunk_start_sec": chunk_start_sec,
            "n_sections":      len(chunk_sections),
        })

    # ── Render chunks — sequentially or in parallel ───────────────────────────
    parallel_workers = settings.chunk_parallel_workers
    logger.debug(
        "Chunked render: %d chunks prepared, parallel_workers=%d",
        len(chunk_specs), parallel_workers,
    )

    def _render_chunk(spec: dict) -> str:
        """Render a single prepared chunk (called in worker thread or directly)."""
        cidx          = spec["chunk_idx"]
        chunk_out_p   = Path(spec["chunk_output"])
        cprops_path   = spec["chunk_props_path"]
        cdur_sec      = spec["chunk_dur_sec"]
        cstart_sec    = spec["chunk_start_sec"]
        n_secs        = spec["n_sections"]
        # Beat-density cap (roadmap Tier 2 R10, deep-audit Finding 5): capped
        # per CHUNK, not per whole render — chunking-by-duration already
        # bounds how long a single composition plays for, but a chunk with
        # many short beats can still hold far more simultaneously-decoded
        # images in memory than a chunk of the same duration with sparse,
        # long-holding beats. See app/config.py's render_concurrency comment.
        chunk_conc    = _capped_concurrency(conc, n_secs)

        logger.debug(
            "Rendering chunk %d/%d: sections=%d dur=%.1fs offset=%.1fs concurrency=%d",
            cidx + 1, n_chunks, n_secs, cdur_sec, cstart_sec, chunk_conc,
        )
        try:
            _run_remotion(_COMP_MAIN, chunk_out_p, cprops_path, chunk_conc,
                          bundle_dir=bundle_dir)
        except RemotionCrashError:
            logger.warning("Chunk %d crashed — retrying with concurrency=1 + safe flags", cidx + 1)
            _run_remotion(
                _COMP_MAIN, chunk_out_p, cprops_path,
                concurrency=1, chrome_flags=_SAFE_CHROME_FLAGS, bundle_dir=bundle_dir,
            )
        return spec["chunk_output"]

    if parallel_workers > 1 and len(chunk_specs) > 1:
        # Parallel mode: submit all chunks to a thread pool, collect in order
        futures: dict = {}
        with ThreadPoolExecutor(max_workers=parallel_workers) as pool:
            for spec in chunk_specs:
                futures[pool.submit(_render_chunk, spec)] = spec["chunk_idx"]
        # Collect results preserving order; re-raise first exception encountered
        results_by_idx: dict[int, str] = {}
        for fut in as_completed(futures):
            cidx = futures[fut]
            results_by_idx[cidx] = fut.result()  # propagates exceptions
        for spec in chunk_specs:
            chunk_paths[spec["chunk_idx"]] = results_by_idx[spec["chunk_idx"]]
    else:
        for spec in chunk_specs:
            out = _render_chunk(spec)
            chunk_paths[spec["chunk_idx"]] = out

    final_chunk_paths = [p for p in chunk_paths if p is not None]
    if not final_chunk_paths:
        raise RemotionRenderError(f"Chunked render: no chunks produced for language={language}")

    output_path = _ensure_output_path(content_id, f"{language}_main.mp4")
    logger.debug("Concatenating %d chunks → %s", len(final_chunk_paths), output_path)
    _concatenate_chunks(final_chunk_paths, str(output_path))

    # Clean up intermediate chunk artifacts now that concat succeeded
    # (roadmap Tier 2 R8, deep-audit Finding 3) — confirmed on real disk to
    # otherwise persist forever, roughly doubling a chunked render's on-disk
    # footprint. Deliberately success-path-only: an exception raised above
    # (a chunk render or the concat step itself failing) leaves chunk_dir in
    # place so per-chunk MP4s/props remain available for manual post-mortem
    # inspection.
    shutil.rmtree(chunk_dir, ignore_errors=True)

    total_time = time.monotonic() - t0
    logger.debug(
        "Chunked render complete: language=%s chunks=%d total_time=%.1fs",
        language, len(chunk_paths), total_time,
    )
    return {
        "file_path":           str(output_path),
        "duration_seconds":    duration_ms / 1000,
        "render_time_seconds": total_time,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ensure_output_path(content_id: str, file_name: str) -> Path:
    output_dir = Path(settings.media_path).resolve() / "video" / content_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / file_name


def _slice_audio_for_chunk(
    audio_path: str,
    start_sec: float,
    duration_sec: float,
    output_path: str,
) -> bool:
    """Cut a time slice of an audio file using ffmpeg -ss/-t stream copy.

    Args:
        audio_path:   Absolute path to the source audio file.
        start_sec:    Start offset in seconds.
        duration_sec: Duration of the slice in seconds.
        output_path:  Absolute path for the output file.

    Returns:
        True on success, False on failure.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-t",  f"{duration_sec:.3f}",
        "-i",  audio_path,
        "-c",  "copy",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=_AUDIO_SLICE_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            logger.warning(
                "ffmpeg audio slice failed (exit %d): %s",
                result.returncode, result.stderr[-200:],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg audio slice timed out after %ds", _AUDIO_SLICE_TIMEOUT_SEC)
        return False
    except Exception as exc:
        logger.warning("ffmpeg audio slice exception: %s", exc)
        return False


def _concatenate_chunks(chunk_paths: list[str], output_path: str) -> None:
    """Concatenate rendered chunk MP4s with ffmpeg concat demuxer (stream copy).

    Falls back to a libx264/aac re-encode if stream copy fails (e.g. mismatched
    codec parameters between chunks).

    Args:
        chunk_paths: Ordered list of absolute MP4 paths to concatenate.
        output_path: Absolute path for the final combined MP4.

    Raises:
        RemotionRenderError: If both stream-copy and re-encode concat fail.
    """
    list_file = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="concat_list_"
        ) as fh:
            for p in chunk_paths:
                fh.write(f"file '{p}'\n")
            list_file = fh.name

        def _run_concat(extra_codec_flags: list[str]) -> bool:
            cmd = [
                "ffmpeg", "-y",
                "-f",    "concat",
                "-safe", "0",
                "-i",    list_file,
                *extra_codec_flags,
                output_path,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=False,
                    timeout=_CONCAT_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg concat timed out after %ds", _CONCAT_TIMEOUT_SEC)
                return False
            if result.returncode != 0:
                logger.warning(
                    "ffmpeg concat failed (exit %d): %s",
                    result.returncode, result.stderr[-300:],
                )
                return False
            return True

        if _run_concat(["-c", "copy"]):
            return
        logger.warning("Stream-copy concat failed — retrying with libx264/aac re-encode")
        # Explicit -crf (roadmap Tier 2 R5, deep-audit Q1): every chunk was
        # already rendered by Remotion at CRF _TARGET_CRF. Without an explicit
        # value here, ffmpeg's own libx264 default is CRF 23 — a real, silent
        # quality regression on the one recovery path meant to keep a long
        # parent render resilient, with no log signal that the fallback
        # produced a softer result than normal.
        if not _run_concat([
            "-c:v", "libx264", "-crf", str(_TARGET_CRF), "-c:a", "aac", "-preset", "fast",
        ]):
            raise RemotionRenderError(
                f"ffmpeg concat failed for {len(chunk_paths)} chunks → {output_path}"
            )
    finally:
        if list_file:
            try:
                os.unlink(list_file)
            except OSError:
                pass


def _run_remotion(
    composition: str,
    output_path: Path,
    props_path: str,
    concurrency: int = 4,
    chrome_flags: str = "",
    bundle_dir: str | None = None,
) -> float:
    """Invoke the Remotion CLI and return wall-clock render time in seconds.

    Remotion 4 CLI usage (direct source):
      node remotion render src/index.ts <CompositionId> <output.mp4>
            --props <props.json> --public-dir <media_path>
            --concurrency N [--chrome-flags "..."]

    Remotion 4 CLI usage (pre-bundled, faster):
      node remotion render <bundle_dir>/index.js <CompositionId> <output.mp4>
            --props <props.json> --public-dir <media_path>
            --concurrency N [--chrome-flags "..."]

    Args:
        composition:  Remotion composition ID (e.g. "MainVideo").
        output_path:  Absolute destination MP4 path.
        props_path:   Absolute path to the props JSON file.
        concurrency:  Number of Chromium tabs (default 4).
        chrome_flags: Extra Chromium command-line flags (space-separated string).
        bundle_dir:   Path to a pre-built Remotion bundle directory.  When provided,
                      ``bundle_dir`` is used as the entry point instead of ``src/index.ts``.

    Returns:
        Render time in seconds.

    Raises:
        RemotionCrashError: stderr contains "Page crashed".
        RemotionRenderError: Any other non-zero exit code.
    """
    remotion_dir = Path(settings.remotion_path).resolve()
    remotion_bin = str(remotion_dir / "node_modules" / ".bin" / "remotion")
    media_dir    = Path(settings.media_path).resolve()

    # Use pre-built bundle directory if provided, otherwise fall back to src/index.ts.
    # remotion render accepts a directory containing index.html as the serve URL.
    # Do NOT pass index.js — Remotion ignores it and treats the path as a composition ID.
    entry_point = str(bundle_dir) if bundle_dir else "src/index.ts"

    cmd = [
        settings.node_bin,
        remotion_bin,
        "render",
        entry_point,
        composition,
        str(output_path.resolve()),
        "--props",         str(Path(props_path).resolve()),
        "--public-dir",    str(media_dir),
        "--concurrency",   str(concurrency),
        "--timeout",       "300000",
        "--log",           "error",
        # Pin final encode settings explicitly (roadmap Tier 2 R7, deep-audit
        # Q3) instead of relying on @remotion/renderer's implicit library
        # defaults. Today's defaults happen to match these exact values (h264
        # CRF 18, yuv420p, 320k AAC) — this makes the quality target a
        # deliberate, version-stable choice instead of one dependency bump
        # away from silently shifting every render's quality with no diff in
        # this repository to review. _TARGET_CRF is the same constant the
        # chunk-concat fallback (R5, _concatenate_chunks) reuses.
        "--crf",            str(_TARGET_CRF),
        "--pixel-format",   "yuv420p",
        "--audio-bitrate",  "320k",
    ]
    if chrome_flags.strip():
        cmd.extend(["--chrome-flags", chrome_flags.strip()])

    logger.info(
        "Remotion render: %s → %s (concurrency=%d safe=%s bundled=%s)",
        composition, output_path, concurrency, bool(chrome_flags), bool(bundle_dir),
    )

    # Remotion's render command re-bundles internally and copies --public-dir into
    # a temp webpack bundle under TMPDIR before starting the Chromium server.
    # /tmp is a tmpfs that fills up quickly (ENOSPC). Redirect TMPDIR to a sibling
    # of media_dir at the project root — same large partition, but outside --public-dir.
    # CRITICAL: TMPDIR must NOT be inside --public-dir (media_dir); Remotion copies
    # --public-dir into TMPDIR, which would cause infinite recursive copy if TMPDIR is
    # a subdirectory of --public-dir.
    remotion_tmp = media_dir.parent / "remotion_tmp"
    remotion_tmp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(remotion_tmp)   # Linux/macOS — Node respects this for fs.mkdtemp

    t0 = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            cwd=str(remotion_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=_REMOTION_TIMEOUT_SEC,
        )
    except FileNotFoundError as exc:
        raise RemotionRenderError(
            f"Remotion CLI not found — set NODE_BIN in .env to your Node ≥18 path. ({exc})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # No layer above this recovers a genuine hang (roadmap Tier 2 R6,
        # deep-audit Finding 2): Celery sets no task_time_limit, and retry
        # logic only fires from a raised exception, which a hung subprocess
        # never raises on its own — without this, a stuck Chromium process
        # would occupy a worker slot forever.
        raise RemotionRenderError(
            f"Remotion render timed out after {_REMOTION_TIMEOUT_SEC}s for {composition}"
        ) from exc

    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        stderr_tail = result.stderr[-3000:]
        logger.error("Remotion stderr:\n%s", stderr_tail)
        if "Page crashed" in result.stderr:
            raise RemotionCrashError(
                f"Remotion Page crashed for {composition}: {result.stderr[-500:]}"
            )
        raise RemotionRenderError(
            f"Remotion render failed (exit {result.returncode}) for {composition}: "
            f"{result.stderr[-500:]}"
        )

    return elapsed


# ── Binary-search debug mode ──────────────────────────────────────────────────

def _debug_find_crashing_section(
    composition: str,
    props_path: str,
    language: str,
) -> None:
    """Render section subsets to isolate which section/media URL causes a crash.

    Strategy: try first 5 sections → if stable, chunk forward in 5-section steps
    until the crash reproduces, then binary-search within the bad chunk.
    Logs the suspected bad section_order and media_url but never raises.

    Args:
        composition: Remotion composition ID.
        props_path:  Path to the full props JSON.
        language:    Language code (for log context).
    """
    try:
        with open(props_path, encoding="utf-8") as fh:
            props = json.load(fh)
    except Exception as exc:
        logger.error("Remotion debug: cannot read props for binary search: %s", exc)
        return

    sections = props.get("sections", [])
    n = len(sections)
    if n == 0:
        logger.error("Remotion debug: no sections in props — cannot isolate crash")
        return

    logger.info(
        "Remotion [REMOTION_DEBUG_SECTION] language=%s total_sections=%d",
        language, n,
    )

    with tempfile.TemporaryDirectory(prefix="remotion_debug_") as tmp_dir:
        debug_out = Path(tmp_dir) / "debug.mp4"

        def _try(subset: list[dict]) -> bool:
            """Render a section subset; return True if it succeeds."""
            test_props = {**props, "sections": subset}
            test_json  = Path(tmp_dir) / f"debug_{len(subset)}.json"
            test_json.write_text(json.dumps(test_props, ensure_ascii=False))
            try:
                _run_remotion(composition, debug_out, str(test_json), concurrency=1,
                              chrome_flags=_SAFE_CHROME_FLAGS)
                return True
            except (RemotionCrashError, RemotionRenderError):
                return False

        # Find the 5-section chunk that crashes
        bad_chunk_start = 0
        chunk_size = 5
        found_chunk = False
        for start in range(0, n, chunk_size):
            chunk = sections[start:start + chunk_size]
            if not _try(chunk):
                bad_chunk_start = start
                found_chunk = True
                logger.debug(
                    "Remotion [REMOTION_DEBUG_SECTION] language=%s "
                    "crashing chunk: sections[%d:%d]",
                    language, start, start + chunk_size,
                )
                break

        if not found_chunk:
            logger.debug(
                "Remotion [REMOTION_DEBUG_SECTION] language=%s "
                "no single 5-section chunk crashed — crash may be cumulative memory issue",
                language,
            )
            return

        # Binary search within the bad chunk
        lo = bad_chunk_start
        hi = min(bad_chunk_start + chunk_size, n)

        while hi - lo > 1:
            mid = (lo + hi) // 2
            # Render sections[lo:mid] within their original context prefix
            subset = sections[:mid]
            if _try(subset):
                lo = mid
            else:
                hi = mid

        suspected_idx = lo
        suspected = sections[suspected_idx] if suspected_idx < n else None

        if suspected:
            logger.error(
                "Remotion [REMOTION_DEBUG_SECTION] language=%s "
                "suspected bad section_order=%s media_url=%.120s media_type=%s",
                language,
                suspected.get("order", suspected_idx),
                suspected.get("media_url", "?"),
                suspected.get("media_type", "?"),
            )
        else:
            logger.error(
                "Remotion [REMOTION_DEBUG_SECTION] language=%s "
                "binary search inconclusive (idx=%d n=%d)",
                language, suspected_idx, n,
            )
