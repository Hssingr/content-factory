"""Storyboard Agent — Claude designs visual beats; Python maps them to audio timestamps.

Replaces the legacy ``section_splitter -> enrich_sections_with_visuals ->
validate_sections`` flow when storyboard generation succeeds. Claude makes every
creative decision (visual intent, visual type, search queries, effects, color
grades, transitions, overlays); Python only does the deterministic work — splitting
the narration into segments, batching the Claude calls, merging the results, locating
each beat's narration span in the real Whisper transcript, and converting it into
millisecond timestamps.

Generation is BATCHED per narration segment ([INTRO] / [SECTION N] / [OUTRO]) rather
than requested as one whole-video call — a 900-1200 word youtube_long script needs
~90-120 beats at the prompt's own pacing rule (1 beat / 3-5s), which serializes to
~20,000-27,000 tokens at the full schema and structurally cannot fit inside a single
~8192-token response (this was the root cause of 100% storyboard failures: Claude hit
max_tokens mid-beat, producing "Unterminated string" JSON errors). Splitting generation
per segment keeps each call to ~5-25 beats — comfortably inside any reasonable ceiling.

Fail-loud chain (per Recommended Fix BLOCKER #2 — no silent quality degradation):
  any segment batch fails / returns no beats        → entire storyboard generation fails
  storyboard generation fails                        → caller checks allow_legacy_fallback:
                                                          True  → fall back to section_splitter
                                                          False → stop language generation, explicit error
  a beat's start_hint/end_hint can't be located      → proportional timing + logged warning
"""

import logging
import re
import unicodedata

from app.agents.agent5_video.system_prompt import (
    STORYBOARD_BATCH_MAX_TOKENS as _STORYBOARD_BATCH_MAX_TOKENS_LOG,
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION_LOG,
    generate_storyboard_batch,
)
from app.shared.text_normalize import normalize_for_matching as _normalize_for_matching

logger = logging.getLogger(__name__)

# Apostrophes and typographic variants — all treated as word-boundary separators
# so that French elisions (l'entreprise → ["l","entreprise"]) tokenize the same
# way Whisper splits them, while English contractions (hadn't → ["hadn","t"])
# also become consistent two-token forms on both hint and transcript sides.
_APOSTROPHE_RE = re.compile(r"['’ʼʻ‘]")
# Token pattern — apostrophe removed; we expand it to spaces before matching
_WORD_RE = re.compile(r"[a-zÀ-ɏ0-9]+", re.IGNORECASE)

# Minimum guaranteed duration per beat after timestamp mapping.
# Prevents zero-width spans when consecutive beats anchor to the same Whisper
# word (or when beat 0 anchors at ms=0 and beat 1 also anchors at ms=0).
_MIN_BEAT_MS = 500

# Fallback-rate acceptance thresholds for the mapping quality gate.
# >30% → WARNING only; >50% → mapping failure (respects allow_legacy_fallback).
_FALLBACK_WARN_RATIO  = 0.30
_FALLBACK_FAIL_RATIO  = 0.50

# Enum sets — Python enforces, never trusts Claude's strings blindly
_VALID_EFFECTS           = {"slow_zoom", "zoom_out", "pan", "push_in", "shake", "cut", "fade_in", "parallax"}
_VALID_GRADES            = {"desaturated", "cold_blue", "warm_amber", "dark_contrast", "neutral"}
_VALID_TRANSITIONS       = {"cut", "crossfade", "dip_to_black", "whip_pan", "zoom_blur", "match_cut", "none"}
_VALID_OVERLAY_POSITIONS = {"center", "lower_third", "top_left", "top_right", "none"}
_VALID_VISUAL_TYPES      = {"b-roll", "action", "text_overlay", "document", "map", "screenshot", "generated_visual"}
_VALID_VISUAL_CATEGORIES = {"person", "place", "object", "document", "screen", "map", "abstract", "text"}
_VALID_ENVIRONMENTS      = {
    "underwater", "indoor_office", "indoor_domestic", "forest_nature", "urban_street",
    "corridor_interior", "abstract_dark", "open_landscape", "laboratory", "industrial",
    "vehicle", "other",
}
_VALID_MOTIFS            = {
    "doorway", "corridor", "face", "hands", "object", "clock", "phone", "photo",
    "exterior", "text", "screen", "reflection", "document", "room", "other",
}

