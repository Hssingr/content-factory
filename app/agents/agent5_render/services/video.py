"""Agent 5 — Render-only service.

Agent 5 is a pure consumer of visual-ready content. It never calls Agent 4 and
never imports any `app.agents.agent4_visuals` module. It discovers render-ready
content from `Content.status` — `PARENT_VISUALS_DONE` or
`CHILD_SHORT_VISUALS_DONE`, written exclusively by Agent 4 (see
`app.scheduler.tasks.pickup_visual_ready`). `VideoSection` row existence is
read directly from the database as a *defensive* validation check, not the
primary discovery signal — if status says ready but the rows are missing,
Agent 5 defers rather than generating them itself. It does not generate
storyboards, run Flux, perform remap, or persist `VideoSection` rows, and it
has no Agent 4 fallback. See CLAUDE.md "6A. Service Ownership Boundaries".

Render pass (once per language, using the `VideoSection` rows Agent 4 already
persisted):
  1. Load VideoSection rows — read-only; Agent 5 never writes this table.
  2. Subtitles         — standard (main) + karaoke from Whisper timestamps.
  3. Remotion builder  — write JSON props files.
  4. Remotion renderer — call Remotion CLI, save VideoRender(format="main").

Standalone shorts — standalone short episode Content rows (is_short_episode=True):
  Short-form videos are produced ONLY by child Content rows created by
  run_shorts_planner(). Each child row has its own script, audio, Whisper, and
  storyboard remap (persisted by Agent 4 as VideoSection rows before Agent 5
  ever sees this content). They render with Short.tsx at 1080×1920 (9:16) and
  store VideoRender(format="short", short_order=short_part_number-1).
  Agent 6 queries:
    Long videos: VideoRender.format=="main" WHERE content.is_short_episode==False
    Shorts:      VideoRender.format=="short" WHERE content.is_short_episode==True

Re-entrancy — each step is skipped when its output already exists:
  • Main MP4 on disk + VideoRender in DB  → language fully done, skip all
  • Props JSON on disk                    → skip subtitles/props, go directly to render
  • No VideoSection rows for a language   → defer that language (not a failure)

Status transitions (Agent 5 is the sole writer of RENDERING/RENDERED):
  (a visual-done status, written by Agent 4) → RENDERING  (set at start)
  RENDERING                                   → RENDERED   (set on full success)
  RENDERING                                   → FAILED     (set if all languages fail)
  (no status change)                           → deferred when VideoSections are missing
"""

import json
import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.services.local_run_paths import ensure_run_dirs
from app.services.video_sections import load_video_sections
from app.models import (
    AudioFile, Channel, ChannelConfig, Content, Script, VideoRender,
)
from app.agents.agent5_render.services.subtitles import (
    build_standard_subtitles, build_karaoke_subtitles,
)
from app.agents.agent5_render.services.remotion_builder import build_main_props, build_short_props
from app.agents.agent5_render.services.renderer import (
    render_main_video, render_main_video_chunked, render_short,
    ensure_bundle,
    RemotionCrashError, RemotionRenderError,
)
from app.agents.agent5_render.services.verify import verify_render

logger = logging.getLogger(__name__)


class VerifyFailedError(RuntimeError):
    """Post-render verification caught a broken render (black frames / silence / bad resolution)."""


# Legacy sentinel that pre-subtitles-only code wrote onto a beat's media_url
# when Flux generation failed. New code never produces it (text cards are
# removed — audit G-0/G-8; hard failures are filled by neighbour reuse in
# Agent 4). Kept only to recognize and neutralize legacy persisted rows.
_TEXT_CARD_SENTINEL = "__text_card__"

# Technical-blocker threshold: >50% of beats with no real media → block render.
_MISSING_MEDIA_BLOCK_RATIO = 0.50


