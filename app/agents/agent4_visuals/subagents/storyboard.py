"""Storyboard Agent — Claude designs visual beats; Python maps them to audio timestamps.

The legacy ``section_splitter -> enrich_sections_with_visuals -> validate_sections``
flow this replaced is fully removed (roadmap 6.3 / audit §7) — it was reachable
only behind ``ChannelConfig.allow_legacy_fallback``, which defaults False and the
UI never sets. Claude makes every creative decision (visual intent, visual type,
flux prompts, effects, color grades, transitions); Python only does
the deterministic work — splitting the narration into segments, batching the
Claude calls, merging the results, locating each beat's narration span in the
real Whisper transcript, and converting it into millisecond timestamps.

Generation is BATCHED per narration segment ([INTRO] / [SECTION N] / [OUTRO]) rather
than requested as one whole-video call — a 900-1200 word youtube_long script needs
~90-120 beats at the prompt's own pacing rule (1 beat / 3-5s), which serializes to
~20,000-27,000 tokens at the full schema and structurally cannot fit inside a single
~8192-token response (this was the root cause of 100% storyboard failures: Claude hit
max_tokens mid-beat, producing "Unterminated string" JSON errors). Splitting generation
per segment keeps each call to ~5-25 beats — comfortably inside any reasonable ceiling.

Fail-loud chain (per Recommended Fix BLOCKER #2 — no silent quality degradation):
  any segment batch fails / returns no beats        → entire storyboard generation fails
  storyboard generation fails                        → caller (_run_visual_pass) stops
                                                          language generation with an explicit
                                                          error, unconditionally (roadmap 6.3)
  a beat's start_hint/end_hint can't be located      → proportional timing + logged warning

A segment whose own target_beat_count exceeds _MAX_BEATS_PER_BATCH is split
into multiple sub-calls in Python BEFORE any Claude call is made (see
_split_segment_for_batching) — this is a preventive size check, distinct from
generate_storyboard_batch()'s reactive truncation retry, which only reduces
beat count by 3 and is not enough to recover a genuinely oversized segment.
"""

import json
import logging
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import settings
from app.agents.agent4_visuals.system_prompt import (
    STORYBOARD_BATCH_MAX_TOKENS as _STORYBOARD_BATCH_MAX_TOKENS_LOG,
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION_LOG,
    generate_storyboard_batch,
)
from app.models import VideoSection
from app.services.claude_client import call_claude_structured_with_usage
from app.agents.agent4_visuals.services.flux_generator import (
    _dedupe_generated_image_once,
    derive_text_prop_prompt,
    fill_failed_beats_from_neighbors,
    generate_beat_image_with_routing,
    is_text_prop_beat,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    has_bright_lighting_evidence,
)
from app.services.script_estimator import get_calibrated_wpm
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
# "text_overlay"/"text" removed (schema v7.1) — nothing they describe exists under
# subtitles-only rendering. A legacy stored beat carrying either value normalizes
# to the default via _safe_enum on load/build.
_VALID_VISUAL_TYPES      = {"b-roll", "action", "document", "map", "screenshot", "generated_visual"}
_VALID_VISUAL_CATEGORIES = {"person", "place", "object", "document", "screen", "map", "abstract"}
_VALID_ENVIRONMENTS      = {
    "underwater", "indoor_office", "indoor_domestic", "forest_nature", "urban_street",
    "corridor_interior", "abstract_dark", "open_landscape", "laboratory", "industrial",
    "vehicle", "other",
}
_VALID_MOTIFS            = {
    "doorway", "corridor", "face", "hands", "object", "clock", "phone", "photo",
    "exterior", "text", "screen", "reflection", "document", "room", "other",
}
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

# ── Hint-search proximity window ───────────────────────────────────────────────
# A beat's start_hint may only match near where the beat is EXPECTED to start
# (the last real anchor plus the suggested durations of the beats since it).
# Root cause this guards against (run 9500c231, beat 50): narration reused the
# phrase "long before the …"; one beat's hint failed its full/5-token match at
# the correct position, fell back to a 3-token prefix, and matched the NEXT
# occurrence of that phrase 285 s downstream — the forward search had no
# distance bound, so one beat held a single image for 4.7 minutes and every
# later beat lost its anchor. A match far beyond the expected position is
# worse than no match: an unmatched beat degrades locally (adjacent-anchor
# fallback), a mismatched one corrupts the whole remaining timeline.
_HINT_SEARCH_WINDOW_MS = 60_000        # full/5-token matches: expected + 60 s max
_HINT_SHORT_PREFIX_WINDOW_MS = 15_000  # ≤3-token matches: expected + 15 s max
_HINT_SHORT_PREFIX_MAX_TOKENS = 3      # candidates this short get the tight window

# ── Anchor span sanity ─────────────────────────────────────────────────────────
# Second line of defense behind the proximity window above: a wrong match can
# still land INSIDE the window (a duplicate phrase 30–50 s ahead). After the
# matching loop, the gap between consecutive trusted anchors is compared
# against the suggested durations of the beats that must cover it — a gap
# larger than _SPAN_SANITY_FACTOR × expected coverage (never less than
# _SPAN_SANITY_MIN_MS) means the NEXT anchor is an anchor error, not a real
# position: it is demoted to fallback, and the in-between beats are
# interpolated proportionally between the surrounding trusted anchors
# (_resolve_boundaries). The factor is deliberately generous — real speech
# runs ~1.3-1.4× slower than suggested durations (measured on production
# audio), so 4× fires on genuine anchor errors, not on ordinary drift.
_SPAN_SANITY_FACTOR = 4.0
_SPAN_SANITY_MIN_MS = 20_000

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

# match_score threshold: assignments at or above this inherit the parent beat's
# image PROMPT (visual continuity); below it the remap model's new_image_prompt
# is used. Every child image is generated fresh at portrait size either way —
# there is no physical image reuse on the child path (audit §3.1).
_MATCH_SCORE_THRESHOLD = 70

# Default child Short visual-hold ceiling. Read through settings so operators can
# tune the target without touching code; parent long-form mapping never uses it.
_SHORT_VISUAL_MAX_HOLD_MS = settings.short_visual_max_hold_ms

# Parent long-form hold ceiling — the last-line frozen-frame guarantee behind
# the hint proximity window and the anchor span sanity check: even if both are
# defeated, no non-terminal parent beat may hold one image longer than this.
# Applied in split_into_beats() after timestamp mapping (never inside
# map_storyboard_beats_to_timestamps itself, which the child remap path shares
# and caps separately with the tighter Short ceiling above).
_PARENT_VISUAL_MAX_HOLD_MS = settings.parent_visual_max_hold_ms
_MICRO_MERGE_DISCARD_FAIL_RATIO = 0.30


@dataclass(frozen=True)
class _MicroMergeStats:
    before: int
    after: int
    discarded: int
    discard_ratio: float
    zero_width_corrected: int


# Word-count window for child remap alignment hints — mirrors the parent
# storyboard schema's own start_hint/end_hint convention (system_prompt.py
# "Exact first/last 6-10 verbatim words"), so map_storyboard_beats_to_timestamps()
# sees the same shape of hint on both paths.
_CHILD_HINT_MIN_WORDS = 6
_CHILD_HINT_MAX_WORDS = 10

# If more than this fraction of assignments in one remap response lack a usable
# narration_phrase, treat it as a systemic remap response defect (fail loud)
# rather than silently letting every affected beat fall back to proportional
# timing one by one.
_CHILD_REMAP_HINT_MISSING_FAIL_RATIO = 0.3

# Child Short beats render vertically (Short.tsx, 1080x1920) — every beat's
# Flux image must be generated at this size (roadmap 3.1 / audit V-2, G-4.2),
# never the parent's landscape default. image_router.build_cache_key_material()
# disambiguates this from the parent's landscape cache by size.
_SHORT_IMAGE_WIDTH = 1080
_SHORT_IMAGE_HEIGHT = 1920

# ── Short episode storyboard remap (secondary model) ───────────────────────────

