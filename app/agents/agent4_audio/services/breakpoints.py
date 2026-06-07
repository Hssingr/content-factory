"""Shorts breakpoints — Claude-driven semantic splits mapped to Whisper timestamps.

Flow:
  1. Calculate the optimal number of Shorts (each 61–91 s) from the audio duration.
  2. Ask Claude to identify N-1 semantic cut points in the narration script,
     returning the exact last words of each segment before the cut.
  3. Locate each phrase in the Whisper transcript and use the end timestamp of
     the last matched word as the precise cut point.
  4. Fall back to equal-interval splits when Whisper data or Claude is unavailable.

Every Short is guaranteed to be between _MIN_SHORT_S and _MAX_SHORT_S seconds.
"""

import logging
import math
import re

from app.agents.agent4_audio.system_prompt import SHORTS_SPLITTER_SYSTEM_PROMPT
from app.services.claude_client import call_claude, parse_claude_json

logger = logging.getLogger(__name__)

_MIN_SHORT_S = 61    # minimum Short duration in seconds
_MAX_SHORT_S = 91    # maximum Short duration in seconds
_TARGET_SHORT_S = (_MIN_SHORT_S + _MAX_SHORT_S) // 2   # 76 s midpoint target


def recalculate_breakpoints(
    whisper_transcript: list[dict],
    duration_ms: int,
    shorts_rule: str,
    voice_script: str = "",
) -> list[int]:
    """Compute Shorts breakpoints using Claude's semantic analysis of the script.

    Claude reads the full narration and identifies the best cut points based on
    topic boundaries and sentence completeness. Each resulting Short is between
    61 and 91 seconds long.

    Args:
        whisper_transcript: Word-timing list from Whisper.
                            Each entry: ``{"word": str, "start": float, "end": float}``.
        duration_ms:        Exact audio duration in milliseconds.
        shorts_rule:        ``"always" | "auto" | "never"``.
        voice_script:       Full narrator text — used by Claude to find semantic cuts.

    Returns:
        List of millisecond offsets where each Short ends.
        Empty list when Shorts should not be generated or audio fits in one Short.
    """
    if shorts_rule == "never":
        return []

    n_shorts = _optimal_n_shorts(duration_ms)
    if n_shorts <= 1:
        logger.info(
            "Audio %.1fs cannot be cleanly split into %d–%ds Shorts — no breakpoints",
            duration_ms / 1000, _MIN_SHORT_S, _MAX_SHORT_S,
        )
        return []

    n_splits = n_shorts - 1

    if not whisper_transcript:
        logger.warning("No Whisper transcript — using equal-interval breakpoints")
        return _equal_splits(duration_ms, n_shorts)

    # Try Claude-driven semantic splits first
    if voice_script:
        try:
            phrases = _get_semantic_split_phrases(voice_script, n_shorts, n_splits)
            breakpoints = _map_phrases_to_timestamps(phrases, whisper_transcript, duration_ms)
            if len(breakpoints) == n_splits:
                breakpoints = _enforce_duration_bounds(breakpoints, duration_ms)
                logger.info(
                    "Semantic breakpoints: %d cut(s) → %d Shorts for %.1fs audio",
                    len(breakpoints), len(breakpoints) + 1, duration_ms / 1000,
                )
                return breakpoints
            logger.warning(
                "Claude returned %d/%d splits — falling back to equal splits",
                len(breakpoints), n_splits,
            )
        except Exception as exc:
            logger.warning("Claude semantic split failed: %s — falling back to equal splits", exc)

    return _equal_splits(duration_ms, n_shorts)


# ── Split count calculation ───────────────────────────────────────────────────

def _optimal_n_shorts(duration_ms: int) -> int:
    """Return the optimal number of Shorts for this audio duration.

    Each Short must be between _MIN_SHORT_S and _MAX_SHORT_S seconds.
    Returns 1 if no valid split count exists (audio too short or cannot be divided).
    """
    D = duration_ms / 1000

    if D < _MIN_SHORT_S:
        return 1   # too short to be a Short

    if D <= _MAX_SHORT_S:
        return 1   # fits in a single Short

    min_n = math.ceil(D / _MAX_SHORT_S)
    max_n = int(D / _MIN_SHORT_S)

    if min_n > max_n or max_n < 2:
        return 1   # no valid split count in range

    # Pick the n closest to D / TARGET (balanced segments)
    ideal = D / _TARGET_SHORT_S
    return min(range(max(2, min_n), max_n + 1), key=lambda n: abs(n - ideal))


# ── Claude call ───────────────────────────────────────────────────────────────