def run_video_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the Agent 5 render-only pipeline for one piece of content.

    Requires that Agent 4 has already persisted `AudioFile` and `VideoSection`
    rows for this content. Renders each language using the persisted
    `VideoSection` rows. A single-language render failure is logged and
    skipped — the pipeline continues for remaining languages. A language with
    no persisted `VideoSection` rows yet is deferred, not failed.

    Args:
        content_id: UUID of content with status ``PARENT_VISUALS_DONE``,
            ``CHILD_SHORT_VISUALS_DONE``, or ``RENDERING`` (re-entrant retry).
        db:         SQLAlchemy session managed by the caller.

    Returns:
        ``True``  — at least one language was successfully rendered.
        ``False`` — all languages failed, or visuals are not ready yet.
    """
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.error("Content %s not found", content_id)
        return False

    ensure_run_dirs(content_id)

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content_id)
        return False

    if content.status not in ("PARENT_VISUALS_DONE", "CHILD_SHORT_VISUALS_DONE", "RENDERING"):
        logger.debug(
            "Content %s status=%s — skipping video generation",
            content_id, content.status,
        )
        return False

    if content.status != "RENDERING":
        content.status = "RENDERING"
        db.commit()
        logger.info("RENDER_START content_id=%s", content_id)

    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    karaoke_color = config.subtitle_karaoke_active_color if config else "#FFD700"

    scripts_by_lang: dict[str, Script] = {
        s.language: s
        for s in db.query(Script)
        .filter(Script.content_id == content_id, Script.validated.is_(True))
        .all()
    }
    audio_by_lang: dict[str, AudioFile] = {
        a.language: a
        for a in db.query(AudioFile)
        .filter(AudioFile.content_id == content_id)
        .all()
    }

    if not scripts_by_lang:
        logger.error("No validated scripts for content %s", content_id)
        content.status = "FAILED"
        db.commit()
        return False

    is_short_episode: bool = bool(getattr(content, "is_short_episode", False))
    parent_content_id = getattr(content, "parent_content_id", None)

    # Compute Short-episode render params once (used in per-language loop)
    short_order: int | None = None
    short_total_parts: int | None = None
    if is_short_episode:
        _part_num    = getattr(content, "short_part_number", None) or 1
        short_order  = _part_num - 1
        short_total_parts = getattr(content, "short_total_parts", None)

    # ── Render readiness — Agent 4 must have already persisted VideoSections ──
    # Agent 5 only reads this table; it never generates or writes VideoSection
    # rows (no storyboard, no Flux, no remap, no Agent 4 fallback).
    beats_by_lang: dict[str, list[dict]] = {}
    for language in scripts_by_lang:
        sections = load_video_sections(content_id, language, db)
        if sections:
            beats_by_lang[language] = sections

    if not beats_by_lang:
        logger.warning(
            "RENDER_DEFERRED content_id=%s reason=visual_sections_missing",
            content_id,
        )
        return False

    # ── Render pass — per language ────────────────────────────────────────────
    successful = 0

    for language, script in scripts_by_lang.items():
        audio = audio_by_lang.get(language)
        if not audio:
            logger.warning(
                "No audio for language=%s content=%s — skipping render",
                language, content_id,
            )
            continue

        beats_for_lang = beats_by_lang.get(language)
        if not beats_for_lang:
            logger.warning(
                "RENDER_DEFERRED content_id=%s language=%s "
                "reason=visual_sections_missing_for_language",
                content_id, language,
            )
            continue

        if is_short_episode:
            logger.info(
                "CHILD_SHORT_RENDER_START content_id=%s parent_content_id=%s "
                "part=%s/%s language=%s format=short resolution=1080x1920",
                content_id, parent_content_id,
                getattr(content, "short_part_number", None),
                short_total_parts,
                language,
            )

        try:
            ok = _process_language(
                content_id=content_id,
                language=language,
                script=script,
                audio=audio,
                beats=beats_for_lang,
                channel=channel,
                karaoke_color=karaoke_color,
                db=db,
                is_short_episode=is_short_episode,
                short_order=short_order,
                short_total_parts=short_total_parts,
            )
            if ok:
                successful += 1
        except (RemotionCrashError, RemotionRenderError) as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=%s details=%s",
                language, content_id, type(exc).__name__, str(exc)[:200],
            )
            db.rollback()
        except Exception as exc:
            logger.error(
                "Video generation failed for language=%s, content=%s: %s",
                language, content_id, exc,
            )
            db.rollback()

    if successful > 0:
        content.status = "RENDERED"
        logger.info(
            "RENDER_DONE content_id=%s status=RENDERED languages=%d",
            content_id, successful,
        )
    else:
        content.status = "FAILED"
        logger.error("Video generation failed for ALL languages — content %s", content_id)

    db.commit()
    return successful > 0


# ── Read-only VideoSection access ──────────────────────────────────────────────
# Agent 5 only reads this table. Persistence (delete-then-insert) is owned by
# Agent 4 — see app.agents.agent4_visuals.services.visual_orchestrator. The
# loader itself is shared (roadmap 6.5 / audit AR-2) — see
# app.services.video_sections.load_video_sections.


# ── Per-language render pass ───────────────────────────────────────────────────

# Part-label templates for Standalone short architecture child short episodes (Short.tsx overlay text).
_PART_LABEL_TEMPLATES: dict[str, str] = {
    "fr": "Partie {n}/{total}",
    "es": "Parte {n}/{total}",
    "it": "Parte {n}/{total}",
    "pt": "Parte {n}/{total}",
}
_PART_LABEL_DEFAULT = "Part {n} of {total}"


def _build_part_label(language: str, part_number: int, total_parts: int) -> str:
    """Return a localised part label for a Short episode overlay."""
    template = _PART_LABEL_TEMPLATES.get(language[:2].lower(), _PART_LABEL_DEFAULT)
    return template.format(n=part_number, total=total_parts)


def _process_language(
    content_id: uuid.UUID,
    language: str,
    script: Script,
    audio: AudioFile,
    beats: list[dict],
    channel: Channel,
    karaoke_color: str,
    db: Session,
    is_short_episode: bool = False,
    short_order: int | None = None,
    short_total_parts: int | None = None,
) -> bool:
    """Run the render pipeline for one language using pre-generated beat images.

    Beats arrive with ``media_url`` already set to a local cache/ path
    (subtitles-only rendering — the retired ``__text_card__`` sentinel is
    never produced by live code; a legacy row carrying it is normalized to
    ``""`` on load and owned by the missing-media blocker). This function
    handles subtitles, Remotion props, render, and verification.

    For parent content (``is_short_episode=False``): renders MainVideo.tsx at
    1920×1080 (16:9) and stores ``VideoRender(format="main")``.

    For Standalone short architecture child short episodes (``is_short_episode=True``): renders
    Short.tsx at 1080×1920 (9:16) and stores ``VideoRender(format="short")``.

    Args:
        beats:            Beat dicts with media_url from the visual pass.
        is_short_episode: True for Standalone short architecture child short episode Content rows.
        short_order:      0-based short index (``short_part_number - 1``).
        short_total_parts: Total number of parts in the series.

    Returns:
        True on success, False on any critical failure.
    """
    cid_str    = str(content_id)
    media_root = Path(settings.media_path).resolve()
    props_dir  = media_root / "remotion_props"
    render_fmt = "short" if is_short_episode else "main"

    logger.info(
        "Agent5 [RENDER_START] language=%s content=%s "
        "beats=%d audio_duration_ms=%d format=%s",
        language, content_id, len(beats), audio.duration_ms, render_fmt,
    )

    # ── Phase check 1: already fully rendered? ─────────────────────────────────
    if _is_rendered(content_id, language, cid_str, media_root, db,
                    fmt=render_fmt, short_order=short_order):
        logger.info(
            "Agent5 [DONE] language=%s content=%s status=ALREADY_RENDERED",
            language, content_id,
        )
        return True

    # ── Phase check 2: props on disk → skip to render (unless stale) ──────────
    if is_short_episode:
        existing_props_file = props_dir / f"{cid_str}_{language}_short_{short_order}.json"
    else:
        existing_props_file = props_dir / f"{cid_str}_{language}_main.json"

    if existing_props_file.exists() and not _props_are_stale(
        existing_props_file, beats, audio.duration_ms,
    ):
        logger.info("Props found on disk for language=%s — skipping to render", language)
        try:
            return _render_from_existing_props(
                content_id, language, audio, cid_str, props_dir, db,
                is_short_episode=is_short_episode,
                short_order=short_order,
                short_total_parts=short_total_parts,
            )
        except VerifyFailedError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=VERIFY_FAILED "
                "reason=POST_RENDER_VERIFICATION (existing-props path) details=%s",
                language, content_id, str(exc)[:300],
            )
            _content_row = db.get(Content, content_id)
            if _content_row:
                _content_row.status = "NEEDS_REVIEW"
                db.commit()
            return False

    # VideoSection rows for this language are already persisted by Agent 4's
    # run_visual_generation() before this function is called.

    # ── Technical blocker check ────────────────────────────────────────────────
    standard_subs = build_standard_subtitles(audio.whisper_transcript or [])
    karaoke_subs  = build_karaoke_subtitles(audio.whisper_transcript or [], active_color=karaoke_color)

    blockers = _collect_technical_blockers(beats, standard_subs, audio, is_short_episode=is_short_episode)
    if blockers:
        logger.error(
            "Agent5 [FAIL] language=%s content=%s status=RENDER_BLOCKED "
            "blockers=%s",
            language, content_id, blockers,
        )
        return False

    # ── Pre-render props sanity check ──────────────────────────────────────────
    _sanity_ok, _sanity_reason = _check_props_sanity(beats, audio.duration_ms)
    if not _sanity_ok:
        logger.error(
            "Agent5 [FAIL] language=%s content=%s status=INVALID_PROPS reason=%s",
            language, content_id, _sanity_reason,
        )
        return False

    # ── Remotion builder ───────────────────────────────────────────────────────
    if is_short_episode:
        # Standalone short architecture child short episode — Short.tsx (9:16, 1080×1920)
        part_number = (short_order or 0) + 1
        short_dict = {
            "short_index":  short_order,
            "start_ms":     0,
            "end_ms":       audio.duration_ms,
            "sections":     beats,
            # Subtitles-only rendering: the part label is Remotion-drawn text,
            # so it ships only when the operator explicitly opts in.
            "part_label":   (
                _build_part_label(language, part_number, short_total_parts or 1)
                if settings.short_part_label_enabled else ""
            ),
            "total_parts":  short_total_parts or 1,
        }
        props_path = build_short_props(
            content_id=cid_str,
            language=language,
            audio_file_path=audio.file_path,
            short=short_dict,
            karaoke_subtitles=karaoke_subs,
        )
    else:
        # Parent content — MainVideo.tsx (16:9, 1920×1080)
        # Standalone short architecture only: parent content never generates short props.
        # Shorts are produced exclusively by standalone Standalone short architecture short episode Content rows.
        logger.info(
            "STANDALONE_SHORTS_ONLY content_id=%s language=%s "
            "parent_cut_shorts_removed=True shorts_path=standalone_episodes_only",
            content_id, language,
        )
        props_path = build_main_props(
            content_id=cid_str,
            language=language,
            audio_file_path=audio.file_path,
            duration_ms=audio.duration_ms,
            sections=beats,
            standard_subtitles=standard_subs,
            karaoke_subtitles=karaoke_subs,
        )

    # ── PRE_RENDER_ASSET_AUDIT — local images only ────────────────────────────
    _all_remote = _audit_props_for_remote_urls(props_path, render_fmt)

    if _all_remote:
        logger.error(
            "Agent5 [FAIL] language=%s content=%s status=REMOTE_URL_IN_PROPS "
            "reason=ASSET_AUDIT_FAILED remote_samples=%s",
            language, content_id, [u[:80] for u in _all_remote[:5]],
        )
        return False

    logger.debug(
        "Agent5 [PRE_RENDER] language=%s content=%s "
        "beats=%d duration_ms=%d "
        "standard_captions=%d karaoke_chunks=%d format=%s",
        language, content_id,
        len(beats), audio.duration_ms,
        len(standard_subs), len(karaoke_subs),
        render_fmt,
    )

    # ── Renderer ───────────────────────────────────────────────────────────────
    if is_short_episode:
        try:
            result = _run_short_render(
                content_id=content_id,
                language=language,
                cid_str=cid_str,
                audio=audio,
                short_order=short_order or 0,
                props_path=props_path,
                db=db,
            )
        except VerifyFailedError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=VERIFY_FAILED "
                "reason=POST_RENDER_VERIFICATION details=%s",
                language, content_id, str(exc)[:300],
            )
            _content_row = db.get(Content, content_id)
            if _content_row:
                _content_row.status = "NEEDS_REVIEW"
                db.commit()
            return False
        except (RemotionCrashError, RemotionRenderError) as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=%s details=%s",
                language, content_id, type(exc).__name__, str(exc)[:200],
            )
            return False
        logger.info(
            "CHILD_SHORT_RENDER_DONE path=%s duration_ms=%d",
            result["file_path"], audio.duration_ms,
        )
    else:
        try:
            _run_renders(
                content_id=content_id,
                language=language,
                cid_str=cid_str,
                audio=audio,
                main_props_path=props_path,
                db=db,
            )
        except VerifyFailedError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=VERIFY_FAILED "
                "reason=POST_RENDER_VERIFICATION details=%s",
                language, content_id, str(exc)[:300],
            )
            _content_row = db.get(Content, content_id)
            if _content_row:
                _content_row.status = "NEEDS_REVIEW"
                db.commit()
            return False
        except RemotionCrashError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=PAGE_CRASHED details=%s",
                language, content_id, str(exc)[:300],
            )
            return False
        except RemotionRenderError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=RENDER_ERROR details=%s",
                language, content_id, str(exc)[:300],
            )
            return False

    logger.info(
        "Agent5 [DONE] language=%s content=%s status=SUCCESS",
        language, content_id,
    )
    return True


# ── Render decision helpers ────────────────────────────────────────────────────

def _collect_technical_blockers(
    sections: list[dict],
    standard_subs: list[dict],
    audio: "AudioFile",
    is_short_episode: bool = False,
) -> list[str]:
    """Return technical blocker descriptions preventing a viable render.

    Args:
        sections:     Beat/section dicts after Flux generation.
        standard_subs: Standard subtitle chunks.
        audio:        AudioFile ORM record.
        is_short_episode: True for Standalone short architecture child Short
            content — gates the empty-transcript blocker below (roadmap 3.9 /
            audit A-3, exec-10).

    Returns:
        List of blocker strings (empty = render is viable).
    """
    blockers: list[str] = []

    if not sections:
        blockers.append("no_beats")
        return blockers

    # Beats without real media. Subtitles-only rendering: the legacy
    # text-card sentinel is NOT real media — a beat carrying it would render
    # as a bare dark frame, so it counts toward the missing-media blocker.
    no_media = sum(
        1 for s in sections
        if not (s.get("media_url", "") or "").startswith("cache/")
    )
    if no_media / len(sections) > _MISSING_MEDIA_BLOCK_RATIO:
        blockers.append(
            f"missing_media_critical ({no_media}/{len(sections)} beats have no media)"
        )

    whisper = audio.whisper_transcript or []
    if not standard_subs and whisper:
        blockers.append("no_captions (Whisper transcript present but caption build failed)")

    # Roadmap 3.9 / audit A-3, exec-10: an empty Whisper transcript (both
    # transcription engines exhausted — see agent3/services/whisper.py) was
    # previously tolerated silently, shipping a captionless vertical Short on
    # platforms where ~80% of viewers watch muted. Scoped to Shorts only —
    # parent long-form rendering already has its own extensive fallback
    # timing machinery and is not blocked by this check.
    if is_short_episode and not whisper:
        blockers.append(
            "empty_whisper_transcript_short (Shorts require captions — both "
            "transcription engines failed or returned no words)"
        )

    return blockers


def _check_props_sanity(sections: list[dict], duration_ms: int) -> tuple[bool, str]:
    """Validate sections for structural correctness before building props.

    Args:
        sections:    Section dicts as they will appear in the props JSON.
        duration_ms: Expected audio duration in milliseconds.

    Returns:
        ``(True, "")`` if all checks pass; ``(False, reason_string)`` otherwise.
    """
    import math

    errors: list[str] = []
    seen_orders: set  = set()
    max_end_ms        = 0

    for i, s in enumerate(sections):
        order = s.get("section_order", i)

        if order in seen_orders:
            errors.append(f"Duplicate section_order={order}")
        seen_orders.add(order)

        start = s.get("audio_start_ms")
        end   = s.get("audio_end_ms")

        if start is None or end is None:
            errors.append(f"Section {order}: missing audio_start_ms or audio_end_ms")
        elif not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            errors.append(f"Section {order}: non-numeric timing")
        elif math.isnan(start) or math.isnan(end):
            errors.append(f"Section {order}: NaN timing")
        elif start < 0 or end <= 0:
            errors.append(f"Section {order}: negative/zero timing")
        elif end <= start:
            errors.append(f"Section {order}: end ≤ start ({end} ≤ {start})")
        else:
            max_end_ms = max(max_end_ms, int(end))

        url = s.get("media_url", "")
        if url and url != _TEXT_CARD_SENTINEL:
            if url.startswith("http"):
                errors.append(f"Section {order}: remote URL survived to props: {url[:80]!r}")
            elif not url.startswith("cache/"):
                errors.append(f"Section {order}: unexpected non-local media_url: {url[:80]!r}")

        if len(errors) >= 5:
            break

    if len(errors) < 5 and duration_ms > 0 and max_end_ms > 0:
        drift_pct = abs(max_end_ms - duration_ms) / duration_ms * 100
        if drift_pct > 20:
            errors.append(
                f"Duration drift {drift_pct:.1f}%: sections end at {max_end_ms}ms "
                f"but audio is {duration_ms}ms"
            )

    if errors:
        return False, "; ".join(errors[:5])
    return True, ""


def _props_are_stale(existing_props_file: Path, beats: list[dict], duration_ms: int) -> bool:
    """Return True when an on-disk props file no longer matches the current
    DB-backed beats/duration (roadmap 2d / audit P0-3,
    code_report/forensic_output_audit_borrasca_run.md).

    A real production run logged "Props found on disk — skipping to render"
    and rendered a prior run's stale props verbatim after the underlying
    VideoSection/AudioFile data had changed (the audited repair scenario: a
    corrupted `duration_ms` corrected in place). Neither `VideoSection` nor
    `AudioFile` carries a row-level modification timestamp, so this compares
    three cheap, deterministic proxies for "the underlying data changed
    since these props were written": the props file's own persisted
    `duration_ms`, its section count, and its last section's `audio_end_ms`
    — against the same three values freshly computed from the current
    `beats`/`duration_ms` this call was given. Any mismatch is treated as
    stale. A missing or unparseable props file is also treated as stale
    (rebuild rather than crash on a corrupt file).
    """
    try:
        with open(existing_props_file, encoding="utf-8") as fh:
            existing_props = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "PROPS_STALENESS_CHECK_FAILED path=%s error=%s — treating as stale, rebuilding",
            existing_props_file, exc,
        )
        return True

    existing_sections = existing_props.get("sections") or []
    existing_last_end_ms = max(
        (s.get("audio_end_ms", 0) for s in existing_sections), default=0,
    )
    current_last_end_ms = max((b.get("audio_end_ms", 0) for b in beats), default=0)

    stale = (
        existing_props.get("duration_ms") != duration_ms
        or len(existing_sections) != len(beats)
        or existing_last_end_ms != current_last_end_ms
    )
    if stale:
        logger.warning(
            "PROPS_STALE_REBUILDING path=%s existing_duration_ms=%s current_duration_ms=%d "
            "existing_sections=%d current_sections=%d existing_last_end_ms=%d "
            "current_last_end_ms=%d — DB beats/duration no longer match the on-disk "
            "props file; rebuilding instead of reusing",
            existing_props_file, existing_props.get("duration_ms"), duration_ms,
            len(existing_sections), len(beats), existing_last_end_ms, current_last_end_ms,
        )
    return stale


def _audit_props_for_remote_urls(props_path: str, label: str) -> list[str]:
    """Scan a props JSON file and return all remote URLs found (http/https).

    Under the Flux architecture every URL should be a local cache/ path.

    Args:
        props_path: Absolute path to a props JSON file.
        label:      "main" or "short" — used only for log messages.

    Returns:
        List of remote URLs found (empty means audit passed).
    """
    try:
        with open(props_path, encoding="utf-8") as fh:
            props = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("PRE_RENDER_ASSET_AUDIT: cannot open %s props=%s: %s", label, props_path, exc)
        return []

    remote: list[str] = []

    def _scan_sections(secs: list) -> None:
        for sec in secs:
            for clip in sec.get("clips") or []:
                u = clip.get("url", "")
                if u.startswith("http"):
                    remote.append(u)
            u = sec.get("media_url", "")
            if u.startswith("http"):
                remote.append(u)

    _scan_sections(props.get("sections") or [])
    if remote:
        logger.warning(
            "PRE_RENDER_ASSET_AUDIT: %s props=%s remote_urls_found=%d samples=%s",
            label, props_path, len(remote), [u[:80] for u in remote[:3]],
        )
    return remote


# ── Phase-skip helpers ─────────────────────────────────────────────────────────

def _is_rendered(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    media_root: Path,
    db: Session,
    fmt: str = "main",
    short_order: int | None = None,
) -> bool:
    """Return True if the output MP4 exists on disk AND a VideoRender row is in DB."""
    q = db.query(VideoRender).filter(
        VideoRender.content_id == content_id,
        VideoRender.language   == language,
        VideoRender.format     == fmt,
    )
    if short_order is not None:
        q = q.filter(VideoRender.short_order == short_order)
    row = q.first()
    if not row:
        return False
    if fmt == "short" and short_order is not None:
        mp4 = media_root / "video" / cid_str / f"{language}_short_{short_order}.mp4"
    else:
        mp4 = media_root / "video" / cid_str / f"{language}_main.mp4"
    return mp4.exists()


def _run_short_render(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    audio: "AudioFile",
    short_order: int,
    props_path: str,
    db: Session,
    concurrency: int | None = None,
) -> dict:
    """Render a Standalone short architecture child short episode with Short.tsx (9:16, 1080×1920).

    Args:
        content_id:  UUID of the child short episode Content row.
        language:    Language code.
        cid_str:     str(content_id).
        audio:       AudioFile record for this language.
        short_order: 0-based index (short_part_number - 1).
        props_path:  Absolute path to the short props JSON file.
        db:          SQLAlchemy session.
        concurrency: Chromium tab concurrency override.

    Returns:
        Result dict with file_path, duration_seconds, render_time_seconds.

    Raises:
        RemotionCrashError:  Chromium crashed.
        RemotionRenderError: Remotion exited non-zero.
        VerifyFailedError:   Post-render verification failed.
    """
    bundle_dir = ensure_bundle()
    result = render_short(
        content_id=cid_str,
        language=language,
        short_index=short_order,
        props_path=props_path,
        duration_ms=audio.duration_ms,
        concurrency=concurrency,
        bundle_dir=bundle_dir,
    )

    if settings.verify_renders:
        issues = verify_render(
            mp4_path=result["file_path"],
            audio_file_path=audio.file_path,   # roadmap 2c / audit P2-6: Shorts get the duration check too
            fmt="short",
        )
        if issues:
            raise VerifyFailedError(
                f"Short render verification failed for language={language} "
                f"short_order={short_order}: {issues}"
            )

    db.add(VideoRender(
        content_id=content_id,
        language=language,
        format="short",
        short_order=short_order,
        duration_seconds=result["duration_seconds"],
        render_time_seconds=result["render_time_seconds"],
    ))
    db.commit()
    logger.info(
        "Short %d rendered: language=%s duration=%.1fs render_time=%.1fs",
        short_order, language,
        result["duration_seconds"], result["render_time_seconds"],
    )
    return result


def _render_from_existing_props(
    content_id: uuid.UUID,
    language: str,
    audio: AudioFile,
    cid_str: str,
    props_dir: Path,
    db: Session,
    is_short_episode: bool = False,
    short_order: int | None = None,
    short_total_parts: int | None = None,
) -> bool:
    """Render main video or short from props files already on disk.

    Raises:
        VerifyFailedError: Post-render verification failed.
        RemotionCrashError: Chromium crashed.
        RemotionRenderError: Remotion exited non-zero.
    """
    if is_short_episode:
        short_props_path = str(props_dir / f"{cid_str}_{language}_short_{short_order}.json")
        if not _render_exists(content_id, language, "short", short_order, db):
            result = _run_short_render(
                content_id=content_id,
                language=language,
                cid_str=cid_str,
                audio=audio,
                short_order=short_order or 0,
                props_path=short_props_path,
                db=db,
            )
            logger.info(
                "CHILD_SHORT_RENDER_DONE path=%s duration_ms=%d (existing-props path)",
                result["file_path"], audio.duration_ms,
            )
        else:
            logger.info("Short render already done for language=%s — skipping", language)
        logger.info("Render from existing props complete for language=%s (short)", language)
        return True

    # ── Main video path ────────────────────────────────────────────────────────
    main_props_path = str(props_dir / f"{cid_str}_{language}_main.json")
    bundle_dir = ensure_bundle()

    if not _render_exists(content_id, language, "main", None, db):
        main_result = render_main_video(
            content_id=cid_str,
            language=language,
            props_path=main_props_path,
            duration_ms=audio.duration_ms,
            bundle_dir=bundle_dir,
        )
        if settings.verify_renders:
            issues = verify_render(
                mp4_path=main_result["file_path"],
                audio_file_path=audio.file_path,
                fmt="main",
            )
            if issues:
                raise VerifyFailedError(
                    f"Main render (existing-props) verification failed for language={language}: {issues}"
                )
        db.add(VideoRender(
            content_id=content_id,
            language=language,
            format="main",
            short_order=None,
            duration_seconds=main_result["duration_seconds"],
                render_time_seconds=main_result["render_time_seconds"],
        ))
        db.commit()
    else:
        logger.info("Main render already done for language=%s — skipping", language)

    logger.info("Render from existing props complete for language=%s", language)
    return True


def _render_exists(
    content_id: uuid.UUID,
    language: str,
    fmt: str,
    short_order: int | None,
    db: Session,
) -> bool:
    """Check if a VideoRender row already exists for this combination."""
    q = db.query(VideoRender).filter(
        VideoRender.content_id == content_id,
        VideoRender.language   == language,
        VideoRender.format     == fmt,
    )
    if short_order is not None:
        q = q.filter(VideoRender.short_order == short_order)
    return q.first() is not None


# ── Render execution ───────────────────────────────────────────────────────────

def _run_renders(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    audio: AudioFile,
    main_props_path: str,
    db: Session,
    concurrency: int | None = None,
) -> None:
    """Render the main video, verifying it before saving a VideoRender row.

    Standalone short architecture only: short renders are never triggered from here.
    Shorts are handled by standalone Standalone short architecture short episode Content rows.

    Raises:
        RemotionCrashError:  Chromium crashed.
        RemotionRenderError: Remotion exited non-zero.
        VerifyFailedError:   Render passed but failed post-render verification.
    """
    bundle_dir = ensure_bundle()

    use_chunked = (
        settings.chunked_render_enabled
        and audio.duration_ms > settings.chunk_duration_sec * 1000
    )
    if use_chunked:
        main_result = render_main_video_chunked(
            content_id=cid_str,
            language=language,
            props_path=main_props_path,
            duration_ms=audio.duration_ms,
            audio_file_path=audio.file_path,
            concurrency=concurrency,
            bundle_dir=bundle_dir,
        )
    else:
        main_result = render_main_video(
            content_id=cid_str,
            language=language,
            props_path=main_props_path,
            duration_ms=audio.duration_ms,
            concurrency=concurrency,
            bundle_dir=bundle_dir,
        )

    if settings.verify_renders:
        main_issues = verify_render(
            mp4_path=main_result["file_path"],
            audio_file_path=audio.file_path,
            fmt="main",
        )
        if main_issues:
            raise VerifyFailedError(
                f"Main render verification failed for language={language}: {main_issues}"
            )

    db.add(VideoRender(
        content_id=content_id,
        language=language,
        format="main",
        short_order=None,
        duration_seconds=main_result["duration_seconds"],
        render_time_seconds=main_result["render_time_seconds"],
    ))
    db.commit()

    logger.info(
        "language=%s done: 1 main + 0 parent-cut shorts (standalone shorts only) for content %s render_mode=%s",
        language, content_id,
        "chunked" if use_chunked else "single",
    )