# Every child Short beat's image is regenerated fresh at portrait size — the
# match_score decides PROMPT INHERITANCE only (parent beat's image prompt vs. a
# new prompt written here), never physical image reuse (fresh full-system audit
# §3.1 — the former reuse-budget machinery measured a "reuse" that portrait
# regeneration had already made impossible).
_SHORT_REMAP_SYSTEM_PROMPT = (
    "You map a parent video's visual beats to a Short episode narration.\n\n"
    "You receive:\n"
    "- A Short narration text (60-90 seconds of standalone content)\n"
    "- A compact index of parent video beats: beat_order, visual_intent, environment, motif\n"
    "- Optional global visual direction / image style lines (the channel's configured look)\n\n"
    "Every Short beat's image is generated fresh in vertical/portrait format. Your\n"
    "match_score decides which IMAGE PROMPT that generation uses: a high score inherits\n"
    "the parent beat's existing prompt (visual continuity with the parent video); a low\n"
    "score means you write a new portrait prompt for this phrase.\n\n"
    "Divide the narration into phrases of roughly 3-5 seconds each. "
    "For each phrase, assign the most thematically relevant parent beat.\n\n"
    "For each assignment:\n"
    "- narration_phrase: the exact narration text this beat covers (verbatim excerpt)\n"
    "- long_beat_order: integer index from the parent beat index that best matches this phrase\n"
    "- match_score: 0-100 integer\n"
    "  90-100: Same subject and setting — inherit the parent prompt\n"
    "  70-89:  Compatible subject and mood — inheriting the parent prompt is appropriate\n"
    "  50-69:  Loosely related, different context — a new prompt is better\n"
    "  0-49:   Unrelated — a new prompt is required\n"
    "- new_image_prompt: for match_score below 70, a complete portrait-safe Flux image prompt "
    "for this phrase; for 70+ inheritance candidates, return an empty string unless "
    "the phrase would still benefit from a custom image\n"
    "- beat_intensity: pacing intent for this phrase\n"
    "  'high':   revelation, shock, key fact\n"
    "  'medium': story progression, context\n"
    "  'low':    setup, establishing, pause\n\n"
    "Rules:\n"
    "- Multiple phrases may share the same long_beat_order\n"
    "- Prefer variety: if two phrases have similar scores, pick different parent beats\n"
    "- new_image_prompt must describe a concrete vertical/portrait physical scene: subject, "
    "place, composition, lighting; no mood-only words, no readable text, no raw narration "
    "copied as a sentence. When global visual direction / image style lines are present, "
    "write the prompt in that style (matching the parent prompts' look)\n"
    "- Return ONLY valid JSON. No markdown. No code fence. No extra keys.\n"
    "PROMPT_VERSION = \"2.0\""
)

_SHORT_REMAP_MAX_TOKENS = 4096