_DEFAULT_EFFECT           = "slow_zoom"
_DEFAULT_GRADE            = "desaturated"
_DEFAULT_TRANSITION       = "cut"
_DEFAULT_OVERLAY_POSITION = "none"
_DEFAULT_VISUAL_TYPE      = "b-roll"
_DEFAULT_VISUAL_CATEGORY  = "place"
_DEFAULT_ENVIRONMENT      = "other"
_DEFAULT_MOTIF            = "other"

# Phrase-locating prefix lengths, longest first — tolerates Whisper transcription drift
_PREFIX_LENGTHS = (None, 5, 3)

# [INTRO] / [SECTION N] / [OUTRO] on their own line — same pattern used to strip
# markers before TTS (agent4_audio/services/tts.py) and before quality prompts
# (services/video.py _script_hook), kept in sync so segmentation matches exactly
# what the narrator actually speaks.
_SEGMENT_MARKER_RE = re.compile(
    r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Rough pacing used ONLY for the diagnostic "estimated beat count" log line —
# matches the rates stated in _STORYBOARD_SYSTEM_PROMPT ("== Pacing ==").
_BEAT_SECONDS_BY_FORMAT: dict[str, float] = {"youtube_long": 4.0}
_DEFAULT_BEAT_SECONDS = 3.0
_WORDS_PER_MINUTE = 150

# Diagnostic-only estimate of serialized size per beat at the reduced 13-field
# schema (~617 chars/beat ≈ 154 tokens/beat at the project's chars/4 heuristic —
# see Storyboard Size Analysis). Used solely to log an "estimated total output
# tokens" figure alongside the real ``total_output_tokens`` for comparison.
_STORYBOARD_TOKENS_PER_BEAT_LOG = 154


def split_into_beats(
    voice_script: str,
    duration_ms: int,
    channel,
    script_format: str,
    whisper_transcript: list[dict],
    allow_legacy_fallback: bool = False,
    language: str = "en",
) -> list[dict] | None:
    """Generate a storyboard with Claude (batched per segment) and map it onto real audio timestamps.

    Splits ``voice_script`` into [INTRO]/[SECTION N]/[OUTRO] segments, runs one
    storyboard batch per segment (so no single Claude call has to describe the
    whole video — see module docstring for why that was the root cause of the
    prior 100% failure rate), merges the batches in order, and maps the merged
    beats onto the real Whisper transcript.

    Args:
        voice_script:       Narrator text with [INTRO]/[SECTION N]/[OUTRO] markers.
        duration_ms:        Exact audio duration in milliseconds.
        channel:            Channel ORM object (provides niche/tone for the prompt).
        script_format:      Format key from ``channel_config.script_format``.
        whisper_transcript: Word-level timestamps (``[{"word", "start", "end"}]``, seconds).
        allow_legacy_fallback: Forwarded to ``map_storyboard_beats_to_timestamps`` to control
            what happens when > 50 % of beats use proportional fallback timing —
            ``False`` (default) treats that as a mapping failure and returns ``None``;
            ``True`` accepts the result regardless.
        language:           BCP-47 language code (e.g. "fr", "en") — used by
            ``normalize_for_matching`` for digit-to-word expansion in hint matching.

    Returns:
        List of renderable beat-section dicts, or ``None`` if storyboard generation
        failed or returned no usable beats — signalling the caller to apply its
        ``allow_legacy_fallback`` policy (fall back to the legacy section splitter,
        or stop language generation with an explicit error).
    """
    if not whisper_transcript:
        logger.warning("No Whisper transcript available — cannot build a storyboard")
        return None

    segments = _split_voice_script_into_segments(voice_script)
    if not segments:
        logger.warning("Storyboard: voice_script has no narration content — cannot build a storyboard")
        return None

    total_words = max(len(voice_script.split()), 1)
    beat_seconds = _BEAT_SECONDS_BY_FORMAT.get(script_format, _DEFAULT_BEAT_SECONDS)
    estimated_beats = _estimate_beat_count(voice_script, script_format)
    logger.info(
        "Storyboard generation start: schema_version=%s language=%s segments=%d "
        "estimated_beat_count=%d (estimated from %d words at ~%.0fs/beat)",
        _STORYBOARD_SCHEMA_VERSION_LOG, language, len(segments), estimated_beats,
        total_words, beat_seconds,
    )

    raw_batches: list[list[dict]] = []
    overall_style = ""
    previous_summary = ""
    total_output_tokens = 0

    # Cross-segment continuity ledger: tracks cumulative environment counts and
    # recent visual_types across all batches to give Claude global repetition context.
    ledger: dict = {"env_counts": {}, "recent_envs": [], "recent_visual_types": [], "total_beats": 0}

    for index, (label, text) in enumerate(segments, start=1):
        # Per-segment target beat count from proportional audio duration
        seg_words = max(len(text.split()), 1)
        seg_duration_sec = (seg_words / total_words) * (duration_ms / 1000)
        target_beat_count = max(1, round(seg_duration_sec / beat_seconds))

        try:
            storyboard, usage = generate_storyboard_batch(
                segment_label=label,
                segment_text=text,
                segment_index=index,
                segment_count=len(segments),
                channel=channel,
                script_format=script_format,
                previous_segment_summary=previous_summary,
                target_beat_count=target_beat_count,
            )
        except Exception as exc:
            logger.error(
                "Storyboard batch failed for segment %s (%d/%d) — aborting storyboard "
                "generation entirely (fail-loud: a partial storyboard would leave gaps "
                "in the narration with no designed visuals): %s",
                label, index, len(segments), exc,
            )
            return None

        total_output_tokens += usage.get("output_tokens", 0)
        beats = storyboard.get("beats") or []
        if not beats:
            logger.warning(
                "Storyboard batch for segment %s (%d/%d) returned no beats — aborting "
                "storyboard generation entirely",
                label, index, len(segments),
            )
            return None

        # Hint hardening: fix any hints that are out-of-range or contain digits
        beats = _harden_hints(beats, text)

        logger.info(
            "Storyboard batch ok: segment=%s (%d/%d) target_beats=%d actual_beats=%d output_tokens=%d/%d",
            label, index, len(segments), target_beat_count, len(beats),
            usage.get("output_tokens", 0), _STORYBOARD_BATCH_MAX_TOKENS_LOG,
        )

        raw_batches.append(beats)
        overall_style = overall_style or storyboard.get("overall_style", "")

        # Update ledger then build the next segment's continuity summary
        _update_ledger(ledger, beats)
        previous_summary = _summarize_batch_for_continuity(label, beats, ledger)

    beats = _merge_batches(raw_batches)

    logger.info(
        "Storyboard generation complete: schema_version=%s language=%s batch_count=%d "
        "estimated_beat_count=%d actual_beat_count=%d estimated_output_tokens=%d "
        "actual_output_tokens=%d style=%r top_envs=%s",
        _STORYBOARD_SCHEMA_VERSION_LOG, language, len(raw_batches), estimated_beats, len(beats),
        estimated_beats * _STORYBOARD_TOKENS_PER_BEAT_LOG, total_output_tokens, overall_style,
        sorted(ledger["env_counts"].items(), key=lambda x: -x[1])[:3],
    )

    mapped = map_storyboard_beats_to_timestamps(
        beats, whisper_transcript, duration_ms,
        allow_legacy_fallback=allow_legacy_fallback,
        language=language,
    )
    return mapped


def _split_voice_script_into_segments(voice_script: str) -> list[tuple[str, str]]:
    """Split narration into ``([INTRO]/[SECTION N]/[OUTRO], text)`` segments for batched generation.

    Each segment's text is a verbatim contiguous substring of ``voice_script`` (markers
    stripped, whitespace trimmed) — so every beat's start_hint/end_hint, generated from
    segment text, remains a valid forward-search target against the full Whisper
    transcript, exactly as it was when generated from the whole script at once.

    Returns:
        Ordered list of ``(marker_label, segment_text)`` tuples, one per non-empty
        segment. If no markers are found, returns a single ``("[FULL]", voice_script)``
        segment so batching degrades gracefully to one call instead of failing outright.
    """
    matches = list(_SEGMENT_MARKER_RE.finditer(voice_script))
    if not matches:
        text = voice_script.strip()
        return [("[FULL]", text)] if text else []

    segments: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        label = f"[{match.group(1).strip().upper()}]"
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(voice_script)
        text = voice_script[start:end].strip()
        if text:
            segments.append((label, text))
    return segments


def _estimate_beat_count(voice_script: str, script_format: str) -> int:
    """Estimate the expected total beat count from word count and the prompt's pacing rule.

    Diagnostic-only (Storyboard Failure Analysis, Task 1) — a rough sanity figure
    logged alongside the actual merged beat count so a future pacing or schema
    change that risks reintroducing the token-overflow failure is visible immediately.
    """
    words = len(voice_script.split())
    narration_seconds = (words / _WORDS_PER_MINUTE) * 60
    beat_seconds = _BEAT_SECONDS_BY_FORMAT.get(script_format, _DEFAULT_BEAT_SECONDS)
    return max(int(narration_seconds / beat_seconds), 1)


def _update_ledger(ledger: dict, beats: list[dict]) -> None:
    """Update the cross-segment continuity ledger with the beats from one batch."""
    for b in beats:
        env = b.get("environment", "other")
        vt  = b.get("visual_type", "b-roll")
        ledger["env_counts"][env] = ledger["env_counts"].get(env, 0) + 1
        ledger["recent_envs"].append(env)
        ledger["recent_visual_types"].append(vt)
        ledger["total_beats"] += 1
    # Keep only the last 10 for the "recent" window
    ledger["recent_envs"] = ledger["recent_envs"][-10:]
    ledger["recent_visual_types"] = ledger["recent_visual_types"][-10:]


def _summarize_batch_for_continuity(label: str, beats: list[dict], ledger: dict) -> str:
    """Build a continuity note for the next segment's prompt.

    Includes the closing 3 beats' descriptors AND the cumulative environment
    distribution from the ledger so Claude can self-avoid the most overused
    environments across segment boundaries.
    """
    if not beats:
        return ""
    tail = beats[-3:]
    descriptors = [
        f"{b.get('environment', 'other')}/{b.get('visual_type', 'b-roll')}/{b.get('motif', 'other')}"
        for b in tail
    ]
    closing = f"{label} closed on: " + ", ".join(descriptors)

    if ledger["total_beats"] > 0:
        top_envs = sorted(ledger["env_counts"].items(), key=lambda x: -x[1])[:4]
        env_str = ", ".join(f"{e}×{c}" for e, c in top_envs)
        recent_vt = ", ".join(ledger["recent_visual_types"][-6:])
        closing += (
            f". Video-wide env totals (avoid most-used): {env_str}. "
            f"Last 6 visual_types: {recent_vt}"
        )
    return closing


def _harden_hints(beats: list[dict], segment_text: str) -> list[dict]:
    """Log quality warnings for start_hint/end_hint that violate verbatim-word rules.

    Rules Claude is required to follow (enforced in the schema prompt):
      - 6–10 verbatim words copied from the narration
      - no digit characters
      - no marker text (INTRO/OUTRO/SECTION)

    When a hint violates these rules we log a WARNING and leave it unchanged.
    The matching pipeline (_locate_phrase / _fill_gaps) handles unmatched beats
    via real anchor timestamps from adjacent matched beats — proportional text
    substitution here inflated the fallback rate by replacing valid short phrases
    with wrong-position text that couldn't match the Whisper transcript.
    """
    _DIGIT_IN_HINT_RE = re.compile(r"\d")
    _MARKER_IN_HINT_RE = re.compile(r"\[(INTRO|OUTRO|SECTION)", re.IGNORECASE)

    for beat in beats:
        for hint_key in ("start_hint", "end_hint"):
            raw = str(beat.get(hint_key, "") or "").strip()
            hint_words = raw.split()
            valid = (
                6 <= len(hint_words) <= 10
                and not _DIGIT_IN_HINT_RE.search(raw)
                and not _MARKER_IN_HINT_RE.search(raw)
            )
            if not valid:
                logger.warning(
                    "Hint quality: beat=%s %s %r is invalid "
                    "(words=%d has_digit=%s has_marker=%s) — kept as-is for matching",
                    beat.get("beat_order"), hint_key, raw[:60],
                    len(hint_words),
                    bool(_DIGIT_IN_HINT_RE.search(raw)),
                    bool(_MARKER_IN_HINT_RE.search(raw)),
                )

    return beats


def _merge_batches(raw_batches: list[list[dict]]) -> list[dict]:
    """Concatenate per-segment beat batches into one globally-ordered, sequentially-numbered list.

    Each batch is locally normalized first (``_normalize_beat_order`` — Claude's
    per-batch ``beat_order`` is untrusted, exactly as in the legacy single-call path),
    sorting and de-duplicating within the segment. Batches are then concatenated in
    segment order and renumbered ``0..N-1`` globally, so beat ordering, transitions,
    and the forward-cursor timestamp mapping all stay consistent across segment
    boundaries — downstream consumers see one continuous storyboard, unaware it was
    assembled from several Claude calls.
    """
    merged: list[dict] = []
    for batch in raw_batches:
        merged.extend(_normalize_beat_order(batch))

    return [{**beat, "beat_order": global_order} for global_order, beat in enumerate(merged)]


# ── Beat → timestamp mapping ──────────────────────────────────────────────────

def map_storyboard_beats_to_timestamps(
    beats: list[dict],
    whisper_transcript: list[dict],
    duration_ms: int,
    allow_legacy_fallback: bool = False,
    language: str = "en",
) -> list[dict] | None:
    """Map each storyboard beat onto real audio timestamps using Whisper words.

    Locates each beat's ``start_hint``/``end_hint`` phrase in the Whisper word
    list (forward-only search, fuzzy prefix matching to tolerate transcription
    drift). Beats whose hints cannot be located fall back to proportional timing,
    interpolated between their nearest successfully matched neighbours.

    Cursor advancement moves only to the END of the START_HINT match (not to
    the end of the full beat span including end_hint) — this prevents greedy
    end_hint matching from consuming subsequent beats' territory and causing a
    cascade of proportional fallbacks.

    Args:
        beats:                Raw beat dicts from ``generate_storyboard``.
        whisper_transcript:   Word-level timestamps (``[{"word", "start", "end"}]``, seconds).
        duration_ms:          Exact audio duration in milliseconds.
        allow_legacy_fallback: When ``True``, accept the result even if > 50 % of beats
            used proportional fallback; when ``False`` (default), return ``None``
            instead so the caller can apply its fallback policy.
        language:             BCP-47 language code — passed to ``_normalize_phrase``
            for digit-to-word expansion when matching hint phrases.

    Returns:
        List of renderable beat-section dicts, or ``None`` if the fallback rate
        exceeded ``_FALLBACK_FAIL_RATIO`` and ``allow_legacy_fallback`` is False.
    """
    flat = _flatten_transcript(whisper_transcript)
    beats = _normalize_beat_order(beats)
    n = len(beats)

    # match[i] = (full_start_idx, full_end_idx) for exact/fuzzy hits, None for misses
    matches: list[tuple[int, int] | None] = [None] * n
    # match_type[i] = "exact" | "fuzzy" | "fallback"
    match_type: list[str] = ["fallback"] * n
    cursor = 0

    for i, beat in enumerate(beats):
        located, start_hint_end_idx = _locate_beat_span(
            flat, cursor, str(beat.get("start_hint", "")), str(beat.get("end_hint", "")),
            language=language,
        )
        if located is None:
            continue
        matches[i] = located
        # Classify: exact = full start_hint token sequence matched; fuzzy = prefix matched
        sh_tokens = _normalize_phrase(str(beat.get("start_hint", "")), language)
        span_len = located[1] - located[0] + 1
        if len(sh_tokens) > 0 and span_len >= len(sh_tokens):
            match_type[i] = "exact"
        else:
            match_type[i] = "fuzzy"
        # Advance cursor past the start_hint match ONLY — not past end_hint.
        # This prevents greedy end_hint matching from consuming subsequent beats'
        # narration territory, which was the primary cause of cascading fallbacks.
        cursor = start_hint_end_idx + 1

    n_exact    = sum(1 for t in match_type if t == "exact")
    n_fuzzy    = sum(1 for t in match_type if t == "fuzzy")
    n_fallback = sum(1 for t in match_type if t == "fallback")
    fallback_orders = [beats[i].get("beat_order", i) for i, t in enumerate(match_type) if t == "fallback"]

    avg_beat_ms = duration_ms / n if n > 0 else 0
    logger.info(
        "Storyboard timestamp mapping: total=%d exact=%d fuzzy=%d fallback=%d "
        "avg_beat_duration=%.0fms",
        n, n_exact, n_fuzzy, n_fallback, avg_beat_ms,
    )
    if fallback_orders:
        logger.warning(
            "Storyboard timestamp mapping: %d beat(s) used proportional fallback "
            "(%.0f%%) — beat_orders=%s",
            n_fallback, 100 * n_fallback / n, fallback_orders,
        )

    fallback_ratio = n_fallback / n if n > 0 else 0
    if fallback_ratio > _FALLBACK_FAIL_RATIO:
        if allow_legacy_fallback:
            logger.warning(
                "Storyboard timestamp mapping: fallback rate %.0f%% > %.0f%% threshold — "
                "accepting result because allow_legacy_fallback=True",
                100 * fallback_ratio, 100 * _FALLBACK_FAIL_RATIO,
            )
        else:
            logger.error(
                "Storyboard timestamp mapping: fallback rate %.0f%% > %.0f%% threshold — "
                "returning None so caller can apply allow_legacy_fallback policy "
                "(fallback_reason=mapping_quality_below_threshold)",
                100 * fallback_ratio, 100 * _FALLBACK_FAIL_RATIO,
            )
            return None
    elif fallback_ratio > _FALLBACK_WARN_RATIO:
        logger.warning(
            "Storyboard timestamp mapping: fallback rate %.0f%% > %.0f%% warn threshold — "
            "check hint quality or normalization",
            100 * fallback_ratio, 100 * _FALLBACK_WARN_RATIO,
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
    """Normalize Whisper words into ``(norm_token, original_word, start_ms, end_ms)`` tuples.

    Apostrophes are expanded to spaces so French elisions that Whisper splits
    into separate tokens (``d'argent`` → ``["d", "argent"]``) match the same
    two-token form produced by ``_normalize_phrase`` on the hint side.  Each
    sub-token inherits the parent word's start/end timestamps.
    """
    flat = []
    for w in whisper_transcript:
        word = str(w.get("word", ""))
        start_ms = int(float(w.get("start", 0)) * 1000)
        end_ms   = int(float(w.get("end",   0)) * 1000)
        for part in _APOSTROPHE_RE.split(word):
            norm = _normalize_word(part)
            if norm:
                flat.append((norm, part, start_ms, end_ms))
    return flat


def _normalize_word(word: str) -> str:
    """Lowercase, strip accents (NFD), and keep only alphanumeric characters.

    Accent normalization (NFD → strip combining marks) makes matching robust to
    encoding differences between Claude hints and Whisper transcriptions (e.g.
    precomposed ``é`` vs decomposed ``e`` + combining accent).
    """
    nfd = unicodedata.normalize("NFD", word.lower())
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return "".join(_WORD_RE.findall(stripped))


def _normalize_phrase(phrase: str, language: str = "en") -> list[str]:
    """Split a hint phrase into normalized tokens with digit expansion.

    Uses ``normalize_for_matching`` for full normalization including digit expansion
    so that a hint containing "1984" matches Whisper's spoken form "nineteen eighty
    four". Apostrophes are handled by ``normalize_for_matching``'s punctuation
    stripping, which aligns with ``_flatten_transcript``'s apostrophe expansion.

    Args:
        phrase:   Raw hint string from Claude (may contain digits or accented chars).
        language: BCP-47 language code for digit-to-word expansion (e.g. "fr", "en").

    Returns:
        List of normalized, lowercase, punctuation-free tokens.
    """
    return _normalize_for_matching(phrase, language)


def _locate_beat_span(
    flat: list[tuple[str, str, int, int]],
    cursor: int,
    start_hint: str,
    end_hint: str,
    language: str = "en",
) -> tuple[tuple[int, int] | None, int]:
    """Locate a beat's inclusive ``[start_idx, end_idx]`` token span in the transcript.

    Returns:
        ``(span, start_hint_end_idx)`` where ``span`` is ``(start_idx, end_idx)``
        or ``None`` on failure, and ``start_hint_end_idx`` is the last token index
        of the start_hint match (used by the caller to advance the cursor without
        overshooting into subsequent beats' territory).
    """
    start_match = _locate_phrase(flat, cursor, start_hint, language=language)
    if start_match is None:
        return None, cursor          # unchanged cursor on failure

    start_idx       = start_match[0]
    start_hint_end  = start_match[1]   # cursor advances HERE, not to end_hint end

    end_match = _locate_phrase(flat, start_idx, end_hint, language=language)
    if end_match is None:
        return (start_idx, start_hint_end), start_hint_end

    return (start_idx, max(end_match[1], start_idx)), start_hint_end


def _locate_phrase(
    flat: list[tuple[str, str, int, int]],
    from_idx: int,
    phrase: str,
    language: str = "en",
) -> tuple[int, int] | None:
    """Find a phrase forward from ``from_idx``, trying shrinking prefixes for fuzzy tolerance.

    Returns:
        Inclusive ``(start_idx, end_idx)`` token-index span, or ``None`` if not found.
    """
    tokens = _normalize_phrase(phrase, language)
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
    beats inherit the previous matched beat's timestamp (adjacent-boundary fallback)
    rather than proportional interpolation — proportional text substitution in
    _harden_hints inflated the fallback rate by producing wrong-position hints that
    couldn't match the transcript. _enforce_minimum_durations then pushes each
    unmatched beat forward by _MIN_BEAT_MS, giving it a real time slice.

    Guarantees (enforced by ``_enforce_minimum_durations`` after boundary assembly):
    - ``audio_end_ms > audio_start_ms`` for every beat (minimum ``_MIN_BEAT_MS``)
    - strictly monotonic: each beat's start >= previous beat's end
    - first beat starts at 0, last beat ends exactly at ``duration_ms``
    - zero-width spans are corrected and logged
    """
    n = len(beats)
    anchors: list[int | None] = [
        flat[match[0]][2] if match is not None else None
        for match in matches
    ]

    # _fill_gaps is intentionally not called here. None anchors fall through to
    # the `prev` path in the loop below, anchoring unmatched beats to the nearest
    # prior real timestamp instead of a proportional guess.

    starts = [0] * n
    prev = 0
    for i in range(n):
        candidate = anchors[i] if anchors[i] is not None else prev
        starts[i] = max(candidate, prev)
        prev = starts[i]

    boundaries: list[tuple[int, int]] = []
    for i in range(n):
        end_ms = starts[i + 1] if i + 1 < n else duration_ms
        boundaries.append((starts[i], end_ms))

    if boundaries:
        last_start, _ = boundaries[-1]
        boundaries[-1] = (last_start, max(duration_ms, last_start))

    _enforce_minimum_durations(boundaries, duration_ms)
    return boundaries


def _enforce_minimum_durations(
    boundaries: list[tuple[int, int]],
    duration_ms: int,
) -> None:
    """Mutate ``boundaries`` in place so every beat has duration >= ``_MIN_BEAT_MS``.

    Propagates end_ms forward through the list so that extending one beat does
    not collapse the next; the last beat is always clamped to ``duration_ms``.
    Zero-width corrections are counted and logged.
    """
    n = len(boundaries)
    if n == 0:
        return

    zero_width_corrected = 0
    for i in range(n):
        start_ms, end_ms = boundaries[i]
        # Guarantee start >= 0
        start_ms = max(start_ms, 0)
        min_end  = start_ms + _MIN_BEAT_MS
        if end_ms < min_end:
            zero_width_corrected += 1
            end_ms = min_end
        boundaries[i] = (start_ms, end_ms)
        # Propagate: next beat must start >= this beat's end
        if i + 1 < n:
            next_start, next_end = boundaries[i + 1]
            if next_start < end_ms:
                boundaries[i + 1] = (end_ms, max(next_end, end_ms))

    # Clamp last beat to duration_ms (never overshoot)
    last_start, last_end = boundaries[-1]
    boundaries[-1] = (min(last_start, duration_ms), duration_ms)

    if zero_width_corrected:
        logger.warning(
            "Storyboard timestamp mapping: %d zero-width beat(s) corrected "
            "(extended to minimum %dms)",
            zero_width_corrected, _MIN_BEAT_MS,
        )


def _fill_gaps(anchors: list[int | None], beats: list[dict], duration_ms: int) -> None:
    """Interpolate start_ms for unmatched beats between their matched neighbours.

    Distributes the span between two known anchors with equal weight across each
    unmatched beat in the run (the per-beat ``duration_target_sec`` weight this used
    was a write-only schema field, removed in schema v2.0 — see Storyboard Schema
    Reduction). Edge runs fall back to ``0`` / ``duration_ms`` as their bounding anchors.
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
        step = span / len(run)
        for offset, k in enumerate(run):
            anchors[k] = int(left_ms + step * offset)

        i = j


def _build_beat_section(beat: dict, index: int, start_ms: int, end_ms: int, script_text: str) -> dict:
    """Build a renderable beat-section dict from a raw beat + resolved timestamps.

    ``section_order`` mirrors ``beat_order`` so downstream pipeline stages
    (persistence, shorts cutter, Remotion builder) work unchanged on storyboard beats.
    ``flux_prompt`` is passed through unchanged — Flux Schnell will use it to
    generate the image for this beat.
    """
    beat_order = beat.get("beat_order", index)

    return {
        "beat_order":      beat_order,
        "section_order":   beat_order,
        "audio_start_ms":  start_ms,
        "audio_end_ms":    end_ms,
        "duration_sec":    max(end_ms - start_ms, 0) / 1000,
        "script_text":     script_text,
        "visual_intent":   str(beat.get("visual_intent", "")),
        "visual_type":     _safe_enum(beat.get("visual_type"), _VALID_VISUAL_TYPES, _DEFAULT_VISUAL_TYPE),
        "visual_category": _safe_enum(beat.get("visual_category"), _VALID_VISUAL_CATEGORIES, _DEFAULT_VISUAL_CATEGORY),
        "environment":     _safe_enum(beat.get("environment"), _VALID_ENVIRONMENTS, _DEFAULT_ENVIRONMENT),
        "flux_prompt":     str(beat.get("flux_prompt", "") or ""),
        "effect":          _safe_enum(beat.get("effect"), _VALID_EFFECTS, _DEFAULT_EFFECT),
        "color_grade":     _safe_enum(beat.get("color_grade"), _VALID_GRADES, _DEFAULT_GRADE),
        "transition_to_next": _safe_enum(beat.get("transition_to_next"), _VALID_TRANSITIONS, _DEFAULT_TRANSITION),
        "overlay_text":    str(beat.get("overlay_text", "") or ""),
        "overlay_position": _safe_enum(beat.get("overlay_position"), _VALID_OVERLAY_POSITIONS, _DEFAULT_OVERLAY_POSITION),
        "motif":           _safe_enum(beat.get("motif"), _VALID_MOTIFS, _DEFAULT_MOTIF),
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
