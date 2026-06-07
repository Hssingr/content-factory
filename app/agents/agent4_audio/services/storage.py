import logging
import uuid
from pathlib import Path

from mutagen.mp3 import MP3

from app.config import settings

logger = logging.getLogger(__name__)


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
        mutagen.MutagenError: If the file is not valid mp3.
    """
    if not audio_bytes:
        raise ValueError(f"Empty audio bytes for content={content_id} lang={language}")

    path = _audio_path(content_id, language)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(audio_bytes)
    logger.info("Audio saved: %s (%d KB)", path, len(audio_bytes) // 1024)

    duration_ms = _measure_duration_ms(path)
    logger.info("Duration: %d ms (%.1f s)", duration_ms, duration_ms / 1000)

    return str(path), duration_ms


def _measure_duration_ms(path: Path) -> int:
    """Return the exact audio duration in milliseconds using mutagen.

    Args:
        path: Absolute path to the mp3 file.

    Returns:
        Duration in milliseconds (integer).
    """
    audio = MP3(str(path))
    return int(audio.info.length * 1000)