_SHORT_REMAP_SCHEMA: dict = {
    "type": "object",
    "required": ["assignments"],
    "properties": {
        "assignments": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["narration_phrase", "long_beat_order", "beat_intensity", "match_score", "new_image_prompt"],
                "properties": {
                    "narration_phrase": {"type": "string"},
                    "long_beat_order":  {"type": "integer"},
                    "new_image_prompt": {"type": "string"},
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
# Static fallback recalibrated from 150 -> 135 wpm (roadmap 3.8 / audit G-7 —
# real measured parent narration ran at ~135 wpm). split_into_beats() prefers
# script_estimator.get_calibrated_wpm() (real per-language measured rate) when
# a db session is available and enough samples exist, falling back to this
# constant otherwise. This never changes the real per-segment
# target_beat_count, which is always computed from the segment's actual share
# of the real measured audio duration_ms, not from wpm — see
# _estimate_beat_count()'s docstring.
_BEAT_SECONDS_BY_FORMAT: dict[str, float] = {"youtube_long": 4.0}
_DEFAULT_BEAT_SECONDS = 3.0
_WORDS_PER_MINUTE = 135

# Diagnostic-only estimate of serialized size per beat at the reduced 13-field
# schema (~617 chars/beat ≈ 154 tokens/beat at the project's chars/4 heuristic —
# see Storyboard Size Analysis). Used solely to log an "estimated total output
# tokens" figure alongside the real ``total_output_tokens`` for comparison.
_STORYBOARD_TOKENS_PER_BEAT_LOG = 154

_ENV_OVERUSE_THRESHOLD = 0.35
_MOTIF_WINDOW_SIZE = 10
_MOTIF_MAX_PER_WINDOW = 2

# Maximum target_beat_count for a single storyboard batch call, checked in
# Python BEFORE any Claude call is made. A segment above this is split into
# multiple sub-calls (see _split_segment_for_batching). Chosen from observed
# production behavior: a 21-beat segment used 7286/8192 output tokens (89%,
# the closest successful call on record); a 27-beat segment truncated at
# max_tokens=8192 on the first attempt AND on the in-call retry (reduced to
# 24 beats) — the fixed -3-beat retry in generate_storyboard_batch() is not
# enough to recover an oversized segment. 20 stays safely under the observed
# failure point while only rarely triggering a split for normal segments.
_MAX_BEATS_PER_BATCH = 20

# ── Deterministic continuity line (Elimination Mandate, D2.1) ───────────────
# Replaces the entire visual-bible layer (Claude generation call, compaction,
# injection, cinematic-prompt enrichment, first-15 enhancement/validation) —
# code_report/forensic_output_audit_borrasca_run.md: a real production run
# showed the bible return 0 locations, 0 recurring motifs, and 0 first-15
# rules, a total no-op that still cost a Claude call and tokens on every
# storyboard batch. This is the one continuity signal genuinely worth
# carrying forward: proper nouns (character names, named places/objects)
# that recur across the blueprint's own text — built in pure Python, no AI
# call, no persisted artifact.
_CONTINUITY_ENTITY_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
_CONTINUITY_STOPWORDS = frozenset({
    "The", "This", "That", "These", "Those", "There", "Then", "But", "And",
    "For", "Not", "Now", "When", "What", "Why", "How", "Where", "Who",
    "Would", "Could", "Should", "Every", "Some", "Someone", "Something",
    "Years", "Each", "Once", "After", "Before", "Until", "While", "Even",
    "Still", "Just", "Only", "Never", "Always", "Maybe", "Perhaps", "Their",
    "They", "She", "His", "Her", "Him", "Its", "Instead", "Inside", "Outside",
})
_CONTINUITY_MIN_REPEATS = 2
_CONTINUITY_MAX_ENTITIES = 5


def build_continuity_line_from_blueprint(blueprint: dict | None) -> str:
    """Return a one-line visual-continuity hint built deterministically from
    the story blueprint, or "" if nothing recurs. No AI call — see the module
    note above this function for why the AI-generated visual bible it
    replaces was deleted.
    """
    if not blueprint:
        return ""
    text_fields = [
        str(blueprint.get("hook") or ""),
        str(blueprint.get("final_payoff") or ""),
        str(blueprint.get("midpoint_retention_trap") or ""),
        str(blueprint.get("central_question") or ""),
        *[str(turn) for turn in (blueprint.get("major_turns") or [])],
    ]
    combined = " ".join(field for field in text_fields if field)
    if not combined:
        return ""

    counts: dict[str, int] = {}
    for word in _CONTINUITY_ENTITY_RE.findall(combined):
        if word in _CONTINUITY_STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1

    recurring = sorted(
        (word for word, count in counts.items() if count >= _CONTINUITY_MIN_REPEATS),
        key=lambda word: (-counts[word], word),
    )[:_CONTINUITY_MAX_ENTITIES]
    if not recurring:
        return ""

    return (
        "Visual continuity: these names recur throughout this story — give "
        "each a stable, consistent physical identity/appearance across beats: "
        + ", ".join(recurring) + "."
    )


def split_into_beats(
    voice_script: str,
    duration_ms: int,
    channel,
    script_format: str,
    whisper_transcript: list[dict],
    allow_legacy_fallback: bool = False,
    language: str = "en",
    visual_style: str = "",
    image_style: str = "",
    continuity_line: str = "",
    content_id: str | None = None,
    db: Session | None = None,
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
        continuity_line: Optional deterministic continuity hint (see
            ``build_continuity_line_from_blueprint()``) forwarded to every
            storyboard batch as a plain line — no AI call, no compaction.
        content_id: Optional caller-supplied content id used only in telemetry
            logs such as ``PARENT_VISUAL_HOLD_CAP_APPLIED``.
        db: Optional session; when provided, the diagnostic "estimated beat
            count" log uses the real per-language calibrated wpm
            (``script_estimator.get_calibrated_wpm()``, roadmap 3.8) instead
            of the static fallback. Never affects the real per-segment
            ``target_beat_count`` sent to Claude, which is always derived
            from the segment's share of the real measured ``duration_ms``.

    Returns:
        List of renderable beat-section dicts, or ``None`` if storyboard generation
        failed or returned no usable beats — the caller (``_run_visual_pass()``)
        always fails loud for that language on ``None`` (roadmap 6.3 removed the
        legacy section-splitter fallback that used to be gated behind
        ``allow_legacy_fallback``).
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
    # Diagnostic-only wpm (never feeds the real target_beat_count below, which
    # is always derived from measured duration_ms) — prefers the real
    # per-language calibrated rate when db is available and enough samples
    # exist, falling back to the static _WORDS_PER_MINUTE otherwise
    # (roadmap 3.8 / audit G-7).
    # split_into_beats() only ever runs on the parent path (child Shorts use
    # remap_beats_for_short), so scope the calibration window to parent rows.
    diagnostic_wpm = (
        get_calibrated_wpm(db, language, is_short_episode=False)
        if db is not None else _WORDS_PER_MINUTE
    )
    estimated_beats = _estimate_beat_count(voice_script, script_format, wpm=diagnostic_wpm)
    logger.info(
        "Storyboard generation start: schema_version=%s language=%s segments=%d "
        "estimated_beat_count=%d (estimated from %d words at ~%.0fs/beat)",
        _STORYBOARD_SCHEMA_VERSION_LOG, language, len(segments), estimated_beats,
        total_words, beat_seconds,
    )
    logger.debug(
        "STORYBOARD_ESTIMATE script_words=%d estimated_beats=%d estimated_formula_used=%s",
        total_words, estimated_beats,
        f"words({total_words})/WPM({diagnostic_wpm:.0f})*60/beat_sec({beat_seconds:.1f})",
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

    # Cross-segment continuity ledger: tracks cumulative environment/motif counts
    # and recent visual choices across all batches to give Claude global
    # repetition context and hard diversity constraints for the next batch.
    ledger: dict = {
        "env_counts": {},
        "motif_counts": {},
        "recent_envs": [],
        "recent_visual_types": [],
        "recent_motifs": [],
        "total_beats": 0,
    }

    for index, (label, text) in enumerate(segments, start=1):
        # Per-segment target beat count from proportional audio duration
        seg_words = max(len(text.split()), 1)
        seg_duration_sec = (seg_words / total_words) * (duration_ms / 1000)
        target_beat_count = max(1, round(seg_duration_sec / beat_seconds))

        # Oversized segments are split into multiple sub-calls BEFORE any Claude
        # call — see _split_segment_for_batching for why the in-call truncation
        # retry alone isn't sufficient. Normal-sized segments get back a single
        # (label, text, target_beat_count) tuple, so this loop is a no-op change
        # in shape for the common case.
        for sub_label, sub_text, sub_target_beat_count in _split_segment_for_batching(
            label, text, target_beat_count
        ):
            try:
                storyboard, usage, diag = generate_storyboard_batch(
                    segment_label=sub_label,
                    segment_text=sub_text,
                    segment_index=index,
                    segment_count=len(segments),
                    channel=channel,
                    script_format=script_format,
                    previous_segment_summary=previous_summary,
                    target_beat_count=sub_target_beat_count,
                    visual_style=visual_style,
                    image_style=image_style,
                    continuity_line=continuity_line,
                )
            except Exception as exc:
                logger.error(
                    "Storyboard batch failed for segment %s (%d/%d) — aborting storyboard "
                    "generation entirely (fail-loud: a partial storyboard would leave gaps "
                    "in the narration with no designed visuals): %s",
                    sub_label, index, len(segments), exc,
                )
                return None

            total_output_tokens      += usage.get("output_tokens", 0)
            total_input_tokens       += diag.get("input_tokens", 0)
            total_generation_time_ms += diag.get("elapsed_ms", 0)
            total_claude_calls       += diag.get("attempt_count", 1)
            if diag.get("was_truncated"):
                _truncation_count += 1
                _retry_count      += 1
            _requested_beats += sub_target_beat_count
            beats = storyboard.get("beats") or []
            if not beats:
                logger.warning(
                    "Storyboard batch for segment %s (%d/%d) returned no beats — aborting "
                    "storyboard generation entirely",
                    sub_label, index, len(segments),
                )
                return None

            # Hint hardening: fix any hints that are out-of-range or contain digits
            beats, _seg_hint_stats = _harden_hints(beats, sub_text)
            _hint_total   += _seg_hint_stats["total_hints"]
            _hint_valid   += _seg_hint_stats["valid_hints"]
            _hint_invalid += _seg_hint_stats["invalid_hints"]

            beats = _trim_beat_count_overshoot(
                beats,
                target_beat_count=sub_target_beat_count,
                segment_label=sub_label,
            )

            logger.info(
                "Storyboard batch ok: segment=%s (%d/%d) target_beats=%d actual_beats=%d output_tokens=%d/%d",
                sub_label, index, len(segments), sub_target_beat_count, len(beats),
                usage.get("output_tokens", 0), _STORYBOARD_BATCH_MAX_TOKENS_LOG,
            )
            overall_style = overall_style or storyboard.get("overall_style", "")

            raw_batches.append(beats)

            # Update ledger then build the next sub-call's (or next segment's)
            # continuity summary — sub-calls of one oversized segment get the
            # same continuity treatment as separate segments.
            _update_ledger(ledger, beats)
            previous_summary = _summarize_batch_for_continuity(sub_label, beats, ledger)

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
        max_hold_ms=_PARENT_VISUAL_MAX_HOLD_MS,
    )
    if mapped is None:
        return None

    # Last-line frozen-frame guarantee for the parent path: even if a bad
    # anchor slips past the proximity window AND the span sanity check, no
    # non-terminal beat may hold one image beyond the configured ceiling.
    return _apply_visual_hold_cap(
        mapped,
        max_hold_ms=_PARENT_VISUAL_MAX_HOLD_MS,
        content_id=content_id,
        language=language,
        log_prefix="PARENT_VISUAL_HOLD_CAP",
    )


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


def _split_segment_for_batching(
    label: str, text: str, target_beat_count: int
) -> list[tuple[str, str, int]]:
    """Split an oversized segment into sub-calls before any Claude call is made.

    A segment whose ``target_beat_count`` exceeds ``_MAX_BEATS_PER_BATCH`` is
    split into roughly equal word-count chunks, snapped to sentence boundaries
    where possible, so each sub-call's own target beat count stays comfortably
    under ``STORYBOARD_BATCH_MAX_TOKENS``. This is a preventive Python check,
    not a reaction to truncation — ``generate_storyboard_batch()``'s in-call
    retry only reduces beat count by 3, which was not enough for a real
    production segment (target_beat_count=27 truncated at max_tokens=8192 both
    on the first attempt and on the reduced retry at 24).

    Each returned sub-segment's text is a verbatim contiguous substring of
    ``text`` (same guarantee as ``_split_voice_script_into_segments``), so
    start_hint/end_hint generated from it remain valid forward-search targets
    against the full Whisper transcript.

    Returns:
        Ordered list of ``(sub_label, sub_text, sub_target_beat_count)``
        tuples. Returns ``[(label, text, target_beat_count)]`` unchanged when
        no split is needed.
    """
    if target_beat_count <= _MAX_BEATS_PER_BATCH:
        return [(label, text, target_beat_count)]

    num_parts = math.ceil(target_beat_count / _MAX_BEATS_PER_BATCH)
    words = text.split()
    total_words = max(len(words), 1)
    chunk_words = math.ceil(total_words / num_parts)

    # Sentence-end positions (character offsets right after ". "/"! "/"? ") to
    # snap split points to, so beats are never designed around a cut sentence.
    sentence_ends = [m.end() for m in re.finditer(r"[.!?]\s+", text)]

    parts: list[tuple[str, str, int]] = []
    start_char = 0
    for part_index in range(num_parts):
        if part_index == num_parts - 1:
            sub_text = text[start_char:].strip()
        else:
            target_word_count = chunk_words * (part_index + 1)
            approx_char = len(" ".join(words[:target_word_count]))
            snap = next((p for p in sentence_ends if p >= approx_char), None)
            split_at = snap if snap is not None else approx_char
            split_at = max(split_at, start_char + 1)  # guarantee forward progress
            sub_text = text[start_char:split_at].strip()
            start_char = split_at
        if not sub_text:
            continue
        sub_words = max(len(sub_text.split()), 1)
        sub_target = max(1, round(target_beat_count * sub_words / total_words))
        parts.append((f"{label} (part {part_index + 1}/{num_parts})", sub_text, sub_target))

    logger.warning(
        "STORYBOARD_SEGMENT_SPLIT segment=%s target_beat_count=%d > max_per_batch=%d "
        "— splitting into %d sub-calls before any Claude call",
        label, target_beat_count, _MAX_BEATS_PER_BATCH, len(parts),
    )
    return parts


def _estimate_beat_count(voice_script: str, script_format: str, wpm: float = _WORDS_PER_MINUTE) -> int:
    """Estimate the expected total beat count from word count and the prompt's pacing rule.

    Diagnostic-only (Storyboard Failure Analysis, Task 1) — a rough sanity figure
    logged alongside the actual merged beat count so a future pacing or schema
    change that risks reintroducing the token-overflow failure is visible immediately.
    Never feeds the real per-segment target_beat_count, which is always derived
    from the segment's actual share of the real measured ``duration_ms``
    (roadmap 3.8 / audit G-7 — confirmed the real math already ignores wpm
    entirely; only this diagnostic estimate used the static assumption).

    Args:
        wpm: Narration rate to use for the estimate. Defaults to the static
             ``_WORDS_PER_MINUTE`` fallback; callers with a calibrated
             per-language rate (``script_estimator.get_calibrated_wpm()``)
             should pass it here so the log stays accurate as real data
             accumulates.
    """
    words = len(voice_script.split())
    narration_seconds = (words / wpm) * 60
    beat_seconds = _BEAT_SECONDS_BY_FORMAT.get(script_format, _DEFAULT_BEAT_SECONDS)
    return max(int(narration_seconds / beat_seconds), 1)




def _update_ledger(ledger: dict, beats: list[dict]) -> None:
    """Update the cross-segment continuity ledger with the beats from one batch."""
    ledger.setdefault("env_counts", {})
    ledger.setdefault("motif_counts", {})
    ledger.setdefault("recent_envs", [])
    ledger.setdefault("recent_visual_types", [])
    ledger.setdefault("recent_motifs", [])
    ledger.setdefault("total_beats", 0)

    for b in beats:
        env = b.get("environment", "other")
        vt = b.get("visual_type", "b-roll")
        motif = b.get("motif", "other")
        ledger["env_counts"][env] = ledger["env_counts"].get(env, 0) + 1
        ledger["motif_counts"][motif] = ledger["motif_counts"].get(motif, 0) + 1
        ledger["recent_envs"].append(env)
        ledger["recent_visual_types"].append(vt)
        ledger["recent_motifs"].append(motif)
        ledger["total_beats"] += 1
    # Keep only the last 10 for the "recent" window and motif-window cap.
    ledger["recent_envs"] = ledger["recent_envs"][-10:]
    ledger["recent_visual_types"] = ledger["recent_visual_types"][-10:]
    ledger["recent_motifs"] = ledger["recent_motifs"][-10:]


def _overused_environments(ledger: dict) -> list[str]:
    total = max(int(ledger.get("total_beats") or 0), 1)
    return sorted(
        env for env, count in (ledger.get("env_counts") or {}).items()
        if count / total > _ENV_OVERUSE_THRESHOLD
    )


def _overused_motifs(ledger: dict) -> list[str]:
    recent = list((ledger.get("recent_motifs") or [])[-_MOTIF_WINDOW_SIZE:])
    return sorted({
        motif for motif in recent
        if motif and recent.count(motif) > _MOTIF_MAX_PER_WINDOW
    })


def _summarize_batch_for_continuity(label: str, beats: list[dict], ledger: dict) -> str:
    """Build a continuity/diversity note for the next segment's prompt.

    Includes the closing 3 beats' descriptors, cumulative environment/motif
    distribution, and hard next-segment diversity constraints when the ledger
    crosses the validator thresholds.
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
        top_motifs = sorted(ledger.get("motif_counts", {}).items(), key=lambda x: -x[1])[:4]
        motif_str = ", ".join(f"{m}×{c}" for m, c in top_motifs)
        recent_vt = ", ".join(ledger["recent_visual_types"][-6:])
        closing += (
            f". Video-wide env totals (avoid most-used): {env_str}. "
            f"Video-wide motif totals (avoid most-used): {motif_str}. "
            f"Last 6 visual_types: {recent_vt}"
        )

    forbidden_envs = _overused_environments(ledger)
    forbidden_motifs = _overused_motifs(ledger)
    if forbidden_envs:
        closing += (
            ". FORBIDDEN environments for the next segment: "
            + ", ".join(forbidden_envs)
            + ". Choose different environment values unless the narration explicitly makes one unavoidable"
        )
    if forbidden_motifs:
        closing += (
            ". FORBIDDEN motifs for the next segment: "
            + ", ".join(forbidden_motifs)
            + ". Choose different motif values unless the narration explicitly makes one unavoidable"
        )
    if forbidden_envs or forbidden_motifs:
        logger.warning(
            "STORYBOARD_DIVERSITY_CONSTRAINTS label=%s forbidden_envs=%s forbidden_motifs=%s total_beats=%d",
            label, forbidden_envs, forbidden_motifs, ledger.get("total_beats", 0),
        )
    return closing


def _harden_hints(beats: list[dict], segment_text: str) -> tuple[list[dict], dict]:
    """Log quality warnings for start_hint/end_hint that violate verbatim-word rules.

    Rules Claude is required to follow (enforced in the schema prompt):
      - 6–10 verbatim words copied from the narration
      - no digit characters
      - no marker text (INTRO/OUTRO/SECTION)

    When a hint violates these rules we log a WARNING and leave it unchanged.
    The matching pipeline (_locate_phrase) handles unmatched beats via real
    anchor timestamps from adjacent matched beats — proportional text
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


def _trim_beat_count_overshoot(
    beats: list[dict],
    *,
    target_beat_count: int,
    segment_label: str,
    tolerance: int = 2,
) -> list[dict]:
    """Trim storyboard beats that exceed the requested target plus tolerance.

    The storyboard prompt says "aim for target, +/-2", but Claude can return
    far more beats. This is a deterministic tail trim, not a regeneration loop:
    keep the earliest beats in narration order and log the discarded local beat
    orders before downstream merge/timestamp mapping sees them.
    """
    target = max(0, int(target_beat_count or 0))
    allowed = target + max(0, int(tolerance))
    if allowed <= 0 or len(beats) <= allowed:
        return beats

    kept = beats[:allowed]
    trimmed = beats[allowed:]
    logger.warning(
        "STORYBOARD_BEAT_COUNT_OVERSHOOT_TRIMMED segment=%s target_beats=%d "
        "allowed_beats=%d generated_beats=%d trimmed_count=%d trimmed_local_orders=%s",
        segment_label,
        target,
        allowed,
        len(beats),
        len(trimmed),
        [beat.get("beat_order", idx + allowed) for idx, beat in enumerate(trimmed)],
    )
    return kept


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
    max_hold_ms: int | None = None,
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
        max_hold_ms:          Optional visual hold ceiling used for fail-loud
            density checks after micro-beat cleanup. Callers still apply the
            actual cap after mapping.

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

    # Expected start of the current beat: last real anchor plus the suggested
    # durations of every beat since it (including unmatched ones). Bounds the
    # forward hint search so a repeated narration phrase minutes downstream can
    # never capture this beat's anchor (see _HINT_SEARCH_WINDOW_MS above).
    expected_ms: float = 0.0

    for i, beat in enumerate(beats):
        suggested_ms = float(beat.get("suggested_duration_sec") or 3.0) * 1000.0
        located, start_hint_end_idx = _locate_beat_span(
            flat, cursor, str(beat.get("start_hint", "")), str(beat.get("end_hint", "")),
            language=language,
            expected_start_ms=expected_ms,
        )
        if located is None:
            expected_ms += suggested_ms
            continue
        matches[i] = located
        # Re-sync expectation to the real anchor so estimation error never accumulates.
        expected_ms = float(flat[located[0]][2]) + suggested_ms
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

    # Span sanity (second line of defense behind the proximity window): demote
    # any anchor whose distance from the last trusted anchor cannot be covered
    # by the beats between them — see _SPAN_SANITY_FACTOR. Runs before the
    # stats below so demoted beats are counted honestly as fallbacks, and
    # before script_text extraction so a wrong match's text never ships.
    _demote_out_of_span_anchors(matches, match_type, beats, flat)

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
        logger.warning(
            "STORYBOARD_SCRIPT_TEXT_EMPTY_FALLBACK language=%s count=%d beat_orders=%s "
            "reason=no_transcript_span_match",
            language, n_fallback, fallback_orders,
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

    boundaries, micro_stats = _resolve_boundaries(
        matches, beats, flat, duration_ms, return_stats=True,
    )
    if not _passes_micro_merge_invariants(
        micro_stats, duration_ms, max_hold_ms=max_hold_ms, language=language,
    ):
        return None

    sections: list[dict] = []
    for i, beat in enumerate(beats):
        start_ms, end_ms = boundaries[i]
        match = matches[i]
        if match is not None:
            script_text = _join_words(flat, match[0], match[1])
            script_text_source = "whisper_transcript"
            script_text_missing = False
        else:
            script_text = ""
            script_text_source = "empty_fallback_no_transcript_span"
            script_text_missing = True
        beat_for_section = {
            **beat,
            "script_text_source": script_text_source,
            "script_text_missing": script_text_missing,
        }
        sections.append(_build_beat_section(beat_for_section, i, start_ms, end_ms, script_text))

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
    expected_start_ms: float | None = None,
) -> tuple[tuple[int, int] | None, int]:
    """Locate a beat's inclusive ``[start_idx, end_idx]`` token span in the transcript.

    Args:
        expected_start_ms: Where this beat is expected to start (last real anchor
            plus suggested durations since it). When set, hint matches are only
            accepted within the proximity window past this position — see
            ``_HINT_SEARCH_WINDOW_MS``. ``None`` disables the bound.

    Returns:
        ``(span, start_hint_end_idx)`` where ``span`` is ``(start_idx, end_idx)``
        or ``None`` on failure, and ``start_hint_end_idx`` is the last token index
        of the start_hint match (used by the caller to advance the cursor without
        overshooting into subsequent beats' territory).
    """
    start_match = _locate_phrase(
        flat, cursor, start_hint, language=language, expected_start_ms=expected_start_ms
    )
    if start_match is None:
        return None, cursor          # unchanged cursor on failure

    start_idx       = start_match[0]
    start_hint_end  = start_match[1]   # cursor advances HERE, not to end_hint end

    end_match = _locate_phrase(
        flat, start_idx, end_hint, language=language, expected_start_ms=expected_start_ms
    )
    if end_match is None:
        return (start_idx, start_hint_end), start_hint_end

    return (start_idx, max(end_match[1], start_idx)), start_hint_end


def _locate_phrase(
    flat: list[tuple[str, str, int, int]],
    from_idx: int,
    phrase: str,
    language: str = "en",
    expected_start_ms: float | None = None,
) -> tuple[int, int] | None:
    """Find a phrase forward from ``from_idx``, trying shrinking prefixes for fuzzy tolerance.

    When ``expected_start_ms`` is set, every candidate match must start within a
    proximity window past the expected position: ``_HINT_SEARCH_WINDOW_MS`` for
    full/5-token candidates, the much tighter ``_HINT_SHORT_PREFIX_WINDOW_MS``
    for candidates of ``_HINT_SHORT_PREFIX_MAX_TOKENS`` tokens or fewer — a
    3-token phrase is too ambiguous to be trusted far from where the beat is
    expected (shrink-prefix demotion). The window applies by candidate LENGTH,
    not cascade position: a hint whose full form is only 3 tokens is exactly as
    ambiguous as a 3-token prefix and gets the tight window too.

    Returns:
        Inclusive ``(start_idx, end_idx)`` token-index span, or ``None`` if not
        found within the window.
    """
    tokens = _normalize_phrase(phrase, language)
    if not tokens:
        return None

    for prefix_len in _PREFIX_LENGTHS:
        candidate = tokens if prefix_len is None else tokens[:prefix_len]
        if not candidate:
            continue
        if expected_start_ms is None:
            max_start_ms = None
        elif len(candidate) <= _HINT_SHORT_PREFIX_MAX_TOKENS:
            max_start_ms = int(expected_start_ms + _HINT_SHORT_PREFIX_WINDOW_MS)
        else:
            max_start_ms = int(expected_start_ms + _HINT_SEARCH_WINDOW_MS)
        found = _search_subsequence(flat, from_idx, candidate, max_start_ms=max_start_ms)
        if found is not None:
            start_idx, matched_len = found
            return (start_idx, start_idx + matched_len - 1)

    # Diagnostics: did the window reject a match the unbounded search would have
    # taken? This is exactly the failure mode the window exists to prevent (a
    # repeated phrase far downstream) — log it so the guard's firing is visible
    # in production logs, then fail the locate so the beat degrades locally.
    if expected_start_ms is not None and flat:
        stray = _search_subsequence(flat, from_idx, tokens)
        if stray is None and len(tokens) > _HINT_SHORT_PREFIX_MAX_TOKENS:
            stray = _search_subsequence(
                flat, from_idx, tokens[:_HINT_SHORT_PREFIX_MAX_TOKENS]
            )
        if stray is not None:
            logger.warning(
                "HINT_MATCH_REJECTED_OUT_OF_WINDOW phrase=%r match_ms=%d "
                "expected_ms=%d window_ms=%d short_prefix_window_ms=%d "
                "— distant duplicate phrase ignored; beat falls back locally",
                phrase[:60], flat[stray[0]][2], int(expected_start_ms),
                _HINT_SEARCH_WINDOW_MS, _HINT_SHORT_PREFIX_WINDOW_MS,
            )

    return None


def _search_subsequence(
    flat: list[tuple[str, str, int, int]],
    from_idx: int,
    tokens: list[str],
    max_start_ms: int | None = None,
) -> tuple[int, int] | None:
    """Find the first forward contiguous occurrence of ``tokens``.

    Args:
        max_start_ms: When set, a match may not START on a token whose start_ms
            exceeds this value. Whisper tokens are time-ordered, so the scan
            stops early instead of walking the rest of the transcript.

    Returns:
        ``(start_idx, length)`` of the match, or ``None``.
    """
    limit = len(flat) - len(tokens) + 1
    for i in range(max(from_idx, 0), max(limit, 0)):
        if max_start_ms is not None and flat[i][2] > max_start_ms:
            return None  # time-ordered — nothing further can qualify
        if all(flat[i + j][0] == tokens[j] for j in range(len(tokens))):
            return (i, len(tokens))
    return None


def _join_words(flat: list[tuple[str, str, int, int]], start_idx: int, end_idx: int) -> str:
    return " ".join(flat[i][1] for i in range(start_idx, end_idx + 1))


def _suggested_ms(beat: dict) -> float:
    """A beat's suggested duration in ms (3 s default — same as the mapping loop)."""
    return float(beat.get("suggested_duration_sec") or 3.0) * 1000.0


def _demote_out_of_span_anchors(
    matches: list[tuple[int, int] | None],
    match_type: list[str],
    beats: list[dict],
    flat: list[tuple[str, str, int, int]],
) -> int:
    """Demote anchors whose implied span cannot be real (mutates matches/match_type).

    Walks trusted anchors left to right, starting from a virtual anchor at
    narration start (0 ms). For each candidate anchor at beat ``j``, the gap
    from the last trusted anchor must be coverable by the beats between them
    (``last_trusted .. j-1``): a gap above
    ``max(_SPAN_SANITY_FACTOR × sum(suggested), _SPAN_SANITY_MIN_MS)`` means
    the candidate matched the wrong occurrence of its hint — accepting it
    would freeze the previous beat's image for the whole gap (run 9500c231
    beat 50: 285 s on one frame). The candidate is demoted to fallback so
    ``_resolve_boundaries`` interpolates it between trusted anchors instead.

    Returns:
        Number of anchors demoted.
    """
    n = len(beats)
    demoted = 0
    last_idx = -1   # virtual trusted anchor: narration starts at 0 ms
    last_ms = 0.0

    for j in range(n):
        if matches[j] is None:
            continue
        anchor_ms = float(flat[matches[j][0]][2])
        covering = range(last_idx if last_idx >= 0 else 0, j)
        expected_ms = sum(_suggested_ms(beats[k]) for k in covering)
        threshold_ms = max(_SPAN_SANITY_FACTOR * expected_ms, _SPAN_SANITY_MIN_MS)
        gap_ms = anchor_ms - last_ms

        if gap_ms > threshold_ms:
            logger.warning(
                "ANCHOR_SPAN_SANITY_DROPPED beat_order=%s anchor_ms=%d "
                "last_trusted_ms=%d gap_ms=%d expected_ms=%d threshold_ms=%d "
                "— anchor would freeze the previous beat for the whole gap; "
                "demoting to fallback for proportional interpolation",
                beats[j].get("beat_order", j), int(anchor_ms), int(last_ms),
                int(gap_ms), int(expected_ms), int(threshold_ms),
            )
            matches[j] = None
            match_type[j] = "fallback"
            demoted += 1
            continue

        last_idx, last_ms = j, anchor_ms

    return demoted


def _resolve_boundaries(
    matches: list[tuple[int, int] | None],
    beats: list[dict],
    flat: list[tuple[str, str, int, int]],
    duration_ms: int,
    *,
    return_stats: bool = False,
) -> list[tuple[int, int]] | tuple[list[tuple[int, int]], _MicroMergeStats]:
    """Turn matched/unmatched beat spans into a monotonic, bounds-clean ms timeline.

    Matched beats anchor their start to the matched phrase's start_ms. Unmatched
    beats are interpolated proportionally (weighted by their
    ``suggested_duration_sec``) between the surrounding trusted anchors — the
    leading run interpolates from 0 ms to the first anchor, the trailing run
    from the last anchor to ``duration_ms``, and a storyboard with no anchor
    at all tiles the full duration proportionally.

    History: proportional interpolation existed here before (``_fill_gaps``),
    was removed in Phase 10A-0, and "inherit the previous anchor" became the
    only fallback. That inheritance is exactly what crammed ~50 desynced beats
    into floor-width slots after one bad anchor in run 9500c231 (audit finding
    G-1) — the last unmatched beat before the next anchor absorbed the whole
    remaining gap as a frozen frame. Interpolation distributes a gap across
    the beats that must cover it, so no single beat can absorb it.

    Guarantees (enforced by ``_cleanup_micro_beats`` after boundary assembly):
    - ``audio_end_ms > audio_start_ms`` for every beat (intensity-aware floor)
    - strictly monotonic: each beat's start >= previous beat's end
    - first beat starts at 0, last beat ends exactly at ``duration_ms``
    - zero-width spans are corrected and logged
    """
    n = len(beats)
    if n == 0:
        return []

    anchors: list[int | None] = [
        flat[match[0]][2] if match is not None else None
        for match in matches
    ]
    trusted = [i for i, a in enumerate(anchors) if a is not None]

    starts: list[float] = [0.0] * n

    def _interpolate(lo: int, hi: int, ms_lo: float, ms_hi: float) -> None:
        """Assign starts for beats lo..hi-1 across [ms_lo, ms_hi), weighted by
        suggested duration. Beat lo keeps ms_lo (cumulative weight 0)."""
        weights = [_suggested_ms(beats[k]) for k in range(lo, hi)]
        total = sum(weights) or 1.0
        span = max(ms_hi - ms_lo, 0.0)
        acc = 0.0
        for offset, k in enumerate(range(lo, hi)):
            starts[k] = ms_lo + span * (acc / total)
            acc += weights[offset]

    if not trusted:
        _interpolate(0, n, 0.0, float(duration_ms))
    else:
        first = trusted[0]
        if first > 0:
            # Leading unmatched run: narration starts at 0, first anchor bounds it.
            _interpolate(0, first, 0.0, float(anchors[first]))
        for a, b in zip(trusted, trusted[1:]):
            # Beats a..b-1 cover [anchor_a, anchor_b); beat a keeps its anchor.
            _interpolate(a, b, float(anchors[a]), float(anchors[b]))
        last = trusted[-1]
        # Trailing run (including the last trusted beat itself): cover to the end.
        _interpolate(last, n, float(anchors[last]), float(duration_ms))

    int_starts = [int(round(s)) for s in starts]
    boundaries: list[tuple[int, int]] = []
    for i in range(n):
        end_ms = int_starts[i + 1] if i + 1 < n else duration_ms
        boundaries.append((int_starts[i], end_ms))

    if boundaries:
        last_start, _ = boundaries[-1]
        boundaries[-1] = (last_start, max(duration_ms, last_start))

    stats = _cleanup_micro_beats(boundaries, duration_ms, beats)
    if return_stats:
        return boundaries, stats
    return boundaries


def _cleanup_micro_beats(
    boundaries: list[tuple[int, int]],
    duration_ms: int,
    beats: list[dict] | None = None,
) -> _MicroMergeStats:
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
        return _MicroMergeStats(0, 0, 0, 0.0, 0)

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

    # Clamp every span to the real audio duration. Earlier versions only
    # clamped the last beat, which let micro-merged intermediate spans extend
    # beyond the track and hid how many beats had effectively been discarded.
    for idx, (start_ms, end_ms) in enumerate(boundaries):
        clamped_start = min(max(start_ms, 0), duration_ms)
        clamped_end = min(max(end_ms, clamped_start), duration_ms)
        boundaries[idx] = (clamped_start, clamped_end)

    positive_after = sum(1 for start, end in boundaries if end > start)
    discarded = max(0, n - positive_after)
    discard_ratio = discarded / n if n else 0.0

    if zero_width_corrected:
        logger.warning(
            "Storyboard timestamp mapping: %d beat(s) below intensity floor corrected",
            zero_width_corrected,
        )
    if discarded:
        logger.warning(
            "MICRO_MERGE_DISCARDED beats_before=%d beats_after=%d discarded=%d "
            "discard_ratio=%.1f%%",
            n, positive_after, discarded, discard_ratio * 100,
        )

    return _MicroMergeStats(n, positive_after, discarded, discard_ratio, zero_width_corrected)


def _passes_micro_merge_invariants(
    stats: _MicroMergeStats,
    duration_ms: int,
    *,
    max_hold_ms: int | None,
    language: str,
) -> bool:
    if stats.before <= 0:
        return False

    if stats.discard_ratio > _MICRO_MERGE_DISCARD_FAIL_RATIO:
        logger.error(
            "MICRO_MERGE_FAIL_DISCARD_RATIO language=%s beats_before=%d beats_after=%d "
            "discarded=%d discard_ratio=%.1f%% threshold=%.1f%%",
            language, stats.before, stats.after, stats.discarded,
            stats.discard_ratio * 100, _MICRO_MERGE_DISCARD_FAIL_RATIO * 100,
        )
        return False

    if max_hold_ms is not None:
        capacity_ms = stats.after * int(max_hold_ms)
        if capacity_ms < int(duration_ms):
            logger.error(
                "VISUAL_DENSITY_INVARIANT_FAILED language=%s beats=%d max_hold_ms=%d "
                "capacity_ms=%d duration_ms=%d reason=beats_times_max_hold_lt_duration",
                language, stats.after, int(max_hold_ms), capacity_ms, int(duration_ms),
            )
            return False

    return True


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

    # ── Subtitles-only rendering (audit G-0/G-8): every beat is a Flux image ──
    # media_strategy is hardcoded — the schema no longer carries it (v7.0), and
    # a legacy value arriving from a stored beat (remotion_text_card, stock_*)
    # is normalized here. visual_type "text_card" is likewise never produced.
    raw_incoming = str(beat.get("media_strategy", "") or "")
    if raw_incoming and raw_incoming != _DEFAULT_MEDIA_STRATEGY:
        logger.info(
            "Beat %d: legacy media_strategy=%r normalized to 'flux_generated' "
            "(subtitles-only rendering — text cards and stock are removed)",
            beat_order, raw_incoming,
        )
    raw_strategy = _DEFAULT_MEDIA_STRATEGY

    resolved_visual_type = _safe_enum(
        beat.get("visual_type"), _VALID_VISUAL_TYPES, _DEFAULT_VISUAL_TYPE
    )
    if resolved_visual_type == "text_card":
        resolved_visual_type = _DEFAULT_VISUAL_TYPE

    # ── Text-prop prompt sanitization (Phase 14.7, prompt half only) ────────
    # Beats whose subject is a document/poster/calendar/sign/name-tag prop get
    # their flux_prompt rewritten to forbid readable text in the generated
    # image. The overlay-derivation half was removed with subtitles-only
    # rendering — no readable text is ever drawn by Remotion except subtitles.
    flux_prompt_value = str(beat.get("flux_prompt", "") or "")

    if is_text_prop_beat(beat):
        sanitized_prompt = derive_text_prop_prompt(beat)
        logger.info(
            "TEXT_PROP_PROMPT_SANITIZED beat=%d original_prompt=%r sanitized_prompt=%r",
            beat_order, flux_prompt_value[:160], sanitized_prompt,
        )
        flux_prompt_value = sanitized_prompt

    # ── Dark-frame guard (audit G-6) ─────────────────────────────────────────
    # dark_contrast renders `contrast(140%) brightness(65%)` — on a source
    # image with no bright light it produces a near-black frame (two confirmed
    # on the run-9500c231 contact sheet). The storyboard prompt requires a
    # well-lit source for this grade; when the FINAL prompt (post-sanitization,
    # i.e. exactly what Flux receives) carries no bright-lighting evidence,
    # downgrade in place to a grade that stays visible. Safe fix, no retry.
    resolved_grade = _safe_enum(beat.get("color_grade"), _VALID_GRADES, _DEFAULT_GRADE)
    if resolved_grade == "dark_contrast" and not has_bright_lighting_evidence(flux_prompt_value):
        logger.info(
            "DARK_CONTRAST_GRADE_DOWNGRADED beat=%d reason=no_bright_lighting_keyword "
            "new_grade=desaturated prompt=%r",
            beat_order, flux_prompt_value[:160],
        )
        resolved_grade = "desaturated"

    return {
        "beat_order":           beat_order,
        "section_order":        beat_order,
        "audio_start_ms":       start_ms,
        "audio_end_ms":         end_ms,
        "duration_sec":         actual_sec,
        "script_text":          script_text,
        "script_text_source":   str(beat.get("script_text_source", "provided")),
        "script_text_missing":  bool(beat.get("script_text_missing", False)),
        "visual_intent":        str(beat.get("visual_intent", "")),
        "visual_type":          resolved_visual_type,
        "visual_category":      _safe_enum(beat.get("visual_category"), _VALID_VISUAL_CATEGORIES, _DEFAULT_VISUAL_CATEGORY),
        "environment":          _safe_enum(beat.get("environment"), _VALID_ENVIRONMENTS, _DEFAULT_ENVIRONMENT),
        "flux_prompt":          flux_prompt_value,
        "effect":               _safe_enum(beat.get("effect"), _VALID_EFFECTS, _DEFAULT_EFFECT),
        "color_grade":          resolved_grade,
        "transition_to_next":   _safe_enum(beat.get("transition_to_next"), _VALID_TRANSITIONS, _DEFAULT_TRANSITION),
        # Subtitles-only rendering: overlay/text-card fields are neutral
        # constants kept solely for DB column/loader compatibility — Remotion
        # never receives or renders them.
        "overlay_text":         "",
        "overlay_position":     "none",
        "motif":                _safe_enum(beat.get("motif"), _VALID_MOTIFS, _DEFAULT_MOTIF),
        "beat_intensity":       intensity,
        "suggested_duration_sec": suggested_sec,
        "media_strategy":       raw_strategy,
        "text_card_style":      _DEFAULT_TEXT_CARD_STYLE,
        "media_url":            beat.get("media_url", ""),
        "media_type":           beat.get("media_type", "image"),
        "start_hint":           str(beat.get("start_hint", "")),
        "end_hint":             str(beat.get("end_hint", "")),
    }


def _apply_short_visual_hold_cap(
    sections: list[dict],
    *,
    max_hold_ms: int | None = None,
    content_id: str | None = None,
    language: str | None = None,
) -> list[dict]:
    """Cap child Short visual exposure by advancing the next existing beat.

    Thin wrapper over ``_apply_visual_hold_cap`` preserving the child Short
    call contract (default cap from ``settings.short_visual_max_hold_ms``,
    ``SHORT_VISUAL_HOLD_CAP_*`` log names).
    """
    return _apply_visual_hold_cap(
        sections,
        max_hold_ms=int(max_hold_ms if max_hold_ms is not None else _SHORT_VISUAL_MAX_HOLD_MS),
        content_id=content_id,
        language=language,
        log_prefix="SHORT_VISUAL_HOLD_CAP",
    )


def _apply_visual_hold_cap(
    sections: list[dict],
    *,
    max_hold_ms: int,
    content_id: str | None = None,
    language: str | None = None,
    log_prefix: str = "VISUAL_HOLD_CAP",
) -> list[dict]:
    """Cap visual exposure by advancing the next existing beat (shared core).

    Used by both the child Short path (``_apply_short_visual_hold_cap``, 6 s
    default) and the parent long-form path (``split_into_beats``,
    ``settings.parent_visual_max_hold_ms``, 9 s default — the last-line
    frozen-frame guarantee behind the hint proximity window and the anchor
    span sanity check).

    This intentionally does not create synthetic beats or alter the audio/subtitle
    timeline. When a non-terminal beat exceeds the cap, its end is shortened and
    the next existing visual beat starts earlier. The terminal beat is left alone
    when no later beat exists to take over the remaining narration span.
    """
    if not sections:
        return []

    cap_ms = int(max_hold_ms)
    if cap_ms <= 0:
        logger.warning(
            "%s_SKIPPED content_id=%s language=%s reason=invalid_cap cap_ms=%d",
            log_prefix, content_id or "unknown", language or "unknown", cap_ms,
        )
        return [dict(section) for section in sections]

    capped = [dict(section) for section in sections]
    durations_before = [
        max(0, int(s.get("audio_end_ms", 0)) - int(s.get("audio_start_ms", 0)))
        for s in capped
    ]
    capped_count = 0

    for idx in range(len(capped) - 1):
        start_ms = int(capped[idx].get("audio_start_ms", 0))
        end_ms = int(capped[idx].get("audio_end_ms", start_ms))
        hold_ms = max(0, end_ms - start_ms)
        if hold_ms <= cap_ms:
            continue

        new_end_ms = start_ms + cap_ms
        capped[idx]["audio_end_ms"] = new_end_ms
        capped[idx + 1]["audio_start_ms"] = new_end_ms
        capped_count += 1

    terminal_uncapped = 0
    if capped:
        last = capped[-1]
        last_hold_ms = max(
            0,
            int(last.get("audio_end_ms", 0)) - int(last.get("audio_start_ms", 0)),
        )
        if last_hold_ms > cap_ms:
            terminal_uncapped = 1

    for section in capped:
        start_ms = int(section.get("audio_start_ms", 0))
        end_ms = int(section.get("audio_end_ms", start_ms))
        section["duration_sec"] = max(0, end_ms - start_ms) / 1000

    durations_after = [
        max(0, int(s.get("audio_end_ms", 0)) - int(s.get("audio_start_ms", 0)))
        for s in capped
    ]
    if capped_count or terminal_uncapped:
        logger.info(
            "%s_APPLIED content_id=%s language=%s cap_ms=%d "
            "capped_beats=%d terminal_uncapped=%d max_before_ms=%d max_after_ms=%d",
            log_prefix,
            content_id or "unknown",
            language or "unknown",
            cap_ms,
            capped_count,
            terminal_uncapped,
            max(durations_before) if durations_before else 0,
            max(durations_after) if durations_after else 0,
        )

    return capped



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

def _derive_child_alignment_hints(narration_phrase: str) -> tuple[str, str]:
    """Derive start_hint/end_hint for a child remap beat from its verbatim narration_phrase.

    Mirrors the parent storyboard schema's start_hint/end_hint convention (first/last
    6-10 verbatim words) so ``map_storyboard_beats_to_timestamps()`` has the same shape
    of input to align against on both the parent and child paths. Word-based slicing
    only — no rewriting, summarizing, or punctuation invented beyond whitespace
    normalization. Phrases shorter than ``_CHILD_HINT_MIN_WORDS`` words use the full
    phrase for both hints; medium phrases may have overlapping start/end windows,
    which is allowed since neither hint needs to be invented to avoid overlap.

    Returns:
        ``("", "")`` if ``narration_phrase`` is empty/whitespace-only.
    """
    words = narration_phrase.split()
    if not words:
        return "", ""
    if len(words) < _CHILD_HINT_MIN_WORDS:
        full = " ".join(words)
        return full, full
    n = min(_CHILD_HINT_MAX_WORDS, len(words))
    return " ".join(words[:n]), " ".join(words[-n:])


def _parent_media_reusable(parent_media: str, extras: dict) -> bool:
    """True when a parent beat's image may be reused by a child Short beat.

    Exclusions (audit G-4.1): missing/sentinel media, non-cache paths, and any
    legacy parent text-card beat — text-card backgrounds were generated to sit
    UNDER readable overlay text and frequently contain baked-in pseudo-text;
    run 9500c231 shipped two of them as plain full-screen Short visuals. Text
    cards are removed going forward (subtitles-only rendering), but parent
    `__visual__` rows persisted before that change can still carry them, so
    the reuse pool must filter them out explicitly.
    """
    if not parent_media or parent_media == _TEXT_CARD_SENTINEL:
        return False
    if not parent_media.startswith("cache/"):
        return False
    if extras.get("media_strategy") == "remotion_text_card":
        return False
    if extras.get("visual_type") == "text_card":
        return False
    return True


def remap_beats_for_short(
    short_content,
    short_voice_script: str,
    short_audio_file,
    parent_content_id: uuid.UUID,
    db: Session,
    visual_style: str = "",
    image_style: str = "",
) -> list[dict]:
    """Remap parent long-video beats to a standalone Short episode narration.

    Loads the parent video's shared visual beats (language="__visual__") and asks
    the secondary-model remap call to assign the most thematically relevant
    parent beat to each narration phrase. The match_score decides PROMPT
    INHERITANCE only (fresh full-system audit §3.1): scores >=
    ``_MATCH_SCORE_THRESHOLD`` inherit the parent beat's ``flux_prompt`` for
    visual continuity; lower scores use the remap model's ``new_image_prompt``.
    Every beat leaves this function with ``media_url=""`` — the child path has
    no physical image reuse; ``generate_pending_beat_images()`` generates every
    beat fresh at portrait size afterward, which also preserves the Phase 4E-E
    ordering (``validate_storyboard()`` runs on the beat list before any
    fal.ai call).

    Timestamps are resolved via ``map_storyboard_beats_to_timestamps()`` on the Short's
    own Whisper transcript with ``allow_legacy_fallback=True`` — short episodes always
    accept proportional fallback timing if hint matching fails.

    Args:
        short_content:      Content ORM row for the Short episode.
        short_voice_script: Narration text for this Short episode.
        short_audio_file:   AudioFile ORM row (provides duration_ms + whisper_transcript + language).
        parent_content_id:  content_id of the long-form parent video.
        db:                 SQLAlchemy session.
        visual_style:       Channel-level visual direction (ChannelConfig.visual_style) —
                            forwarded into the remap user message so new child prompts
                            match the channel look, not an unstyled default.
        image_style:        Channel-level image rendering style (ChannelConfig.image_style).

    Returns:
        List of renderable beat-section dicts, every one with ``media_url=""``
        (pending portrait generation). Empty list on failure.
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
    reusable_parent_pairs = [
        (r, extras)
        for r, extras in zip(parent_rows, parent_extras)
        if _parent_media_reusable(extras.get("media_url", ""), extras)
    ]
    excluded_parent_count = len(parent_rows) - len(reusable_parent_pairs)
    if excluded_parent_count:
        logger.info(
            "remap_beats_for_short: excluded %d/%d parent beat(s) from child reuse pool "
            "because media is missing, non-cache, sentinel, or legacy text-card",
            excluded_parent_count, len(parent_rows),
        )

    # Build compact beat index — no flux_prompt (reduces context; Claude doesn't need it).
    # Legacy parent text-card rows are excluded before Claude sees the candidate pool
    # (audit G-4.1), so they cannot be selected for high-score reuse.
    beat_index: list[dict] = [
        {
            "beat_order":    r.section_order,
            "visual_intent": extras.get("visual_intent", ""),
            "environment":   extras.get("environment", "other"),
            "motif":         extras.get("motif", "other"),
        }
        for r, extras in reusable_parent_pairs
    ]

    # 3. Secondary-model remap call
    direction_lines = ""
    if visual_style or image_style:
        direction_lines = (
            f"Global visual direction: {visual_style or 'story_driven'}\n"
            f"Global image style: {image_style or 'photorealistic'}\n\n"
        )
    user_message = (
        direction_lines
        + f"Short narration ({short_audio_file.duration_ms}ms):\n"
        f"{short_voice_script}\n\n"
        f"Parent beat index ({len(beat_index)} beats):\n"
        + json.dumps(beat_index, ensure_ascii=False)
    )

    def _run_remap() -> tuple[dict, dict]:
        return call_claude_structured_with_usage(
            task="short_storyboard_remap",
            system_prompt=_SHORT_REMAP_SYSTEM_PROMPT,
            user_message=user_message,
            schema_name="short_storyboard_remap",
            input_schema=_SHORT_REMAP_SCHEMA,
            max_tokens=_SHORT_REMAP_MAX_TOKENS,
        )

    try:
        result, usage = _run_remap()
    except Exception as exc:
        logger.error(
            "remap_beats_for_short: Claude call failed for content=%s: %s",
            content_id_str, exc,
        )
        return []

    output_tokens = usage.get("output_tokens", 0)
    assignments: list[dict] = result.get("assignments") or []

    if not assignments and output_tokens >= _SHORT_REMAP_MAX_TOKENS:
        # Truncated — Claude hit max_tokens mid-response; the forced tool-use
        # input comes back empty in this case (same failure shape as the
        # truncation case in generate_storyboard_batch()). A larger parent
        # storyboard (more beats in beat_index) and/or a longer short
        # narration (more phrases to assign) makes this more likely — retry
        # once unmodified; if it truncates again, the narration is genuinely
        # too long for one remap call and this is a real failure, not a fluke.
        logger.warning(
            "remap_beats_for_short: truncated (output_tokens=%d == max_tokens=%d) "
            "for content=%s — retrying once",
            output_tokens, _SHORT_REMAP_MAX_TOKENS, content_id_str,
        )
        try:
            result, usage = _run_remap()
        except Exception as exc:
            logger.error(
                "remap_beats_for_short: retry Claude call failed for content=%s: %s",
                content_id_str, exc,
            )
            return []
        output_tokens = usage.get("output_tokens", 0)
        assignments = result.get("assignments") or []
        if not assignments and output_tokens >= _SHORT_REMAP_MAX_TOKENS:
            logger.error(
                "remap_beats_for_short: truncated again on retry (output_tokens=%d) "
                "for content=%s — narration may be too long for one remap call",
                output_tokens, content_id_str,
            )

    if not assignments:
        logger.warning(
            "remap_beats_for_short: no assignments returned for content=%s "
            "(output_tokens=%d, not a truncation)",
            content_id_str, output_tokens,
        )
        return []

    # 4. Apply threshold: inherit the parent beat's prompt or use the new one.
    # Every beat leaves with media_url="" — generation happens strictly after
    # validate_storyboard() (Phase 4E-E ordering), in
    # generate_pending_beat_images(), fresh at portrait size for every beat.
    parent_by_order: dict[int, tuple] = {
        r.section_order: (r, extras)
        for r, extras in reusable_parent_pairs
    }

    beats: list[dict] = []
    inherited_count = 0
    new_prompt_count = 0
    missing_hint_count = 0

    for i, assignment in enumerate(assignments):
        long_order       = int(assignment.get("long_beat_order", -1))
        match_score      = int(assignment.get("match_score", 0))
        beat_intensity   = _safe_enum(assignment.get("beat_intensity"), {"high", "medium", "low"}, "medium")
        narration_phrase = str(assignment.get("narration_phrase", "")).strip()
        new_image_prompt = str(assignment.get("new_image_prompt", "")).strip()

        if not narration_phrase:
            missing_hint_count += 1
            logger.warning(
                "CHILD_REMAP_HINT_MISSING content_id=%s assignment_index=%d long_beat_order=%d "
                "— assignment has no narration_phrase, alignment hints will be empty for this beat",
                content_id_str, i, long_order,
            )

        start_hint, end_hint = _derive_child_alignment_hints(narration_phrase)

        parent_row, extras = parent_by_order.get(long_order, (None, {}))

        # Prompt inheritance decision (audit §3.1): high score + a pool-eligible
        # parent beat → inherit the parent's flux_prompt for visual continuity;
        # otherwise use the remap model's new_image_prompt. media_url is never
        # set here — every child image is generated fresh at portrait size by
        # generate_pending_beat_images(), after validate_storyboard() runs.
        parent_prompt = (parent_row.flux_prompt if parent_row is not None else "") or ""
        if match_score >= _MATCH_SCORE_THRESHOLD and parent_prompt:
            flux_prompt = parent_prompt
            inherited_count += 1
        else:
            flux_prompt = new_image_prompt
            new_prompt_count += 1
            if not flux_prompt and match_score < _MATCH_SCORE_THRESHOLD:
                logger.error(
                    "CHILD_REMAP_NEW_PROMPT_MISSING content_id=%s assignment_index=%d "
                    "long_beat_order=%d match_score=%d — new child image would inherit "
                    "a rejected parent prompt or raw narration; failing remap",
                    content_id_str, i, long_order, match_score,
                )
                return []
            if not flux_prompt:
                # High-score assignment whose parent beat was excluded from the
                # candidate pool (legacy text-card / missing media — audit
                # G-4.1) and no new_image_prompt supplied. Use a deterministic
                # physical prompt, never the excluded parent prompt and never
                # the raw narration sentence by itself.
                flux_prompt = (
                    "Vertical photograph of a concrete physical clue "
                    f"from this Short moment: {narration_phrase[:180]}, practical light, "
                    "subject in foreground, no readable text"
                )

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
            # Subtitles-only rendering: never inherit parent overlay text.
            "overlay_text":           "",
            "overlay_position":       "none",
            "motif":                  _safe_enum(extras.get("motif") if extras else None, _VALID_MOTIFS, _DEFAULT_MOTIF),
            "beat_intensity":         beat_intensity,
            "suggested_duration_sec": 3.0,
            # Always pending — the child path has no physical image reuse;
            # generate_pending_beat_images() generates every beat fresh at
            # portrait size (audit §3.1).
            "media_url":              "",
            "media_type":             "image",
            "parent_reuse_match_score": match_score,
            "start_hint":             start_hint,
            "end_hint":               end_hint,
        }
        beats.append(beat)

    total_beats = len(beats)
    missing_hint_ratio = (missing_hint_count / total_beats) if total_beats > 0 else 0.0
    if missing_hint_ratio > _CHILD_REMAP_HINT_MISSING_FAIL_RATIO:
        logger.error(
            "remap_beats_for_short: content=%s %d/%d assignments (%.0f%%) had no "
            "narration_phrase — exceeds %.0f%% fail threshold, treating as a "
            "systemic remap response defect rather than per-beat fallback",
            content_id_str, missing_hint_count, total_beats,
            100 * missing_hint_ratio, 100 * _CHILD_REMAP_HINT_MISSING_FAIL_RATIO,
        )
        return []
    inherited_rate = (inherited_count / total_beats * 100) if total_beats > 0 else 0.0
    logger.info(
        "CHILD_SHORT_PROMPT_INHERITANCE_STATS content_id=%s beats=%d "
        "inherited_parent_prompts=%d new_prompts=%d inherited_rate=%.1f%%",
        content_id_str, total_beats, inherited_count, new_prompt_count, inherited_rate,
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
        max_hold_ms=_SHORT_VISUAL_MAX_HOLD_MS,
    )
    if not mapped:
        return []

    return _apply_short_visual_hold_cap(
        mapped,
        content_id=content_id_str,
        language=language,
    )


def generate_pending_beat_images(beats: list[dict], content_id: str) -> list[dict]:
    """Generate portrait (1080x1920) Flux images for every child Short beat.

    Child Shorts render vertically (Short.tsx). Every beat arrives from
    `remap_beats_for_short()` with ``media_url=""`` and a ``flux_prompt``
    (either inherited from the matched parent beat or freshly written by the
    remap call — audit §3.1: the child path has no physical image reuse).
    Each beat is generated fresh at `_SHORT_IMAGE_WIDTH` x
    `_SHORT_IMAGE_HEIGHT` and cached under this child's own cache dir —
    never the parent's landscape cache (roadmap 3.1 / audit V-2, G-4.2).

    Runs strictly AFTER `validate_storyboard()` (Phase 4E-E ordering —
    validate-then-generate, mirroring the parent path).

    Mutates every beat in-place (mirrors `generate_all_beat_images()`'s
    contract):
      - Success: sets ``beat["media_url"]`` to a local cache path.
      - Hard failure: left empty here, then filled by
        ``fill_failed_beats_from_neighbors()`` (subtitles-only rendering —
        the ``__text_card__`` sentinel is never produced; a repeated
        neighbouring image is strictly better than a text card, and a beat
        that still has no image is caught by Agent 5's missing-media blocker).

    Args:
        beats:      Beat dicts returned by `remap_beats_for_short()`.
        content_id: Content UUID string for logging.

    Returns:
        The same list, with every beat's `media_url` regenerated at portrait size.
    """
    if not beats:
        return beats

    logger.info(
        "generate_pending_beat_images: content=%s beats=%d requesting portrait "
        "%dx%d images",
        content_id, len(beats), _SHORT_IMAGE_WIDTH, _SHORT_IMAGE_HEIGHT,
    )

    # Caller-scoped only (this child language's generation pass), never
    # persisted — same Pro-tier bookkeeping contract as the parent path's
    # generate_all_beat_images(); see generate_beat_image_with_routing().
    tier_counts: dict[str, int] = {}
    pixel_ledger: list[dict] = []

    for beat in beats:
        # Defensive clear (beats normally arrive pending already) so
        # image_router.select_route() never short-circuits to "reuse" —
        # every beat gets a fresh portrait call using its own flux_prompt.
        beat["media_url"] = ""
        new_url = generate_beat_image_with_routing(
            beat, content_id, tier_counts,
            width=_SHORT_IMAGE_WIDTH, height=_SHORT_IMAGE_HEIGHT,
        )
        if new_url:
            new_url = _dedupe_generated_image_once(
                beat, new_url, content_id, tier_counts, pixel_ledger,
                width=_SHORT_IMAGE_WIDTH, height=_SHORT_IMAGE_HEIGHT,
            )
            beat["media_url"] = new_url
            beat["media_type"] = "image"

    fill_failed_beats_from_neighbors(beats, content_id)

    if tier_counts:
        logger.info(
            "IMAGE_ROUTE_TIER_COUNTS content=%s tier_counts=%s", content_id, tier_counts,
        )

    return beats
