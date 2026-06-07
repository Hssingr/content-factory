"""Storyboard Agent — Claude designs visual beats; Python maps them to audio timestamps.

Replaces the legacy ``section_splitter -> enrich_sections_with_visuals ->
validate_sections`` flow when storyboard generation succeeds. Claude makes every
creative decision (visual intent, visual type, search queries, effects, color
grades, transitions, overlays); Python only does the deterministic work — locating
each beat's narration span in the real Whisper transcript and converting it into
millisecond timestamps.

Fallback chain (the pipeline never breaks):
  storyboard generation fails / returns no beats → caller falls back to section_splitter
  a beat's start_hint/end_hint can't be located    → proportional timing + logged warning
"""

import logging
import re

from app.agents.agent5_video.system_prompt import generate_storyboard

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-zà-öø-ÿ0-9']+", re.IGNORECASE)

# Enum sets — Python enforces, never trusts Claude's strings blindly
_VALID_EFFECTS           = {"slow_zoom", "zoom_out", "pan", "push_in", "shake", "cut", "fade_in", "parallax"}
_VALID_GRADES            = {"desaturated", "cold_blue", "warm_amber", "dark_contrast", "neutral"}
_VALID_TRANSITIONS       = {"cut", "crossfade", "dip_to_black", "whip_pan", "zoom_blur", "match_cut", "none"}
_VALID_OVERLAY_POSITIONS = {"center", "lower_third", "top_left", "top_right", "none"}
_VALID_VISUAL_TYPES      = {"b-roll", "action", "text_overlay", "document", "map", "screenshot", "generated_visual"}
_VALID_PRIORITIES        = {"essential", "optional"}
_VALID_VISUAL_CATEGORIES = {"person", "place", "object", "document", "screen", "map", "abstract", "text"}

_DEFAULT_EFFECT           = "slow_zoom"
_DEFAULT_GRADE            = "desaturated"
_DEFAULT_TRANSITION       = "cut"
_DEFAULT_OVERLAY_POSITION = "none"
_DEFAULT_VISUAL_TYPE      = "b-roll"
_DEFAULT_PRIORITY         = "essential"
_DEFAULT_VISUAL_CATEGORY  = "place"

# Phrase-locating prefix lengths, longest first — tolerates Whisper transcription drift
_PREFIX_LENGTHS = (None, 5, 3)


def split_into_beats(
    voice_script: str,
    duration_ms: int,
    channel,
    script_format: str,
    whisper_transcript: list[dict],
) -> list[dict] | None:
    """Generate a storyboard with Claude and map its beats onto real audio timestamps.

    Args:
        voice_script:       Narrator text with [INTRO]/[SECTION N]/[OUTRO] markers.
        duration_ms:        Exact audio duration in milliseconds.
        channel:            Channel ORM object (provides niche/tone for the prompt).
        script_format:      Format key from ``channel_config.script_format``.
        whisper_transcript: Word-level timestamps (``[{"word", "start", "end"}]``, seconds).

    Returns:
        List of renderable beat-section dicts, or ``None`` if storyboard generation
        failed or returned no usable beats — signalling the caller to fall back to
        the legacy section splitter.
    """
    if not whisper_transcript:
        logger.warning("No Whisper transcript available — cannot build a storyboard")
        return None

    try:
        storyboard = generate_storyboard(voice_script, channel, script_format=script_format)
    except Exception as exc:
        logger.error("Storyboard generation failed: %s", exc)
        return None

    beats = storyboard.get("beats") or []
    if not beats:
        logger.warning("Storyboard returned no beats — falling back to legacy section splitter")
        return None

    mapped = map_storyboard_beats_to_timestamps(beats, whisper_transcript, duration_ms)
    logger.info(
        "Storyboard generated: %d beat(s), style=%r",
        len(mapped), storyboard.get("overall_style", ""),
    )
    return mapped


# ── Beat → timestamp mapping ──────────────────────────────────────────────────

def map_storyboard_beats_to_timestamps(
    beats: list[dict],
    whisper_transcript: list[dict],
    duration_ms: int,
) -> list[dict]:
    """Map each storyboard beat onto real audio timestamps using Whisper words.

    Locates each beat's ``start_hint``/``end_hint`` phrase in the Whisper word
    list (forward-only search, fuzzy prefix matching to tolerate transcription
    drift). Beats whose hints cannot be located fall back to proportional timing,
    interpolated between their nearest successfully matched neighbours.

    Args:
        beats:              Raw beat dicts from ``generate_storyboard``.
        whisper_transcript: Word-level timestamps (``[{"word", "start", "end"}]``, seconds).
        duration_ms:        Exact audio duration in milliseconds.

    Returns:
        List of renderable beat-section dicts (one per input beat, in order), each
        with: beat_order, section_order, section_marker, audio_start_ms, audio_end_ms,
        duration_sec, script_text, visual_intent, visual_type, visual_category,
        avoid_reason, search_query, fallback_query, effect, color_grade,
        transition_to_next, overlay_text, overlay_position, priority.
    """
    flat = _flatten_transcript(whisper_transcript)
    beats = _normalize_beat_order(beats)
    n = len(beats)

    matches: list[tuple[int, int] | None] = [None] * n
    failed_orders: list = []
    cursor = 0

    for i, beat in enumerate(beats):
        located = _locate_beat_span(
            flat, cursor, str(beat.get("start_hint", "")), str(beat.get("end_hint", ""))
        )
        if located is None:
            failed_orders.append(beat.get("beat_order", i))
            continue
        matches[i] = located
        cursor = located[1] + 1

    if failed_orders:
        logger.warning(
            "Storyboard timestamp mapping: %d beat(s) used proportional fallback — orders=%s",
            len(failed_orders), failed_orders,
        )

    boundaries = _resolve_boundaries(matches, beats, flat, duration_ms)

    sections: list[dict] = []
    for i, beat in enumerate(beats):
        start_ms, end_ms = boundaries[i]
        match = matches[i]
        script_text = (
            _join_words(flat, match[0], match[1])
            if match is not None
            else str(beat.get("visual_intent", "")).strip()
        )
        sections.append(_build_beat_section(beat, i, start_ms, end_ms, script_text))

    return sections


