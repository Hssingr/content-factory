import json
import logging
import subprocess
import uuid
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_FFPROBE_TIMEOUT_SEC = 30


def _audio_path(content_id: uuid.UUID, language: str) -> Path:
    """Return the absolute path for an audio file.

    Structure: <MEDIA_PATH>/audio/<content_id>/<language>.mp3
    """
    base = Path(settings.media_path).resolve()
    return base / "audio" / str(content_id) / f"{language}.mp3"


def audio_path(content_id: uuid.UUID, language: str) -> Path:
    """Public accessor for the expected audio file path.

    Use this to check whether a file already exists on disk before
    deciding whether to re-call ElevenLabs TTS.

    Args:
        content_id: UUID of the content item.
        language:   BCP-47 language code.

    Returns:
        Absolute ``Path`` — may or may not exist yet.
    """
    return _audio_path(content_id, language)


def save_audio(content_id: uuid.UUID, language: str, audio_bytes: bytes) -> tuple[str, int]:
    """Write mp3 bytes to disk and measure exact duration.

    Creates parent directories automatically.

    Args:
        content_id:  UUID of the content item.
        language:    BCP-47 language code (e.g. "fr", "en").
        audio_bytes: Raw mp3 bytes returned by ``tts.generate_audio()``.

    Returns:
        Tuple of ``(file_path, duration_ms)`` where ``file_path`` is the
        path stored in the DB and ``duration_ms`` is the exact audio length.

    Raises:
        ValueError: If ``audio_bytes`` is empty.
        RuntimeError: If ffprobe is unavailable or reports no usable duration.
    """
    if not audio_bytes:
        raise ValueError(f"Empty audio bytes for content={content_id} lang={language}")

    path = _audio_path(content_id, language)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(audio_bytes)
    logger.debug("Audio saved: %s (%d KB)", path, len(audio_bytes) // 1024)

    duration_ms = measure_audio_duration_ms(path)
    logger.debug("Duration: %d ms (%.1f s)", duration_ms, duration_ms / 1000)

    return str(path), duration_ms


def measure_audio_duration_ms(path: Path) -> int:
    """Return the exact audio duration in milliseconds using ffprobe.

    ffprobe reads the container's real stream duration rather than a single
    file header (roadmap 2a / audit P0-1,
    code_report/forensic_output_audit_borrasca_run.md): a prior
    mutagen-based measurement (``MP3(path).info.length``) parses only the
    first frame header it finds, which silently read just the first chunk's
    length on a raw-byte-concatenated multi-chunk mp3 — 161.7s stored for a
    real 616.8s file, corrupting every downstream timeline (beat budget,
    timestamp mapping, render length, WPM calibration). This is the single
    duration-measurement point for every caller (fresh generation and the
    on-disk resume path in ``audio.py``) — fails loud (raises) rather than
    falling back to a wrong duration, since a corrupting fallback is worse
    than a failed run.

    Args:
        path: Absolute path to the mp3 file.

    Returns:
        Duration in milliseconds (integer).

    Raises:
        RuntimeError: If ffprobe is unavailable or reports no usable duration.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_FFPROBE_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"ffprobe is not available — cannot measure audio duration for {path}"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (exit {result.returncode}) measuring {path}: "
            f"{result.stderr[:200]}"
        )

    try:
        info = json.loads(result.stdout)
        duration_sec = float(info["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"ffprobe returned no usable duration for {path}: {exc}"
        ) from exc

    return int(round(duration_sec * 1000))
