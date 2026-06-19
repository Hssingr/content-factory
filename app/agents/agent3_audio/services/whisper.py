import logging
import time
from pathlib import Path

from app.services.openai_client import get_client

logger = logging.getLogger(__name__)

_WHISPER_MODEL = "whisper-1"
_MAX_RETRIES   = 3
_BACKOFF_BASE  = 2.0   # seconds; delay = base ** attempt


def transcribe(file_path: str, language: str | None = None) -> list[dict]:
    """Transcribe an audio file using OpenAI Whisper with word-level timestamps.

    Attempts the OpenAI Whisper API up to ``_MAX_RETRIES`` times with exponential
    backoff. On final API failure, falls back to local ``faster-whisper`` (small
    model, CPU, int8). Only if both fail does the function return an empty list.

    Engine used is logged at INFO level for observability.

    Args:
        file_path: Absolute path to the mp3 file produced by ``storage.save_audio()``.
        language:  Optional BCP-47 hint (e.g. "fr") to improve accuracy.
                   If None, Whisper auto-detects the language.

    Returns:
        List of word-timing dicts:
        ``[{"word": str, "start": float, "end": float}, ...]``
        where ``start`` and ``end`` are offsets in seconds from the audio start.
        Returns an empty list if both engines fail.

    Raises:
        FileNotFoundError: If ``file_path`` does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    logger.info("Whisper transcription start: %s lang=%s", path.name, language or "auto")

    result = _try_openai_whisper(path, language)
    if result is not None:
        return result

    logger.warning(
        "OpenAI Whisper failed after %d attempt(s) for %s — trying local faster-whisper",
        _MAX_RETRIES, path.name,
    )
    result = _try_faster_whisper(path, language)
    if result is not None:
        return result

    logger.error(
        "Both OpenAI Whisper and faster-whisper failed for %s — returning empty transcript",
        path.name,
    )
    return []


# ── Private helpers ───────────────────────────────────────────────────────────

def _try_openai_whisper(path: Path, language: str | None) -> list[dict] | None:
    """Attempt OpenAI Whisper API with exponential backoff.

    Returns:
        Parsed word-timing list on success, ``None`` on final failure.
    """
    kwargs: dict = {
        "model": _WHISPER_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word"],
    }
    if language:
        kwargs["language"] = language

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with path.open("rb") as audio_file:
                transcript = get_client().audio.transcriptions.create(
                    file=audio_file,
                    **kwargs,
                )
            words_raw = getattr(transcript, "words", None) or []
            result = [
                {
                    "word":  w.word.strip(),
                    "start": round(float(w.start), 3),
                    "end":   round(float(w.end), 3),
                }
                for w in words_raw
                if w.word.strip()
            ]
            logger.info(
                "OpenAI Whisper complete (attempt %d/%d): %d words, duration %.1fs engine=openai",
                attempt, _MAX_RETRIES,
                len(result),
                result[-1]["end"] if result else 0.0,
            )
            return result

        except Exception as exc:
            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE ** attempt
                logger.warning(
                    "OpenAI Whisper attempt %d/%d failed: %s — retrying in %.1fs",
                    attempt, _MAX_RETRIES, exc, delay,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "OpenAI Whisper attempt %d/%d failed: %s — no more retries",
                    attempt, _MAX_RETRIES, exc,
                )
    return None


def _try_faster_whisper(path: Path, language: str | None) -> list[dict] | None:
    """Attempt local transcription via ``faster-whisper`` (small model, CPU).

    Normalizes the output to the same ``[{"word", "start", "end"}]`` shape as
    the OpenAI Whisper path.

    Returns:
        Parsed word-timing list on success, ``None`` on failure or missing package.
    """
    try:
        from faster_whisper import WhisperModel  # optional dependency
    except ImportError:
        logger.warning("faster-whisper not installed — local fallback unavailable")
        return None

    try:
        model = WhisperModel("small", device="cpu", compute_type="int8")
        transcribe_kwargs: dict = {"word_timestamps": True}
        if language:
            transcribe_kwargs["language"] = language.split("-")[0]

        segments, _info = model.transcribe(str(path), **transcribe_kwargs)

        result: list[dict] = []
        for segment in segments:
            for word in (segment.words or []):
                w = word.word.strip()
                if w:
                    result.append({
                        "word":  w,
                        "start": round(float(word.start), 3),
                        "end":   round(float(word.end), 3),
                    })

        logger.info(
            "faster-whisper complete: %d words, duration %.1fs engine=faster-whisper",
            len(result),
            result[-1]["end"] if result else 0.0,
        )
        return result

    except Exception as exc:
        logger.error("faster-whisper failed: %s", exc)
        return None