def _flatten_transcript(whisper_transcript: list[dict]) -> list[tuple[str, str, int, int]]:
    """Normalize whisper words into ``(norm_token, original_word, start_ms, end_ms)`` tuples."""
    flat = []
    for w in whisper_transcript:
        word = str(w.get("word", ""))
        norm = _normalize_word(word)
        if not norm:
            continue
        flat.append((
            norm,
            word,
            int(float(w.get("start", 0)) * 1000),
            int(float(w.get("end", 0)) * 1000),
        ))
    return flat


def _normalize_word(word: str) -> str:
    """Collapse a word to a single lowercase alphanumeric token for matching."""
    return "".join(_WORD_RE.findall(word.lower()))


def _normalize_phrase(phrase: str) -> list[str]:
    """Split a phrase into normalized tokens, dropping punctuation-only words."""
    return [t for t in (_normalize_word(w) for w in phrase.split()) if t]


def _locate_beat_span(
    flat: list[tuple[str, str, int, int]],
    cursor: int,
    start_hint: str,
    end_hint: str,
) -> tuple[int, int] | None:
    """Locate a beat's inclusive ``[start_idx, end_idx]`` token span in the transcript."""
    start_match = _locate_phrase(flat, cursor, start_hint)
    if start_match is None:
        return None
    start_idx = start_match[0]

    end_match = _locate_phrase(flat, start_idx, end_hint)
    if end_match is None:
        return (start_idx, start_match[1])   # minimal span — just the start phrase

    return (start_idx, max(end_match[1], start_idx))


def _locate_phrase(
    flat: list[tuple[str, str, int, int]],
    from_idx: int,
    phrase: str,
) -> tuple[int, int] | None:
    """Find a phrase forward from ``from_idx``, trying shrinking prefixes for fuzzy tolerance.

    Returns:
        Inclusive ``(start_idx, end_idx)`` token-index span, or ``None`` if not found.
    """
    tokens = _normalize_phrase(phrase)
    if not tokens:
        return None

    for prefix_len in _PREFIX_LENGTHS:
        candidate = tokens if prefix_len is None else tokens[:prefix_len]
        if not candidate:
            continue
        found = _search_subsequence(flat, from_idx, candidate)
        if found is not None:
            start_idx, matched_len = found
            return (start_idx, start_idx + matched_len - 1)

    return None


def _search_subsequence(
    flat: list[tuple[str, str, int, int]],
    from_idx: int,
    tokens: list[str],
) -> tuple[int, int] | None:
    """Find the first forward contiguous occurrence of ``tokens``.

    Returns:
        ``(start_idx, length)`` of the match, or ``None``.
    """
    limit = len(flat) - len(tokens) + 1
    for i in range(max(from_idx, 0), max(limit, 0)):
        if all(flat[i + j][0] == tokens[j] for j in range(len(tokens))):
            return (i, len(tokens))
    return None


def _join_words(flat: list[tuple[str, str, int, int]], start_idx: int, end_idx: int) -> str:
    return " ".join(flat[i][1] for i in range(start_idx, end_idx + 1))


def _resolve_boundaries(
    matches: list[tuple[int, int] | None],
    beats: list[dict],
    flat: list[tuple[str, str, int, int]],
    duration_ms: int,
) -> list[tuple[int, int]]:
    """Turn matched/unmatched beat spans into a monotonic, bounds-clean ms timeline.

    Matched beats anchor their start to the matched phrase's start_ms. Unmatched
    runs are filled proportionally — by ``duration_target_sec`` weight — between
    their matched neighbours (or the timeline edges). A final pass enforces
    monotonic, non-overlapping boundaries within ``[0, duration_ms]`` and snaps
    the last beat to end exactly at ``duration_ms``.
    """
    n = len(beats)
    anchors: list[int | None] = [
        flat[match[0]][2] if match is not None else None
        for match in matches
    ]

    _fill_gaps(anchors, beats, duration_ms)

    starts = [0] * n
    prev = 0
    for i in range(n):
        candidate = anchors[i] if anchors[i] is not None else prev
        starts[i] = max(candidate, prev)
        prev = starts[i]

    boundaries: list[tuple[int, int]] = []
    for i in range(n):
        end_ms = starts[i + 1] if i + 1 < n else duration_ms
        boundaries.append((starts[i], max(end_ms, starts[i])))

    if boundaries:
        last_start, _ = boundaries[-1]
        boundaries[-1] = (last_start, max(duration_ms, last_start))

    return boundaries


