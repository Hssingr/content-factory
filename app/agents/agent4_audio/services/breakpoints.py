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

from app.agents.agent4_audio.system_prompt import SHORTS_SPLITTER_SYSTEM_PROMPT
from app.services.claude_client import call_claude_structured
from app.shared.text_normalize import normalize_for_matching

logger = logging.getLogger(__name__)

_MIN_SHORT_S = 61    # minimum Short duration in seconds
_MAX_SHORT_S = 91    # maximum Short duration in seconds
_TARGET_SHORT_S = (_MIN_SHORT_S + _MAX_SHORT_S) // 2   # 76 s midpoint target


def recalculate_breakpoints(
    whisper_transcript: list[dict],
    duration_ms: int,
    shorts_rule: str,
    voice_script: str = "",
    language: str = "en",
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
        language:           BCP-47 language code — used by ``normalize_for_matching``
                            to expand digit runs before phrase matching.

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
            breakpoints = _map_phrases_to_timestamps(phrases, whisper_transcript, duration_ms, language)
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

_SPLITS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "splits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "split_after_words": {
                        "type": "string",
                        "description": "Exact verbatim last 8-12 words of the sentence before this cut.",
                    },
                    "segment_ends": {
                        "type": "integer",
                        "description": "0-based index of the segment that ends at this cut.",
                    },
                },
                "required": ["split_after_words", "segment_ends"],
            },
        },
    },
    "required": ["splits"],
}


