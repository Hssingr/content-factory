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

import json
import logging
import re
import unicodedata
import uuid

from sqlalchemy.orm import Session

from app.agents.agent4_visuals.system_prompt import (
    STORYBOARD_BATCH_MAX_TOKENS as _STORYBOARD_BATCH_MAX_TOKENS_LOG,
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION_LOG,
    generate_storyboard_batch,
)
from app.models import VideoSection
from app.services.claude_client import call_claude_structured
from app.agents.agent4_visuals.services.flux_generator import generate_beat_image
from app.shared.text_normalize import normalize_for_matching as _normalize_for_matching

logger = logging.getLogger(__name__)

# Apostrophes and typographic variants — all treated as word-boundary separators
# so that French elisions (l'entreprise → ["l","entreprise"]) tokenize the same
# way Whisper splits them, while English contractions (hadn't → ["hadn","t"])
# also become consistent two-token forms on both hint and transcript sides.
_APOSTROPHE_RE = re.compile(r"['’ʼʻ‘]")
# Token pattern — apostrophe removed; we expand it to spaces before matching
_WORD_RE = re.compile(r"[a-zÀ-ɏ0-9]+", re.IGNORECASE)

# Intensity-aware minimum durations per beat after timestamp mapping.
# Prevents zero-width spans while respecting narrative pacing intent.
# Falls back to _MIN_BEAT_MS_FALLBACK for beats with unknown intensity.
INTENSITY_FLOOR_MS: dict[str, int] = {
    "high":   1000,   # 1.0s — reveals, contradictions, shock moments
    "medium": 2000,   # 2.0s — normal story progression
    "low":    3000,   # 3.0s — establishing shots, emotional pauses
}
_MIN_BEAT_MS_FALLBACK = 500   # used only when beat_intensity is absent

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
_VALID_MEDIA_STRATEGIES  = {"flux_generated", "stock_video", "stock_image", "remotion_text_card"}
_VALID_TEXT_CARD_STYLES  = {"chat", "document", "statistic", "quote", "default"}
_DEFAULT_MEDIA_STRATEGY  = "flux_generated"
_DEFAULT_TEXT_CARD_STYLE = "default"

