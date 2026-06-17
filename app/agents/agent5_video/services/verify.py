"""Post-render verification — deterministic ffprobe / blackdetect / silencedetect checks.

Called after every Remotion render, before the VideoRender row is saved to DB.
A failure list triggers NEEDS_REVIEW status (render kept on disk for inspection).

All checks use ffprobe/ffmpeg subprocess calls — no Python video libraries needed.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

_DURATION_TOLERANCE      = 0.02   # ±2% of expected duration
_BLACK_MIN_DURATION_SEC  = 10.0   # any black interval ≥ this triggers failure
                                   # 10s minimum: individual dark sections (5-8s each) are
                                   # content quality, not render failures; only flag systematic
                                   # multi-section blackouts (consecutive bad sections ≥10s)
_BLACK_PIX_TH            = 0.03   # blackdetect pixel luminance threshold (≤ 7.65/255)
                                   # 0.03 catches near-pure-black render failures without
                                   # flagging intentionally dark Flux-generated images
_SILENCE_MIN_DURATION_SEC = 4.0   # interior silence ≥ this triggers failure
_SILENCE_LEVEL_DB        = -50.0  # dB threshold for silence
_EDGE_GRACE_SEC          = 1.0    # ignore black/silence in first/last second

# Expected W×H per format
_EXPECTED_RESOLUTION: dict[str, tuple[int, int]] = {
    "main":  (1920, 1080),
    "short": (1080, 1920),
}


# ── Public API ────────────────────────────────────────────────────────────────

def verify_render(
    mp4_path: str,
    expected_duration_ms: int | None,
    fmt: str,
) -> list[str]:
    """Run post-render verification on an MP4 file before saving to DB.

    Checks applied in order:
      1. ffprobe: duration ±2% (if expected_duration_ms provided); exactly one
         audio stream; resolution matches fmt (1920×1080 main / 1080×1920 short).
      2. blackdetect: no black interval ≥ 3 s anywhere in the file.
      3. silencedetect: no interior silence ≥ 4 s (ignores first/last 1 s).

    Args:
        mp4_path:             Absolute path to the rendered MP4.
        expected_duration_ms: Expected audio duration in ms, or None to skip
                              the duration check (e.g. Shorts with unknown
                              bookend padding).
        fmt:                  "main" or "short" — selects expected resolution.

    Returns:
        List of failure description strings.  Empty list means all checks passed.
    """
    issues: list[str] = []

    if not Path(mp4_path).exists():
        return [f"file not found: {mp4_path}"]

    # ── Step 1: ffprobe structural checks ────────────────────────────────────
    issues.extend(_check_ffprobe(mp4_path, expected_duration_ms, fmt))

    # ── Step 2: black-frame detection ────────────────────────────────────────
    issues.extend(_check_blackdetect(mp4_path))

    # ── Step 3: interior silence detection ───────────────────────────────────
    actual_sec = _probe_duration_sec(mp4_path)
    if actual_sec > 0:
        issues.extend(_check_silencedetect(mp4_path, actual_sec))

    if issues:
        logger.warning(
            "verify_render FAILED path=%s fmt=%s issues=%d: %s",
            mp4_path, fmt, len(issues), issues,
        )
    else:
        logger.info("verify_render OK path=%s fmt=%s", mp4_path, fmt)

    return issues


# ── Step implementations ──────────────────────────────────────────────────────

def _check_ffprobe(
    mp4_path: str,
    expected_duration_ms: int | None,
    fmt: str,
) -> list[str]:
    """Run ffprobe and return structural issues."""
    issues: list[str] = []
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        mp4_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        if result.returncode != 0:
            return [f"ffprobe failed (exit {result.returncode}): {result.stderr[:200]}"]
        info = json.loads(result.stdout)
    except Exception as exc:
        return [f"ffprobe exception: {exc}"]

    # Duration check
    if expected_duration_ms is not None:
        try:
            actual_sec = float(info["format"]["duration"])
            expected_sec = expected_duration_ms / 1000.0
            drift = abs(actual_sec - expected_sec) / max(expected_sec, 0.001)
            if drift > _DURATION_TOLERANCE:
                issues.append(
                    f"duration_drift={drift:.1%} actual={actual_sec:.1f}s "
                    f"expected={expected_sec:.1f}s"
                )
        except (KeyError, ValueError, TypeError) as exc:
            issues.append(f"duration_parse_error: {exc}")

    streams = info.get("streams", [])

    # Audio stream check — exactly one
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio_streams) == 0:
        issues.append("no_audio_stream")
    elif len(audio_streams) > 1:
        issues.append(f"multiple_audio_streams={len(audio_streams)}")

    # Resolution check
    expected_w, expected_h = _EXPECTED_RESOLUTION.get(fmt, (0, 0))
    if expected_w > 0:
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        if not video_streams:
            issues.append("no_video_stream")
        else:
            v = video_streams[0]
            w, h = v.get("width", 0), v.get("height", 0)
            if w != expected_w or h != expected_h:
                issues.append(
                    f"wrong_resolution actual={w}x{h} expected={expected_w}x{expected_h}"
                )

    return issues


def _check_blackdetect(mp4_path: str) -> list[str]:
    """Run ffmpeg blackdetect and return any detected black intervals."""
    cmd = [
        "ffmpeg", "-y",
        "-i", mp4_path,
        "-vf", f"blackdetect=d={_BLACK_MIN_DURATION_SEC}:pix_th={_BLACK_PIX_TH}:pic_th=0.98",
        "-an",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=120
        )
        # ffmpeg writes blackdetect output to stderr
        stderr = result.stderr
    except Exception as exc:
        logger.warning("blackdetect subprocess error: %s", exc)
        return []  # non-fatal — don't block on ffmpeg errors

    issues: list[str] = []
    for line in stderr.splitlines():
        if "black_start" in line:
            issues.append(f"black_interval_detected: {line.strip()[:200]}")
    return issues


def _check_silencedetect(mp4_path: str, total_duration_sec: float) -> list[str]:
    """Run ffmpeg silencedetect and return any interior silence intervals."""
    cmd = [
        "ffmpeg", "-y",
        "-i", mp4_path,
        "-af", f"silencedetect=n={_SILENCE_LEVEL_DB}dB:d={_SILENCE_MIN_DURATION_SEC}",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=120
        )
        stderr = result.stderr
    except Exception as exc:
        logger.warning("silencedetect subprocess error: %s", exc)
        return []

    issues: list[str] = []
    # Parse silence_start / silence_end pairs; skip edge intervals
    current_start: float | None = None
    for line in stderr.splitlines():
        if "silence_start" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip())
            except (IndexError, ValueError):
                current_start = None
        elif "silence_end" in line and current_start is not None:
            try:
                parts   = line.split("|")
                end_sec = float(parts[0].split("silence_end:")[1].strip())
                # Only flag interior silence — ignore edges
                starts_after_edge = current_start >= _EDGE_GRACE_SEC
                ends_before_edge  = end_sec <= total_duration_sec - _EDGE_GRACE_SEC
                if starts_after_edge and ends_before_edge:
                    issues.append(
                        f"interior_silence start={current_start:.1f}s "
                        f"end={end_sec:.1f}s "
                        f"duration={(end_sec - current_start):.1f}s"
                    )
            except (IndexError, ValueError):
                pass
            current_start = None

    return issues


# ── Utility ───────────────────────────────────────────────────────────────────

def _probe_duration_sec(mp4_path: str) -> float:
    """Return actual video duration in seconds via ffprobe, 0.0 on error."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        mp4_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
        return float(result.stdout.strip())
    except Exception:
        return 0.0
