"""Shorts breakpoints recalculation using real Whisper word-level timestamps.

Agent 3 estimated breakpoints from word counts and speech rates.
Agent 4 replaces those estimates with millisecond-accurate values derived
from the real audio duration (mutagen) and Whisper transcription timestamps.

A cut point is placed at a natural word boundary:
  - Preferred: a word that ends a sentence (ends with . ? ! …)
  - Fallback: the largest inter-word pause within a ±WINDOW around the target time
  - Last resort: the raw target time if no word is found in the window
"""

import logging

logger = logging.getLogger(__name__)

_MAX_SHORT_MS = 58_000   # target ≤ 58 seconds per Short segment
_WINDOW_MS    = 5_000    # ±5 s search window around each target cut point
_SENTENCE_END = {".", "?", "!", "…", "...", '"', "'"}


def recalculate_breakpoints(
    whisper_transcript: list[dict],
    duration_ms: int,
    shorts_rule: str,
) -> list[int]:
    """Compute Shorts breakpoints from real audio timing.

    Uses Whisper word-level timestamps to place each cut at a natural pause
    or sentence boundary, within ±5 seconds of each 58-second target.

    Args:
        whisper_transcript: Word-timing list from ``whisper.transcribe()``.
                            Each entry: ``{"word": str, "start": float, "end": float}``.
        duration_ms:        Exact audio duration from ``storage.save_audio()``.
        shorts_rule:        Channel config value: ``"always" | "auto" | "never"``.

    Returns:
        List of millisecond offsets (from audio start) where each Short ends.
        Empty list when ``shorts_rule = "never"`` or audio fits in one Short.
    """
    if shorts_rule == "never":
        return []

    if duration_ms <= _MAX_SHORT_MS:
        return []   # whole audio fits in a single Short

    if not whisper_transcript:
        # No timestamps — fall back to equal-interval splits
        logger.warning("No Whisper transcript — using equal-interval breakpoints")
        return _equal_splits(duration_ms)

    breakpoints: list[int] = []
    segment_start_ms = 0

    while True:
        target_ms = segment_start_ms + _MAX_SHORT_MS
        if target_ms >= duration_ms:
            break   # remaining segment fits within one Short

        cut_ms = _find_best_cut(whisper_transcript, target_ms, segment_start_ms)
        if cut_ms <= segment_start_ms:
            # Safety: no valid cut found — force progress to avoid infinite loop
            cut_ms = target_ms
        breakpoints.append(cut_ms)
        segment_start_ms = cut_ms

    logger.info(
        "Breakpoints recalculated: %d cut(s) for %.1fs audio",
        len(breakpoints), duration_ms / 1000,
    )
    return breakpoints


def _find_best_cut(words: list[dict], target_ms: int, segment_start_ms: int) -> int:
    """Return the best word-boundary millisecond offset near ``target_ms``.

    Search window: [target_ms - WINDOW, target_ms + WINDOW].
    Scoring (higher = better):
      +500  if the word ends a sentence (., ?, !, …)
      +gap  inter-word pause (ms, capped at 300) — natural breath/pause
      -dist distance from target_ms ÷ 10 — prefer cuts close to the target

    Returns the raw ``target_ms`` if no word is found in the window.
    """
    target_sec = target_ms / 1000
    window_sec = _WINDOW_MS / 1000

    lo = target_sec - window_sec
    hi = target_sec + window_sec

    candidates = []
    for i, w in enumerate(words):
        end_sec = float(w["end"])
        if end_sec < lo or end_sec > hi:
            continue
        if end_sec * 1000 <= segment_start_ms:
            continue   # must be after the current segment start

        # Gap to the next word (inter-word silence in ms)
        gap_ms = 0
        if i + 1 < len(words):
            gap_ms = max(0, int((float(words[i + 1]["start"]) - end_sec) * 1000))

        word_text = w["word"].strip()
        is_sentence_end = any(word_text.endswith(ch) for ch in _SENTENCE_END)

        candidates.append({
            "end_ms":          int(end_sec * 1000),
            "gap_ms":          gap_ms,
            "is_sentence_end": is_sentence_end,
            "distance_ms":     abs(int(end_sec * 1000) - target_ms),
        })

    if not candidates:
        return target_ms   # fallback — no word found in window

    best = max(
        candidates,
        key=lambda c: (
            (500 if c["is_sentence_end"] else 0)
            + min(c["gap_ms"], 300)
            - c["distance_ms"] // 10
        ),
    )
    return best["end_ms"]


def _equal_splits(duration_ms: int) -> list[int]:
    """Fallback: equal-interval splits when no Whisper data is available."""
    n = max(2, int(duration_ms / _MAX_SHORT_MS) + 1)
    step = duration_ms // n
    return [step * i for i in range(1, n)]