# Hybrid media strategy enforcement: stock_video / stock_image are reserved for a future release.
# Python overrides them to flux_generated and logs a WARNING.
_STOCK_STRATEGIES = frozenset({"stock_video", "stock_image"})

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
# markers before TTS (agent3_audio/services/tts.py) and before quality prompts
# (services/video.py _script_hook), kept in sync so segmentation matches exactly
# what the narrator actually speaks.
_SEGMENT_MARKER_RE = re.compile(
    r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Language sentinel that marks the shared visual-pass beats (generated once, reused
# by all language renders). Must match the value defined in video.py.
_VISUAL_LANGUAGE = "__visual__"

# text_card fallback sentinel — written to media_url when Flux generation fails.
_TEXT_CARD_SENTINEL = "__text_card__"

# match_score threshold: assignments at or above this reuse the parent Flux image;
# below this threshold a new image is generated.
_MATCH_SCORE_THRESHOLD = 70

# ── Short episode storyboard remap (Haiku) ─────────────────────────────────────

_SHORT_REMAP_SYSTEM_PROMPT = (
    "You map a parent documentary video's visual beats to a Short episode narration.\n\n"
    "You receive:\n"
    "- A Short narration text (60-90 seconds of standalone content)\n"
    "- A compact index of parent video beats: beat_order, visual_intent, environment, motif\n\n"
    "Divide the narration into phrases of roughly 3-5 seconds each. "
    "For each phrase, assign the most thematically relevant parent beat.\n\n"
    "For each assignment:\n"
    "- narration_phrase: the exact narration text this beat covers (verbatim excerpt)\n"
    "- long_beat_order: integer index from the parent beat index that best matches this phrase\n"
    "- match_score: 0-100 integer\n"
    "  90-100: Same subject and setting — direct visual match\n"
    "  70-89:  Compatible environment and mood — reuse is appropriate\n"
    "  50-69:  Loosely related, different context — new image is better\n"
    "  0-49:   Unrelated — new image required\n"
    "- beat_intensity: pacing intent for this phrase\n"
    "  'high':   revelation, shock, key fact\n"
    "  'medium': story progression, context\n"
    "  'low':    setup, establishing, pause\n\n"
    "Rules:\n"
    "- Multiple phrases may share the same long_beat_order (parent beats can be reused)\n"
    "- Prefer variety: if two phrases have similar scores, pick different parent beats\n"
    "- Return ONLY valid JSON. No markdown. No code fence. No extra keys.\n"
    "PROMPT_VERSION = \"1.0\""
)

_SHORT_REMAP_SCHEMA: dict = {
    "type": "object",
    "required": ["assignments"],
    "properties": {
        "assignments": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["narration_phrase", "long_beat_order", "beat_intensity", "match_score"],
                "properties": {
                    "narration_phrase": {"type": "string"},
                    "long_beat_order":  {"type": "integer"},
                    "beat_intensity":   {"type": "string", "enum": ["high", "medium", "low"]},
                    "match_score":      {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}

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
    storyboard_constraints: str = "",
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
        storyboard_constraints: Optional text appended to every segment's user
            message via ``generate_storyboard_batch(override_instructions=...)``.
            Used by the validation-gate retry pass in ``video.py`` to pass MAJOR
            issue descriptions back to Claude so it can correct them.

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
    logger.debug(
        "STORYBOARD_ESTIMATE script_words=%d estimated_beats=%d estimated_formula_used=%s",
        total_words, estimated_beats,
        f"words({total_words})/WPM({_WORDS_PER_MINUTE})*60/beat_sec({beat_seconds:.1f})",
    )

    raw_batches: list[list[dict]] = []
    overall_style = ""
    previous_summary = ""
    total_output_tokens      = 0
    total_input_tokens       = 0
    total_generation_time_ms = 0
    total_claude_calls       = 0
    _retry_count             = 0
    _truncation_count        = 0
    _requested_beats         = 0
    _hint_total              = 0
    _hint_valid              = 0
    _hint_invalid            = 0

    # Cross-segment continuity ledger: tracks cumulative environment counts and
    # recent visual_types across all batches to give Claude global repetition context.
    ledger: dict = {"env_counts": {}, "recent_envs": [], "recent_visual_types": [], "total_beats": 0}

    for index, (label, text) in enumerate(segments, start=1):
        # Per-segment target beat count from proportional audio duration
        seg_words = max(len(text.split()), 1)
        seg_duration_sec = (seg_words / total_words) * (duration_ms / 1000)
        target_beat_count = max(1, round(seg_duration_sec / beat_seconds))

        try:
            storyboard, usage, diag = generate_storyboard_batch(
                segment_label=label,
                segment_text=text,
                segment_index=index,
                segment_count=len(segments),
                channel=channel,
                script_format=script_format,
                previous_segment_summary=previous_summary,
                target_beat_count=target_beat_count,
                override_instructions=storyboard_constraints,
            )
        except Exception as exc:
            logger.error(
                "Storyboard batch failed for segment %s (%d/%d) — aborting storyboard "
                "generation entirely (fail-loud: a partial storyboard would leave gaps "
                "in the narration with no designed visuals): %s",
                label, index, len(segments), exc,
            )
            return None

        total_output_tokens      += usage.get("output_tokens", 0)
        total_input_tokens       += diag.get("input_tokens", 0)
        total_generation_time_ms += diag.get("elapsed_ms", 0)
        total_claude_calls       += diag.get("attempt_count", 1)
        if diag.get("was_truncated"):
            _truncation_count += 1
            _retry_count      += 1
        _requested_beats += target_beat_count
        beats = storyboard.get("beats") or []
        if not beats:
            logger.warning(
                "Storyboard batch for segment %s (%d/%d) returned no beats — aborting "
                "storyboard generation entirely",
                label, index, len(segments),
            )
            return None

        # Hint hardening: fix any hints that are out-of-range or contain digits
        beats, _seg_hint_stats = _harden_hints(beats, text)
        _hint_total   += _seg_hint_stats["total_hints"]
        _hint_valid   += _seg_hint_stats["valid_hints"]
        _hint_invalid += _seg_hint_stats["invalid_hints"]

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

    _actual_beats = len(beats)
    _estimate_error_pct = abs(_actual_beats - estimated_beats) / max(estimated_beats, 1) * 100

    logger.info(
        "Storyboard generation complete: schema_version=%s language=%s batch_count=%d "
        "estimated_beat_count=%d actual_beat_count=%d estimated_output_tokens=%d "
        "actual_output_tokens=%d style=%r top_envs=%s",
        _STORYBOARD_SCHEMA_VERSION_LOG, language, len(raw_batches), estimated_beats, _actual_beats,
        estimated_beats * _STORYBOARD_TOKENS_PER_BEAT_LOG, total_output_tokens, overall_style,
        sorted(ledger["env_counts"].items(), key=lambda x: -x[1])[:3],
    )
    logger.info(
        "STORYBOARD_FINAL segments=%d requested_beats=%d generated_beats=%d "
        "avg_beats_per_segment=%.1f total_output_tokens=%d total_input_tokens=%d "
        "total_generation_time_ms=%d retry_count=%d truncation_count=%d",
        len(segments), _requested_beats, _actual_beats,
        _actual_beats / max(len(segments), 1),
        total_output_tokens, total_input_tokens, total_generation_time_ms,
        _retry_count, _truncation_count,
    )
    logger.debug(
        "STORYBOARD_ESTIMATE_ACCURACY estimated_beats=%d actual_generated_beats=%d error_percent=%.1f",
        estimated_beats, _actual_beats, _estimate_error_pct,
    )
    _est_usd = (total_input_tokens / 1_000_000 * 3.0) + (total_output_tokens / 1_000_000 * 15.0)
    logger.info(
        "STORYBOARD_COST_ESTIMATE claude_calls=%d total_input_tokens=%d "
        "total_output_tokens=%d estimated_usd=%.4f",
        total_claude_calls, total_input_tokens, total_output_tokens, _est_usd,
    )
    if _hint_total > 0:
        _inv_rate = _hint_invalid / _hint_total * 100
        logger.warning(
            "HINT_QUALITY_SUMMARY total_hints=%d valid_hints=%d invalid_hints=%d "
            "invalid_rate_percent=%.1f",
            _hint_total, _hint_valid, _hint_invalid, _inv_rate,
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


def _harden_hints(beats: list[dict], segment_text: str) -> tuple[list[dict], dict]:
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

    Returns:
        ``(beats, stats)`` — beats is the same list (unchanged); stats has
        ``total_hints``, ``valid_hints``, ``invalid_hints`` counts for aggregation
        into a ``HINT_QUALITY_SUMMARY`` log after all segments are processed.
    """
    _DIGIT_IN_HINT_RE = re.compile(r"\d")
    _MARKER_IN_HINT_RE = re.compile(r"\[(INTRO|OUTRO|SECTION)", re.IGNORECASE)

    _total = 0
    _invalid = 0

    for beat in beats:
        for hint_key in ("start_hint", "end_hint"):
            raw = str(beat.get(hint_key, "") or "").strip()
            hint_words = raw.split()
            valid = (
                6 <= len(hint_words) <= 10
                and not _DIGIT_IN_HINT_RE.search(raw)
                and not _MARKER_IN_HINT_RE.search(raw)
            )
            _total += 1
            if not valid:
                _invalid += 1
                logger.warning(
                    "Hint quality: beat=%s %s %r is invalid "
                    "(words=%d has_digit=%s has_marker=%s) — kept as-is for matching",
                    beat.get("beat_order"), hint_key, raw[:60],
                    len(hint_words),
                    bool(_DIGIT_IN_HINT_RE.search(raw)),
                    bool(_MARKER_IN_HINT_RE.search(raw)),
                )

    _stats = {
        "total_hints":   _total,
        "valid_hints":   _total - _invalid,
        "invalid_hints": _invalid,
    }
    return beats, _stats


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
    logger.warning(
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
    couldn't match the transcript. _cleanup_micro_beats then pushes each
    unmatched beat forward by its intensity floor, giving it a real time slice.

    Guarantees (enforced by ``_cleanup_micro_beats`` after boundary assembly):
    - ``audio_end_ms > audio_start_ms`` for every beat (intensity-aware floor)
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

    _cleanup_micro_beats(boundaries, duration_ms, beats)
    return boundaries


def _cleanup_micro_beats(
    boundaries: list[tuple[int, int]],
    duration_ms: int,
    beats: list[dict] | None = None,
) -> None:
    """Mutate ``boundaries`` in place so every beat meets its intensity-aware floor.

    Propagates end_ms forward through the list so that extending one beat does
    not collapse the next; the last beat is always clamped to ``duration_ms``.
    Zero-width corrections are counted and logged.

    When ``beats`` is provided, each beat's ``beat_intensity`` determines its
    minimum duration via ``INTENSITY_FLOOR_MS``. Unknown or absent intensities
    fall back to ``_MIN_BEAT_MS_FALLBACK``.

    Merge preference (when a beat is below its floor): the beat's end_ms is
    extended forward, which absorbs time from the next beat's territory. Prefer
    merging into a beat with the same or lower intensity — this is implemented
    by propagating into the next beat regardless, since extending into a lower-
    intensity beat is always valid (low beats have the largest floor).
    """
    n = len(boundaries)
    if n == 0:
        return

    zero_width_corrected = 0
    for i in range(n):
        start_ms, end_ms = boundaries[i]
        start_ms = max(start_ms, 0)

        intensity = "medium"
        if beats and i < len(beats):
            intensity = beats[i].get("beat_intensity") or "medium"
        floor_ms = INTENSITY_FLOOR_MS.get(intensity, _MIN_BEAT_MS_FALLBACK)

        min_end = start_ms + floor_ms
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
            "Storyboard timestamp mapping: %d beat(s) below intensity floor corrected",
            zero_width_corrected,
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
    (persistence and Remotion builder) work unchanged on storyboard beats.
    ``flux_prompt`` is passed through unchanged — Flux Schnell will use it to
    generate the image for this beat. ``media_url``/``media_type`` are also
    passed through unchanged — for the parent storyboard path these are
    always absent at this point (Flux hasn't run yet), but the child remap
    path (`remap_beats_for_short()`) sets `media_url` *before* calling
    timestamp mapping so `validate_storyboard()` can run before any Flux
    call (Phase 4E-E ordering alignment); dropping the field here would
    silently erase every reuse-vs-pending decision the child path makes.
    """
    beat_order = beat.get("beat_order", index)

    intensity = _safe_enum(beat.get("beat_intensity"), {"high", "medium", "low"}, "medium")
    raw_suggested = beat.get("suggested_duration_sec")
    suggested_sec: float = float(raw_suggested) if raw_suggested is not None else 3.0

    actual_sec = max(end_ms - start_ms, 0) / 1000
    if intensity == "high" and actual_sec > 3.0:
        logger.debug(
            "Beat %d (high intensity) spans %.2fs after timestamp mapping — "
            "Whisper timing preserved; suggested=%.2fs",
            beat_order, actual_sec, suggested_sec,
        )

    # ── media_strategy: stock override (Hybrid media strategy enforcement) ─────────────────
    raw_strategy = _safe_enum(
        beat.get("media_strategy"), _VALID_MEDIA_STRATEGIES, _DEFAULT_MEDIA_STRATEGY
    )
    if raw_strategy in _STOCK_STRATEGIES:
        logger.warning(
            "Beat %d: media_strategy=%r — stock media not yet implemented for the current architecture. "
            "Overriding to 'flux_generated'.",
            beat_order, raw_strategy,
        )
        raw_strategy = "flux_generated"

    # When strategy is remotion_text_card, force visual_type to "text_card" so
    # MediaSection.tsx renders TextCard instead of trying to display a Flux image.
    resolved_visual_type = _safe_enum(
        beat.get("visual_type"), _VALID_VISUAL_TYPES, _DEFAULT_VISUAL_TYPE
    )
    if raw_strategy == "remotion_text_card":
        resolved_visual_type = "text_card"

    return {
        "beat_order":           beat_order,
        "section_order":        beat_order,
        "audio_start_ms":       start_ms,
        "audio_end_ms":         end_ms,
        "duration_sec":         actual_sec,
        "script_text":          script_text,
        "visual_intent":        str(beat.get("visual_intent", "")),
        "visual_type":          resolved_visual_type,
        "visual_category":      _safe_enum(beat.get("visual_category"), _VALID_VISUAL_CATEGORIES, _DEFAULT_VISUAL_CATEGORY),
        "environment":          _safe_enum(beat.get("environment"), _VALID_ENVIRONMENTS, _DEFAULT_ENVIRONMENT),
        "flux_prompt":          str(beat.get("flux_prompt", "") or ""),
        "effect":               _safe_enum(beat.get("effect"), _VALID_EFFECTS, _DEFAULT_EFFECT),
        "color_grade":          _safe_enum(beat.get("color_grade"), _VALID_GRADES, _DEFAULT_GRADE),
        "transition_to_next":   _safe_enum(beat.get("transition_to_next"), _VALID_TRANSITIONS, _DEFAULT_TRANSITION),
        "overlay_text":         str(beat.get("overlay_text", "") or ""),
        "overlay_position":     _safe_enum(beat.get("overlay_position"), _VALID_OVERLAY_POSITIONS, _DEFAULT_OVERLAY_POSITION),
        "motif":                _safe_enum(beat.get("motif"), _VALID_MOTIFS, _DEFAULT_MOTIF),
        "beat_intensity":       intensity,
        "suggested_duration_sec": suggested_sec,
        "media_strategy":       raw_strategy,
        "text_card_style":      _safe_enum(
            beat.get("text_card_style"), _VALID_TEXT_CARD_STYLES, _DEFAULT_TEXT_CARD_STYLE
        ),
        "media_url":            beat.get("media_url", ""),
        "media_type":           beat.get("media_type", "image"),
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


# ── Short episode storyboard remap ─────────────────────────────────────────────

def remap_beats_for_short(
    short_content,
    short_voice_script: str,
    short_audio_file,
    parent_content_id: uuid.UUID,
    db: Session,
) -> list[dict]:
    """Remap parent long-video beats to a standalone Short episode narration.

    Loads the parent video's shared visual beats (language="__visual__"), asks Haiku
    to assign the most thematically relevant parent beat to each narration phrase, then
    applies a match_score threshold: scores >= ``_MATCH_SCORE_THRESHOLD`` reuse the
    parent's Flux image; lower scores require a new Flux image. Missing or text_card
    ``media_url`` values also require a new image regardless of score.

    Does NOT call Flux/fal.ai itself for beats that need a new image — those are
    left with ``media_url=""`` (pending) so the caller can run
    ``validate_storyboard()`` against the beat list before any generation
    happens (Phase 4E-E ordering alignment, mirroring the parent path's
    validate-then-generate order). Call ``generate_pending_beat_images()``
    afterward to fill in the pending beats' ``media_url``.

    Timestamps are resolved via ``map_storyboard_beats_to_timestamps()`` on the Short's
    own Whisper transcript with ``allow_legacy_fallback=True`` — short episodes always
    accept proportional fallback timing if hint matching fails.

    Args:
        short_content:      Content ORM row for the Short episode.
        short_voice_script: Narration text for this Short episode.
        short_audio_file:   AudioFile ORM row (provides duration_ms + whisper_transcript + language).
        parent_content_id:  content_id of the long-form parent video.
        db:                 SQLAlchemy session.

    Returns:
        List of renderable beat-section dicts. Reused beats have ``media_url``
        set; beats needing a new image have ``media_url=""`` until
        ``generate_pending_beat_images()`` runs. Empty list on failure.
    """
    content_id_str = str(short_content.id)

    # 1. Load parent __visual__ beats
    parent_rows: list[VideoSection] = (
        db.query(VideoSection)
        .filter(
            VideoSection.content_id == parent_content_id,
            VideoSection.language   == _VISUAL_LANGUAGE,
        )
        .order_by(VideoSection.section_order)
        .all()
    )

    if not parent_rows:
        logger.warning(
            "remap_beats_for_short: no __visual__ beats found for parent=%s "
            "(content=%s) — Agent 4 visuals may not have run on the parent yet",
            parent_content_id, content_id_str,
        )
        return []

    # 2. Parse generation_prompt extras (visual_intent, environment, motif, media_url)
    def _parse_extras(row: VideoSection) -> dict:
        if not row.generation_prompt:
            return {}
        try:
            return json.loads(row.generation_prompt) or {}
        except (json.JSONDecodeError, TypeError):
            return {}

    parent_extras: list[dict] = [_parse_extras(r) for r in parent_rows]

    # Build compact beat index — no flux_prompt (reduces context; Claude doesn't need it)
    beat_index: list[dict] = [
        {
            "beat_order":    r.section_order,
            "visual_intent": extras.get("visual_intent", ""),
            "environment":   extras.get("environment", "other"),
            "motif":         extras.get("motif", "other"),
        }
        for r, extras in zip(parent_rows, parent_extras)
    ]

    # 3. Haiku remap call
    user_message = (
        f"Short narration ({short_audio_file.duration_ms}ms):\n"
        f"{short_voice_script}\n\n"
        f"Parent beat index ({len(beat_index)} beats):\n"
        + json.dumps(beat_index, ensure_ascii=False)
    )

    try:
        result = call_claude_structured(
            task="short_storyboard_remap",
            system_prompt=_SHORT_REMAP_SYSTEM_PROMPT,
            user_message=user_message,
            schema_name="short_storyboard_remap",
            input_schema=_SHORT_REMAP_SCHEMA,
            max_tokens=1024,
        )
    except Exception as exc:
        logger.error(
            "remap_beats_for_short: Claude call failed for content=%s: %s",
            content_id_str, exc,
        )
        return []

    assignments: list[dict] = result.get("assignments") or []
    if not assignments:
        logger.warning(
            "remap_beats_for_short: no assignments returned for content=%s",
            content_id_str,
        )
        return []

    # 4. Apply threshold: reuse parent image or generate new
    parent_by_order: dict[int, tuple] = {
        r.section_order: (r, extras)
        for r, extras in zip(parent_rows, parent_extras)
    }

    beats: list[dict] = []
    reuse_count = 0
    generate_count = 0

    for i, assignment in enumerate(assignments):
        long_order       = int(assignment.get("long_beat_order", -1))
        match_score      = int(assignment.get("match_score", 0))
        beat_intensity   = _safe_enum(assignment.get("beat_intensity"), {"high", "medium", "low"}, "medium")
        narration_phrase = str(assignment.get("narration_phrase", ""))

        parent_row, extras = parent_by_order.get(long_order, (None, {}))
        parent_media: str = extras.get("media_url", "") if extras else ""

        # Phase 4E-E ordering alignment: decide reuse-vs-new here, but do NOT
        # call generate_beat_image() yet — image generation for beats that
        # need a new image is deferred to generate_pending_beat_images(),
        # which the caller (_run_child_short_visuals) runs AFTER
        # validate_storyboard() has had a chance to fire, mirroring the
        # parent path's validate-then-generate ordering
        # (_run_visual_pass: _run_storyboard_validation() before
        # generate_all_beat_images()). A beat needing a new image is left
        # with media_url="" (pending) here; generate_pending_beat_images()
        # fills it in afterward using this same beat dict's flux_prompt.
        if (
            match_score >= _MATCH_SCORE_THRESHOLD
            and parent_media
            and parent_media != _TEXT_CARD_SENTINEL
            and parent_media.startswith("cache/")
        ):
            media_url = parent_media
            reuse_count += 1
        else:
            # Below threshold OR missing/text_card media_url → needs a new
            # Flux image, generated later by generate_pending_beat_images().
            media_url = ""
            generate_count += 1

        flux_prompt = (parent_row.flux_prompt if parent_row else "") or narration_phrase

        beat: dict = {
            "beat_order":             i,
            "section_order":          i,
            "audio_start_ms":         0,
            "audio_end_ms":           0,
            "duration_sec":           0.0,
            "script_text":            narration_phrase,
            "visual_intent":          extras.get("visual_intent", narration_phrase) if extras else narration_phrase,
            "visual_type":            _safe_enum(extras.get("visual_type") if extras else None, _VALID_VISUAL_TYPES, _DEFAULT_VISUAL_TYPE),
            "visual_category":        _safe_enum(extras.get("visual_category") if extras else None, _VALID_VISUAL_CATEGORIES, _DEFAULT_VISUAL_CATEGORY),
            "environment":            _safe_enum(extras.get("environment") if extras else None, _VALID_ENVIRONMENTS, _DEFAULT_ENVIRONMENT),
            "flux_prompt":            flux_prompt,
            "effect":                 _safe_enum(getattr(parent_row, "effect", None) if parent_row else None, _VALID_EFFECTS, _DEFAULT_EFFECT),
            "color_grade":            _safe_enum(getattr(parent_row, "color_grade", None) if parent_row else None, _VALID_GRADES, _DEFAULT_GRADE),
            "transition_to_next":     _safe_enum(extras.get("transition_to_next") if extras else None, _VALID_TRANSITIONS, _DEFAULT_TRANSITION),
            "overlay_text":           str(extras.get("overlay_text", "") or "") if extras else "",
            "overlay_position":       _safe_enum(extras.get("overlay_position") if extras else None, _VALID_OVERLAY_POSITIONS, _DEFAULT_OVERLAY_POSITION),
            "motif":                  _safe_enum(extras.get("motif") if extras else None, _VALID_MOTIFS, _DEFAULT_MOTIF),
            "beat_intensity":         beat_intensity,
            "suggested_duration_sec": 3.0,
            "media_url":              media_url,
            "media_type":             "image",
        }
        beats.append(beat)

    total_beats = len(beats)
    reuse_rate = (reuse_count / total_beats * 100) if total_beats > 0 else 0.0
    logger.info(
        "remap_beats_for_short: content=%s beats=%d reuse=%d pending_generation=%d",
        content_id_str, total_beats, reuse_count, generate_count,
    )
    logger.info(
        "CHILD_SHORT_REUSE_STATS content_id=%s beats=%d reused_parent_images=%d "
        "new_flux_images=%d reuse_rate=%.1f%%",
        content_id_str, total_beats, reuse_count, generate_count, reuse_rate,
    )

    # 5. Map to real timestamps via Short's own Whisper transcript
    whisper: list[dict] = getattr(short_audio_file, "whisper_transcript", None) or []
    language: str = getattr(short_audio_file, "language", "en") or "en"

    if not whisper:
        logger.warning(
            "remap_beats_for_short: no Whisper transcript for content=%s "
            "— proportional timing will be used for all beats",
            content_id_str,
        )

    mapped = map_storyboard_beats_to_timestamps(
        beats=beats,
        whisper_transcript=whisper,
        duration_ms=short_audio_file.duration_ms,
        allow_legacy_fallback=True,
        language=language,
    )
    return mapped or []


def generate_pending_beat_images(beats: list[dict], content_id: str) -> list[dict]:
    """Generate Flux images for beats `remap_beats_for_short()` left pending.

    A beat is "pending" when its `media_strategy` is `flux_generated` and its
    `media_url` is still empty — `remap_beats_for_short()` deliberately defers
    the actual fal.ai call for any beat below the reuse threshold so that
    `validate_storyboard()` runs against the beat list *before* any
    generation happens, mirroring the parent path's validate-then-generate
    ordering. Beats that already have a reused parent `media_url` are left
    untouched (not re-generated).

    Mutates each pending beat in-place (mirrors `generate_all_beat_images()`'s
    contract):
      - Success: sets ``beat["media_url"]`` to a local cache path.
      - Failure: sets ``beat["visual_type"] = "text_card"``,
        ``beat["media_url"] = "__text_card__"``.

    Args:
        beats:      Beat dicts returned by `remap_beats_for_short()`.
        content_id: Content UUID string for logging.

    Returns:
        The same list, with every pending beat's `media_url` filled in.
    """
    pending = [
        b for b in beats
        if b.get("media_strategy", "flux_generated") == "flux_generated" and not b.get("media_url")
    ]
    if not pending:
        return beats

    logger.info(
        "generate_pending_beat_images: content=%s pending=%d/%d beats",
        content_id, len(pending), len(beats),
    )

    for beat in pending:
        new_url = generate_beat_image(
            flux_prompt=beat.get("flux_prompt", ""),
            beat_index=beat.get("beat_order", 0),
            content_id=content_id,
            environment=beat.get("environment", "other"),
        )
        beat["media_url"] = new_url if new_url else _TEXT_CARD_SENTINEL
        beat["media_type"] = "image"

    return beats