def _fill_gaps(anchors: list[int | None], beats: list[dict], duration_ms: int) -> None:
    """Interpolate start_ms for unmatched beats between their matched neighbours.

    Distributes the span between two known anchors proportionally to each
    unmatched beat's ``duration_target_sec`` weight. Edge runs fall back to
    ``0`` / ``duration_ms`` as their bounding anchors.
    """
    n = len(anchors)
    i = 0
    while i < n:
        if anchors[i] is not None:
            i += 1
            continue

        j = i
        while j < n and anchors[j] is None:
            j += 1

        left_ms  = anchors[i - 1] if i > 0 else 0
        right_ms = anchors[j] if j < n else duration_ms
        span = max(right_ms - left_ms, 0)

        run = list(range(i, j))
        weights = [max(float(beats[k].get("duration_target_sec") or 3), 0.5) for k in run]
        total_weight = sum(weights) or float(len(run))

        cumulative = 0.0
        for offset, k in enumerate(run):
            anchors[k] = int(left_ms + span * (cumulative / total_weight))
            cumulative += weights[offset]

        i = j


def _build_beat_section(beat: dict, index: int, start_ms: int, end_ms: int, script_text: str) -> dict:
    """Build a renderable beat-section dict from a raw beat + resolved timestamps.

    ``section_order`` mirrors ``beat_order`` so the existing pipeline stages
    (persistence, stock fetcher, assembly validator, shorts cutter, Remotion
    builder) keep working unchanged on storyboard beats.
    """
    beat_order = beat.get("beat_order", index)
    return {
        "beat_order": beat_order,
        "section_order": beat_order,
        "section_marker": str(beat.get("section_marker", "")),
        "audio_start_ms": start_ms,
        "audio_end_ms": end_ms,
        "duration_sec": max(end_ms - start_ms, 0) / 1000,
        "script_text": script_text,
        "visual_intent": str(beat.get("visual_intent", "")),
        "visual_type": _safe_enum(beat.get("visual_type"), _VALID_VISUAL_TYPES, _DEFAULT_VISUAL_TYPE),
        "visual_category": _safe_enum(beat.get("visual_category"), _VALID_VISUAL_CATEGORIES, _DEFAULT_VISUAL_CATEGORY),
        "avoid_reason": str(beat.get("avoid_reason", "") or ""),
        "search_query": str(beat.get("search_query", "")),
        "fallback_query": str(beat.get("fallback_query", "")),
        "effect": _safe_enum(beat.get("effect"), _VALID_EFFECTS, _DEFAULT_EFFECT),
        "color_grade": _safe_enum(beat.get("color_grade"), _VALID_GRADES, _DEFAULT_GRADE),
        "transition_to_next": _safe_enum(beat.get("transition_to_next"), _VALID_TRANSITIONS, _DEFAULT_TRANSITION),
        "overlay_text": str(beat.get("overlay_text", "") or ""),
        "overlay_position": _safe_enum(beat.get("overlay_position"), _VALID_OVERLAY_POSITIONS, _DEFAULT_OVERLAY_POSITION),
        "priority": _safe_enum(beat.get("priority"), _VALID_PRIORITIES, _DEFAULT_PRIORITY),
    }


def _normalize_beat_order(beats: list[dict]) -> list[dict]:
    """Sort beats by ``beat_order`` and renumber duplicates/non-numeric values.

    Claude is instructed to return sequential integers starting at 0, but the
    value is untrusted input — duplicates or out-of-order values would otherwise
    corrupt the forward-cursor mapping and collide on the unconstrained
    ``video_sections.section_order`` column.
    """
    ordered = sorted(enumerate(beats), key=lambda pair: _coerce_int(pair[1].get("beat_order"), pair[0]))

    seen: set[int] = set()
    normalized: list[dict] = []
    for new_order, (original_index, beat) in enumerate(ordered):
        order = _coerce_int(beat.get("beat_order"), original_index)
        if order in seen:
            replacement = new_order
            while replacement in seen:
                replacement += 1
            logger.warning("Duplicate beat_order=%d from Claude — renumbering to %d", order, replacement)
            order = replacement
        seen.add(order)
        normalized.append({**beat, "beat_order": order})

    return normalized


def _coerce_int(value, default: int) -> int:
    """Return ``value`` as an int when it parses cleanly, otherwise ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_enum(value, valid: set, default: str) -> str:
    """Return ``value`` lower-cased if it's a recognized enum member, otherwise ``default``."""
    if isinstance(value, str) and value.strip().lower() in valid:
        return value.strip().lower()
    return default