def _get_semantic_split_phrases(
    voice_script: str,
    n_shorts: int,
    n_splits: int,
) -> list[str]:
    """Ask Claude to identify semantic cut points in the narration script.

    Uses forced tool-use (call_claude_structured) to guarantee structured JSON
    output without relying on text parsing or code-fence stripping.

    Args:
        voice_script: Full narrator text (will be truncated to 8 000 chars).
        n_shorts:     Target number of Shorts.
        n_splits:     Number of cut points (n_shorts - 1).

    Returns:
        List of verbatim phrases (last 8-12 words before each cut), in order.

    Raises:
        ValueError: If Claude returns wrong number of splits or empty phrases.
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
    data = call_claude_structured(
        task="semantic_splits",
        system_prompt=system,
        user_message=user_message,
        schema_name="semantic_splits",
        input_schema=_SPLITS_SCHEMA,
        max_tokens=512,
    )

    splits = data.get("splits", [])
    if len(splits) != n_splits:
        raise ValueError(f"Expected {n_splits} splits, got {len(splits)}")

    splits_sorted = sorted(splits, key=lambda s: s.get("segment_ends", 0))
    phrases = [s.get("split_after_words", "").strip() for s in splits_sorted]
    if any(not p for p in phrases):
        raise ValueError("One or more split_after_words are empty")

    return phrases


# ── Phrase → timestamp mapping ────────────────────────────────────────────────

def _map_phrases_to_timestamps(
    phrases: list[str],
    words: list[dict],
    duration_ms: int,
    language: str = "en",
) -> list[int]:
    """Map each Claude-identified phrase to a Whisper word-end timestamp.

    Uses ``normalize_for_matching`` on both sides so digit-form numbers in
    Claude's phrase (e.g. "1984") match the spoken-form words in the Whisper
    transcript (e.g. "nineteen eighty four").

    For each phrase, searches for the best-matching word sequence in the
    transcript and uses the end timestamp of the last matched word.

    Args:
        phrases:     Verbatim end-of-segment phrases from Claude.
        words:       Whisper word list (each: {"word", "start", "end"}).
        duration_ms: Full audio duration in milliseconds.
        language:    BCP-47 language code — passed to ``normalize_for_matching``.

    Returns:
        List of millisecond cut points, in ascending order.
        May be shorter than ``phrases`` if some phrases could not be matched.
    """
    # Normalize each Whisper word in isolation (no digit expansion needed for
    # individual spoken words, but punctuation / case must match)
    normalized_transcript = [
        normalize_for_matching(w["word"], language)
        for w in words
    ]
    # Flatten to single strings for window comparison
    flat_transcript = ["".join(toks) for toks in normalized_transcript]

    breakpoints: list[int] = []
    search_from = 0   # ensure breakpoints are strictly increasing

    for phrase in phrases:
        # Normalize the full phrase — expands any digit runs to spoken form
        phrase_tokens = normalize_for_matching(phrase, language)
        if not phrase_tokens:
            continue

        n = len(phrase_tokens)
        best_end_ms: int | None = None
        best_score   = 0

        # The phrase may expand to more tokens than there are Whisper words (e.g.
        # "1984" → 3 tokens but was 1 Whisper word).  We therefore iterate over
        # Whisper word positions; for each starting position i, we walk j forward
        # collecting flat tokens until we have n of them.  The score is then the
        # fraction of phrase_tokens that match the collected window.
        for i in range(search_from, len(words)):
            window_tokens: list[str] = []
            j = i
            while len(window_tokens) < n and j < len(flat_transcript):
                window_tokens.append(flat_transcript[j])
                j += 1

            if len(window_tokens) < n:
                break   # not enough tokens left in transcript

            score = sum(
                1 for a, b in zip(phrase_tokens, window_tokens)
                if a == b or (a and b and (a in b or b in a))
            )
            ratio = score / n
            if ratio >= 0.65 and score > best_score:
                best_score  = score
                best_end_ms = int(float(words[j - 1]["end"]) * 1000)
                search_from = j   # next phrase must come after this one

        if best_end_ms is not None:
            breakpoints.append(best_end_ms)
        else:
            logger.warning("Could not match phrase in Whisper transcript: %r", phrase[:60])

    return sorted(breakpoints)


# ── Duration-bounds enforcement ───────────────────────────────────────────────

def _enforce_duration_bounds(breakpoints: list[int], duration_ms: int) -> list[int]:
    """Remove or split breakpoints so all Shorts fall within [MIN, MAX] seconds.

    Pass 1 (floor): Drop any cut that would produce a segment shorter than MIN
    (absorbed into the next segment). Also removes the final breakpoint if the
    tail would be under-floor.

    Pass 2 (ceiling): For any surviving segment that exceeds MAX, insert equal-
    interval bisection cuts. The number of pieces is ceil(seg_dur / MAX). If the
    resulting piece size would be below MIN (e.g. a 115s segment that can only
    produce two 57.5s pieces), the over-cap segment is left unchanged and a
    WARNING is logged — violating the floor is worse than a single long Short.
    """
    if not breakpoints:
        return []

    min_ms = _MIN_SHORT_S * 1000
    max_ms = _MAX_SHORT_S * 1000

    # ── Pass 1: floor enforcement ─────────────────────────────────────────────
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

    if cleaned and duration_ms - cleaned[-1] < min_ms:
        logger.debug(
            "Final segment %.1fs < minimum — removing last breakpoint",
            (duration_ms - cleaned[-1]) / 1000,
        )
        cleaned.pop()

    # ── Pass 2: ceiling enforcement ───────────────────────────────────────────
    final: list[int] = []
    segments = list(zip([0] + cleaned, cleaned + [duration_ms]))

    for seg_start, seg_end in segments:
        seg_dur = seg_end - seg_start
        is_last_seg = (seg_end == duration_ms)

        if seg_dur > max_ms:
            n_pieces = math.ceil(seg_dur / max_ms)
            piece_ms = seg_dur // n_pieces
            if piece_ms >= min_ms:
                for k in range(1, n_pieces):
                    cut = seg_start + piece_ms * k
                    final.append(cut)
                    logger.info(
                        "Over-cap bisect: %.1fs → %d pieces of %.1fs — added cut at %.1fs",
                        seg_dur / 1000, n_pieces, piece_ms / 1000, cut / 1000,
                    )
            else:
                logger.warning(
                    "Over-cap segment %.1fs cannot be split cleanly into ≥%.1fs pieces "
                    "(would give %.1fs/piece) — keeping as-is",
                    seg_dur / 1000, _MIN_SHORT_S, piece_ms / 1000,
                )

        if not is_last_seg:
            final.append(seg_end)

    return final


# ── Equal-interval fallback ───────────────────────────────────────────────────

def _equal_splits(duration_ms: int, n_shorts: int) -> list[int]:
    """Fallback: equal-interval splits when Claude or Whisper data is unavailable."""
    step = duration_ms // n_shorts
    return [step * i for i in range(1, n_shorts)]
