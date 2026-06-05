import logging
from pathlib import Path

from app.services.openai_client import get_client

logger = logging.getLogger(__name__)

_WHISPER_MODEL = "whisper-1"


def transcribe(file_path: str, language: str | None = None) -> list[dict]:
    """Transcribe an audio file using OpenAI Whisper with word-level timestamps.

    Sends the mp3 file to the Whisper API and returns word-level timing data
    used for karaoke-style subtitle generation in Agent 5.

    Args:
        file_path: Absolute path to the mp3 file produced by ``storage.save_audio()``.
        language:  Optional BCP-47 hint (e.g. "fr") to improve accuracy.
                   If None, Whisper auto-detects the language.

    Returns:
        List of word-timing dicts:
        ``[{"word": str, "start": float, "end": float}, ...]``
        where ``start`` and ``end`` are offsets in seconds from the audio start.
        Returns an empty list if transcription returns no word data.

    Raises:
        RuntimeError: If OPENAI_API_KEY is not configured.
        openai.APIError: On any OpenAI API error.
        FileNotFoundError: If ``file_path`` does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    logger.info("Whisper transcription start: %s lang=%s", path.name, language or "auto")

    kwargs: dict = {
        "model": _WHISPER_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word"],
    }
    if language:
        kwargs["language"] = language

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
        "Whisper complete: %d words, duration %.1fs",
        len(result),
        result[-1]["end"] if result else 0.0,
    )
    return result