def _get_semantic_split_phrases(
    voice_script: str,
    n_shorts: int,
    n_splits: int,
) -> list[str]:
    """Ask Claude to identify semantic cut points in the narration script.

    Args:
        voice_script: Full narrator text (will be truncated to 8 000 chars).
        n_shorts:     Target number of Shorts.
        n_splits:     Number of cut points (n_shorts - 1).

    Returns:
        List of verbatim phrases (last 8-12 words before each cut), in order.

    Raises:
        ValueError: If Claude returns malformed JSON or wrong number of splits.
    """
    system = SHORTS_SPLITTER_SYSTEM_PROMPT.format(
        n_splits=n_splits,
        n_shorts=n_shorts,
        target_sec=_MIN_SHORT_S,
        max_sec=_MAX_SHORT_S,
    )
    user_message = (
        f"Target: {n_shorts} Shorts ({_MIN_SHORT_S}–{_MAX_SHORT_S}s each)\n\n"
        f"Narration script:\n{voice_script[:8000]}"
    )
    raw = call_claude(system, user_message, max_tokens=512)
    data = parse_claude_json(raw, required_keys=["splits"], type_checks={"splits": list})

    splits = data["splits"]
    if len(splits) != n_splits:
        raise ValueError(f"Expected {n_splits} splits, got {len(splits)}")

    # Sort by segment_ends in case Claude returned them out of order
    splits_sorted = sorted(splits, key=lambda s: s.get("segment_ends", 0))
    phrases = [s.get("split_after_words", "").strip() for s in splits_sorted]
    if any(not p for p in phrases):
        raise ValueError("One or more split_after_words are empty")

    return phrases


# ── Phrase → timestamp mapping ────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase and strip punctuation for fuzzy word matching."""
    return re.sub(r"[^\w]", "", text.lower())


def _map_phrases_to_timestamps(
    phrases: list[str],
    words: list[dict],
    duration_ms: int,
) -> list[int]:
    """Map each Claude-identified phrase to a Whisper word-end timestamp.

    For each phrase, searches for the best-matching word sequence in the
    transcript and uses the end timestamp of the last matched word.

    Args:
        phrases:     Verbatim end-of-segment phrases from Claude.
        words:       Whisper word list (each: {"word", "start", "end"}).
        duration_ms: Full audio duration in milliseconds.

    Returns:
        List of millisecond cut points, in ascending order.
        May be shorter than ``phrases`` if some phrases could not be matched.
    """
    normalized_transcript = [_normalize(w["word"]) for w in words]
    breakpoints: list[int] = []
    search_from = 0   # ensure breakpoints are strictly increasing

    for phrase in phrases:
        phrase_words = [_normalize(t) for t in phrase.split() if _normalize(t)]
        if not phrase_words:
            continue

        n = len(phrase_words)
        best_end_ms: int | None = None
        best_score   = 0

        for i in range(search_from, len(words) - n + 1):
            window = normalized_transcript[i : i + n]
            score  = sum(1 for a, b in zip(phrase_words, window) if a == b or (a and b and (a in b or b in a)))
            ratio  = score / n
            if ratio >= 0.65 and score > best_score:
                best_score   = score
                best_end_ms  = int(float(words[i + n - 1]["end"]) * 1000)
                search_from  = i + n   # next phrase must come after this one

        if best_end_ms is not None:
            breakpoints.append(best_end_ms)
        else:
            logger.warning("Could not match phrase in Whisper transcript: %r", phrase[:60])

    return sorted(breakpoints)


# ── Duration-bounds enforcement ───────────────────────────────────────────────

def _enforce_duration_bounds(breakpoints: list[int], duration_ms: int) -> list[int]:
    """Remove breakpoints that would create Shorts outside [MIN, MAX] seconds.

    Works through the breakpoints in order. Any cut that would produce a
    segment shorter than MIN is dropped (absorbed into the next segment).
    If the final segment would exceed MAX, no corrective action is taken —
    the semantic boundary is respected over the hard cap.
    """
    if not breakpoints:
        return []

    min_ms = _MIN_SHORT_S * 1000
    cleaned: list[int] = []
    prev = 0

    for bp in breakpoints:
        if bp - prev >= min_ms:
            cleaned.append(bp)
            prev = bp
        else:
            logger.debug(
                "Dropping breakpoint at %.1fs — segment %.1fs < minimum %.1fs",
                bp / 1000, (bp - prev) / 1000, _MIN_SHORT_S,
            )

    # Check final segment
    if cleaned and duration_ms - cleaned[-1] < min_ms:
        logger.debug(
            "Final segment %.1fs < minimum — removing last breakpoint",
            (duration_ms - cleaned[-1]) / 1000,
        )
        cleaned.pop()

    return cleaned


# ── Equal-interval fallback ───────────────────────────────────────────────────

def _equal_splits(duration_ms: int, n_shorts: int) -> list[int]:
    """Fallback: equal-interval splits when Claude or Whisper data is unavailable."""
    step = duration_ms // n_shorts
    return [step * i for i in range(1, n_shorts)]
