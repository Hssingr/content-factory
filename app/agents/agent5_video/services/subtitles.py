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
import re

logger = logging.getLogger(__name__)

# Caption chunk limits — chunks split on natural phrase/sentence boundaries with a
# minimum size, rather than purely on a word-count or duration ceiling that could
# fire mid-phrase and produce broken-fragment captions.
_MIN_WORDS_STANDARD      = 3
_TARGET_WORDS_STANDARD   = 7
_MAX_WORDS_STANDARD      = 12
_MAX_DURATION_MS         = 4500   # split chunk if it would exceed 4.5 s

_MIN_WORDS_KARAOKE       = 2
_TARGET_WORDS_KARAOKE    = 4
_MAX_WORDS_KARAOKE       = 6      # smaller chunks for karaoke style (easier to follow)
_MAX_DURATION_MS_KARAOKE = 3000

_DEFAULT_KARAOKE_COLOR = "#FFD700"

_SENTENCE_END_RE = re.compile(r"[.!?…]$")
_CLAUSE_END_RE   = re.compile(r"[,;:—–]$")


def _chunk_transcript(
    whisper_transcript: list[dict],
    min_words: int,
    target_words: int,
    max_words: int,
    max_duration_ms: int,
) -> list[list[dict]]:
    """Group Whisper words into readable caption chunks on natural boundaries.

    Splits preferentially at sentence-ending punctuation (``.!?…``), then at
    clause-ending punctuation (``,;:—–``) once the chunk has reached
    ``target_words``, and only falls back to a hard word-count/duration ceiling
    when no natural boundary appears in time. A trailing chunk smaller than
    ``min_words`` is merged into the previous chunk so captions never end on an
    orphaned one- or two-word fragment.

    Args:
        whisper_transcript: Word-level Whisper output
                            (``[{"word": str, "start": float, "end": float}]``).
        min_words:          Minimum words before a punctuation-based split is allowed.
        target_words:       Word count at which a clause-boundary split becomes preferred.
        max_words:          Hard ceiling — always split once a chunk reaches this size.
        max_duration_ms:    Hard ceiling — always split once a chunk reaches this duration.

    Returns:
        List of chunks; each chunk is a list of
        ``{"word": str, "start_ms": int, "end_ms": int}`` dicts, in order.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []

    for word_data in whisper_transcript:
        word = word_data.get("word", "").strip()
        if not word:
            continue

        current.append({
            "word":     word,
            "start_ms": int(word_data.get("start", 0) * 1000),
            "end_ms":   int(word_data.get("end", 0) * 1000),
        })

        n        = len(current)
        duration = current[-1]["end_ms"] - current[0]["start_ms"]

        if n >= max_words or duration >= max_duration_ms:
            chunks.append(current)
            current = []
        elif n >= min_words and _SENTENCE_END_RE.search(word):
            chunks.append(current)
            current = []
        elif n >= target_words and _CLAUSE_END_RE.search(word):
            chunks.append(current)
            current = []

    if current:
        if chunks and len(current) < min_words:
            chunks[-1].extend(current)
        else:
            chunks.append(current)

    return chunks


def build_standard_subtitles(whisper_transcript: list[dict]) -> list[dict]:
    """Generate standard subtitle captions from Whisper word timestamps.

    Chunks split on natural sentence/clause boundaries (with minimum-size and
    hard-ceiling safeguards via ``_chunk_transcript``) so captions read as clean,
    grammatically coherent phrases rather than broken word fragments. Timestamps
    come directly from Whisper — no approximation.

    Args:
        whisper_transcript: Word-level Whisper output.
                            Each word: {"word": str, "start": float, "end": float}

    Returns:
        List of caption dicts: [{text, start_ms, end_ms}, ...]
        Empty list if transcript is empty.
    """
    if not whisper_transcript:
        return []

    chunks = _chunk_transcript(
        whisper_transcript,
        min_words=_MIN_WORDS_STANDARD,
        target_words=_TARGET_WORDS_STANDARD,
        max_words=_MAX_WORDS_STANDARD,
        max_duration_ms=_MAX_DURATION_MS,
    )
    captions = [
        {
            "text":     " ".join(w["word"] for w in chunk),
            "start_ms": chunk[0]["start_ms"],
            "end_ms":   chunk[-1]["end_ms"],
        }
        for chunk in chunks
    ]

    total_words = sum(len(c["text"].split()) for c in captions)
    avg_words   = total_words / len(captions) if captions else 0.0
    logger.info(
        "Standard subtitles: %d caption(s), avg %.1f words/caption",
        len(captions), avg_words,
    )
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

    raw_chunks = _chunk_transcript(
        whisper_transcript,
        min_words=_MIN_WORDS_KARAOKE,
        target_words=_TARGET_WORDS_KARAOKE,
        max_words=_MAX_WORDS_KARAOKE,
        max_duration_ms=_MAX_DURATION_MS_KARAOKE,
    )
    chunks = [
        {
            "words":        [{"w": w["word"], "s": w["start_ms"], "e": w["end_ms"]} for w in chunk],
            "start_ms":     chunk[0]["start_ms"],
            "end_ms":       chunk[-1]["end_ms"],
            "active_color": active_color,
        }
        for chunk in raw_chunks
    ]

    logger.info("Karaoke subtitles: %d chunk(s)", len(chunks))
    return chunks
