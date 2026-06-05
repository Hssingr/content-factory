"""Subtitles generator — converts Whisper word-level timestamps into subtitle data.

Two output styles:

  standard  (main 16:9 video)
    Groups consecutive words into readable caption chunks (≤ MAX_WORDS or ≤ MAX_DURATION_MS).
    Output: [{text, start_ms, end_ms}, ...]

  karaoke   (Shorts 9:16)
    Groups words into chunks, but each chunk carries individual word timings so Remotion
    can highlight the word currently being spoken in the active color.
    Output: [{words: [{w, s, e}, ...], start_ms, end_ms}, ...]

No Claude call — pure Python computed from Whisper timestamps.
Whisper transcript format: [{"word": str, "start": float, "end": float}]
"""

import logging

logger = logging.getLogger(__name__)

# Caption chunk limits
_MAX_WORDS_STANDARD   = 8
_MAX_DURATION_MS      = 3000   # split chunk if it would exceed 3 s
_MAX_WORDS_KARAOKE    = 5      # smaller chunks for karaoke style (easier to follow)

_DEFAULT_KARAOKE_COLOR = "#FFD700"


def build_standard_subtitles(whisper_transcript: list[dict]) -> list[dict]:
    """Generate standard subtitle captions from Whisper word timestamps.

    Chunks are split when they reach MAX_WORDS or MAX_DURATION_MS, whichever
    comes first. Timestamps come directly from Whisper — no approximation.

    Args:
        whisper_transcript: Word-level Whisper output.
                            Each word: {"word": str, "start": float, "end": float}

    Returns:
        List of caption dicts: [{text, start_ms, end_ms}, ...]
        Empty list if transcript is empty.
    """
    if not whisper_transcript:
        return []

    captions: list[dict] = []
    chunk_words: list[str] = []
    chunk_start_ms: int    = 0
    chunk_end_ms: int      = 0

    for word_data in whisper_transcript:
        word       = word_data.get("word", "").strip()
        start_ms   = int(word_data.get("start", 0) * 1000)
        end_ms_val = int(word_data.get("end", 0) * 1000)

        if not word:
            continue

        if not chunk_words:
            # Start a new chunk
            chunk_start_ms = start_ms

        chunk_words.append(word)
        chunk_end_ms = end_ms_val

        # Check split conditions
        duration = chunk_end_ms - chunk_start_ms
        if len(chunk_words) >= _MAX_WORDS_STANDARD or duration >= _MAX_DURATION_MS:
            captions.append({
                "text":     " ".join(chunk_words),
                "start_ms": chunk_start_ms,
                "end_ms":   chunk_end_ms,
            })
            chunk_words    = []
            chunk_start_ms = 0
            chunk_end_ms   = 0

    # Flush remaining words
    if chunk_words:
        captions.append({
            "text":     " ".join(chunk_words),
            "start_ms": chunk_start_ms,
            "end_ms":   chunk_end_ms,
        })

    logger.info("Standard subtitles: %d caption(s)", len(captions))
    return captions


def build_karaoke_subtitles(
    whisper_transcript: list[dict],
    active_color: str = _DEFAULT_KARAOKE_COLOR,
) -> list[dict]:
    """Generate karaoke-style subtitle chunks from Whisper word timestamps.

    Each chunk contains individual word timings so Remotion can highlight the
    currently spoken word in ``active_color``.

    Args:
        whisper_transcript: Word-level Whisper output.
        active_color:       CSS hex color for the currently spoken word.
                            Defaults to #FFD700 (gold).

    Returns:
        List of karaoke chunk dicts:
        [{words: [{w, s, e}, ...], start_ms, end_ms, active_color}, ...]
        Empty list if transcript is empty.
    """
    if not whisper_transcript:
        return []

    chunks: list[dict] = []
    chunk_words: list[dict] = []
    chunk_start_ms: int     = 0
    chunk_end_ms: int       = 0

    for word_data in whisper_transcript:
        word       = word_data.get("word", "").strip()
        start_ms   = int(word_data.get("start", 0) * 1000)
        end_ms_val = int(word_data.get("end", 0) * 1000)

        if not word:
            continue

        if not chunk_words:
            chunk_start_ms = start_ms

        chunk_words.append({"w": word, "s": start_ms, "e": end_ms_val})
        chunk_end_ms = end_ms_val

        duration = chunk_end_ms - chunk_start_ms
        if len(chunk_words) >= _MAX_WORDS_KARAOKE or duration >= _MAX_DURATION_MS:
            chunks.append({
                "words":        chunk_words,
                "start_ms":     chunk_start_ms,
                "end_ms":       chunk_end_ms,
                "active_color": active_color,
            })
            chunk_words    = []
            chunk_start_ms = 0
            chunk_end_ms   = 0

    if chunk_words:
        chunks.append({
            "words":        chunk_words,
            "start_ms":     chunk_start_ms,
            "end_ms":       chunk_end_ms,
            "active_color": active_color,
        })

    logger.info("Karaoke subtitles: %d chunk(s)", len(chunks))
    return chunks
