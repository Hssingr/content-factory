"""Agent 5 — Video Generation orchestration service.

Orchestrates the full per-language video pipeline:
  1. Storyboard Agent        — Claude designs visual beats from the narration and
                               real Whisper timestamps (falls back to the legacy
                               Section Splitter + Section Validator if storyboard
                               generation fails or returns no usable beats)
  2. Save video_sections     — persist to DB
  3. Stock fetcher           — fetch actual media URLs per beat/section
  4. Media Validation Agent  — Claude reviews fetched media, replacement loop
                               (storyboard beats only — max 2 passes)
  4b. Repetition detection   — Python-side anti-slideshow guard: flags repeated/
                               near-repeated visuals (keyword families, duplicate
                               media, consecutive same-subject queries) and
                               re-fetches them via each beat's fallback_query
  5. Assembly Validator      — validate overall assembly quality (Claude, 1 pass)
  6. Shorts Cutter           — group sections into Short segments
  7. Subtitles generator     — standard (main) + karaoke (Shorts) from Whisper timestamps
  7b. Viewer Experience      — Claude reviews the final plan as a real viewer would
      Validator                (intro, script, visuals, captions, audio, pacing);
                               one deterministic repair pass, then skip render if
                               still NEEDS_FIXES
  8. Remotion builder        — write JSON props files
  9. Remotion renderer       — call Remotion CLI, save VideoRender records

Re-entrancy — each phase is skipped when its output already exists:
  • Main MP4 on disk + VideoRender in DB  → language fully done, skip all
  • Props JSON on disk                    → skip steps 1-8, go directly to render
  • Sections in DB                        → skip steps 1-3, go to stock fetch

Status transitions:
  AUDIO_DONE       → GENERATING_VIDEO  (set at start, guards against double-processing)
  GENERATING_VIDEO → VIDEO_DONE        (set on full success)
  GENERATING_VIDEO → FAILED            (set if all languages fail)
"""

import json
import logging
import re
import uuid
from collections import Counter
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AudioFile, Channel, ChannelConfig, Content, Script, VideoRender, VideoSection,
)
from app.agents.agent5_video.subagents.section_splitter import split_into_sections
from app.agents.agent5_video.subagents.section_validator import validate_sections
from app.agents.agent5_video.subagents.storyboard import split_into_beats
from app.agents.agent5_video.subagents.media_validator import validate_and_replace_media
from app.agents.agent5_video.subagents.assembly_validator import validate_assembly
from app.agents.agent5_video.subagents.shorts_cutter import cut_shorts
from app.agents.agent5_video.services.stock_fetcher import (
    fetch_all_sections, fetch_all_beats, fetch_for_beat, fetch_for_section,
)
from app.agents.agent5_video.services.subtitles import (
    build_standard_subtitles, build_karaoke_subtitles,
)
from app.agents.agent5_video.services.remotion_builder import build_main_props, build_short_props
from app.agents.agent5_video.services.renderer import render_main_video, render_short
from app.agents.agent5_video.system_prompt import (
    assess_viewer_experience,
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

# ── Visual repetition detection (anti-slideshow guard, runs before assembly) ──
# Keyword families grouped from the user-reported "repetitive dark corridor
# slideshow" symptom — overused generic b-roll defaults that read as filler.
_REPETITION_KEYWORD_FAMILIES: list[set[str]] = [
    {"corridor", "hallway"},
    {"dark room", "empty room"},
    {"forest"},
    {"office"},
    {"silhouette", "shadow"},
    {"underwater", "ocean", "deep sea", "abyss", "cavern", "submarine", "bioluminescent", "ocean floor"},
]
_REPETITION_WINDOW = 5      # how many nearby beats count as "nearby"
_REPETITION_THRESHOLD = 3   # 3+ nearby beats from the same family → force replacement

# Environment-level repetition: a coarser, deterministic complement to the
# keyword-family check above. Claude assigns each beat's `environment` (a
# fixed 12-value enum); lexically different search queries that describe the
# same SETTING (e.g. "underwater cavern" vs "bioluminescent cave") both map to
# "underwater", so this catches repetition the keyword matcher would miss.
_ENVIRONMENT_REPETITION_WINDOW = 5
_ENVIRONMENT_REPETITION_THRESHOLD = 3   # >3 beats sharing an environment in the window → flag
_ENVIRONMENT_IGNORED = {"other"}        # too coarse to be meaningful as a repetition signal

_SUBJECT_WORD_RE = re.compile(r"[a-zà-öø-ÿ0-9']+", re.IGNORECASE)
_SUBJECT_STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "to", "for", "from"}

_MARKER_STRIP_RE = re.compile(r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$", re.IGNORECASE | re.MULTILINE)
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")

_MAX_VIEWER_REPAIR_PASSES = 2   # 2 Claude passes → 1 repair round between them

# ── Technical-blocker thresholds for the render decision ──────────────────────
# "Missing media critical" = enough placeholder/empty-URL beats that the
# rendered video would be visually broken regardless of other quality.
_MISSING_MEDIA_BLOCK_RATIO = 0.50   # >50% of beats have no real media → block

# ── Pipeline diagnostic helpers ────────────────────────────────────────────────
_PLACEHOLDER_URLS: frozenset[str] = frozenset({"__generated_pending__", "__dark_fallback__"})


def _log_sections_state(sections: list[dict], label: str, language: str) -> None:
    """Log beat/section stats at a named diagnostic checkpoint."""
    n = len(sections)
    if n == 0:
        logger.info("Agent5 [%s] language=%s 0 sections", label, language)
        return

    durations_ms = [s.get("audio_end_ms", 0) - s.get("audio_start_ms", 0) for s in sections]
    very_short   = sum(1 for d in durations_ms if d < 2000)
    min_dur      = min(durations_ms, default=0) / 1000
    max_dur      = max(durations_ms, default=0) / 1000
    avg_dur      = (sum(durations_ms) / len(durations_ms) / 1000) if durations_ms else 0.0

    env_dist   = Counter(s.get("environment", "other") for s in sections)
    motif_dist = Counter(s.get("motif", "other") for s in sections)
    vtype_dist = Counter(s.get("visual_type", "b-roll") for s in sections)
    passage    = sum(1 for s in sections if s.get("motif", "other") in _PASSAGE_MOTIF_FAMILY)
    placeholder = sum(1 for s in sections if s.get("media_url", "") in _PLACEHOLDER_URLS)
    missing     = sum(1 for s in sections if not s.get("media_url", ""))

    logger.info(
        "Agent5 [%s] language=%s beats=%d "
        "dur=%.1f/%.1f/%.1fs very_short=%d passage_motifs=%d "
        "placeholder=%d missing_media=%d "
        "env=%s motifs=%s types=%s",
        label, language, n,
        min_dur, avg_dur, max_dur, very_short, passage,
        placeholder, missing,
        dict(env_dist.most_common(5)),
        dict(motif_dist.most_common(5)),
        dict(vtype_dist.most_common(5)),
    )


def _log_media_state(sections: list[dict], label: str, language: str) -> None:
    """Log media fetch/validation state (after stock fetch or media validation)."""
    n = len(sections)
    if n == 0:
        logger.info("Agent5 [%s] language=%s 0 sections", label, language)
        return

    video_count  = sum(1 for s in sections if s.get("media_type") == "video")
    image_count  = sum(1 for s in sections if s.get("media_type") == "image")
    text_overlay = sum(1 for s in sections if s.get("visual_type") == "text_overlay")
    gen_ph       = sum(1 for s in sections if s.get("media_url") == "__generated_pending__")
    dark_fb      = sum(1 for s in sections if s.get("media_url") == "__dark_fallback__")
    missing      = sum(1 for s in sections if not s.get("media_url", ""))

    url_counter  = Counter(
        s.get("media_url", "")
        for s in sections
        if s.get("media_url", "") and s.get("media_url") not in _PLACEHOLDER_URLS
    )
    dup_urls     = sum(1 for c in url_counter.values() if c > 1)
    top_repeated = [(u[-40:], c) for u, c in url_counter.most_common(10) if c > 1]

    env_dist   = Counter(s.get("environment", "other") for s in sections)
    motif_dist = Counter(s.get("motif", "other") for s in sections)

    logger.info(
        "Agent5 [%s] language=%s beats=%d "
        "video=%d image=%d text_overlay=%d gen_placeholder=%d dark_fallback=%d "
        "missing=%d dup_urls=%d env=%s motifs=%s",
        label, language, n,
        video_count, image_count, text_overlay, gen_ph, dark_fb,
        missing, dup_urls,
        dict(env_dist.most_common(5)),
        dict(motif_dist.most_common(5)),
    )
    if top_repeated:
        logger.warning(
            "Agent5 [%s] language=%s duplicated media_urls: %s",
            label, language, top_repeated,
        )


# ── Render decision helpers ────────────────────────────────────────────────────

def _collect_technical_blockers(
    sections: list[dict],
    standard_subs: list[dict],
    audio: "AudioFile",
) -> list[str]:
    """Return a list of technical blocker descriptions (empty = no blockers).

    Technical blockers are Python-detectable preconditions for a viable render;
    they are independent of Claude's subjective quality assessment. A blocker
    means the output video would be objectively broken:

      - no_beats         : zero sections were generated
      - missing_media_critical : more than 50% of beats have no real media URL
      - no_captions      : caption list is empty despite a populated Whisper transcript

    Args:
        sections:     Beat/section list after all validation stages.
        standard_subs: Standard subtitle chunks built from the Whisper transcript.
        audio:        AudioFile row — used to check whether Whisper transcript exists.

    Returns:
        List of blocker strings (empty means render is technically viable).
    """
    blockers: list[str] = []

    if not sections:
        blockers.append("no_beats")
        return blockers  # rest are moot

    no_media = sum(
        1 for s in sections
        if not s.get("media_url", "") or s.get("media_url", "") in _PLACEHOLDER_URLS
    )
    if no_media / len(sections) > _MISSING_MEDIA_BLOCK_RATIO:
        blockers.append(
            f"missing_media_critical ({no_media}/{len(sections)} beats have no real media)"
        )

    whisper = audio.whisper_transcript or []
    if not standard_subs and whisper:
        blockers.append("no_captions (Whisper transcript present but caption build failed)")

    return blockers


def decide_render_allowed(
    strict_quality_gate: bool,
    technical_blockers: list[str],
    viewer_issues: list[dict],
    media_stop_reason: str,
) -> tuple[bool, str]:
    """Single point of truth for the render decision.

    Decision hierarchy:
      1. Technical blockers always block regardless of quality gate setting.
      2. strict_quality_gate=True: block if any HIGH-severity viewer issue remains.
      3. strict_quality_gate=False: render despite remaining subjective viewer issues.

    Args:
        strict_quality_gate: From channel_config.strict_quality_gate.
        technical_blockers:  From ``_collect_technical_blockers``.
        viewer_issues:       Remaining blocking_issues from the last Viewer Experience call.
        media_stop_reason:   How media validation ended (logged in the reason string).

    Returns:
        ``(render_allowed, reason_string)``.
    """
    if technical_blockers:
        return False, f"BLOCKED_TECHNICAL: {'; '.join(technical_blockers)}"

    if strict_quality_gate:
        high = [i for i in viewer_issues if str(i.get("severity", "")).upper() == "HIGH"]
        if high:
            cats = sorted({str(i.get("category", "?")) for i in high})
            return False, f"BLOCKED_QUALITY_GATE: high_severity_categories={cats}"

    warnings = len(viewer_issues)
    return True, f"ALLOWED warnings={warnings} media_stop={media_stop_reason}"


# ── Micro-beat duration floors (FIX 6) ────────────────────────────────────────
# Beats shorter than these thresholds are absorbed into their neighbour during
# _cleanup_micro_beats, which runs immediately after timestamp mapping.
_MIN_BEAT_MS_NORMAL       = 2000   # 2.0s — floor for any regular beat
_MIN_BEAT_MS_TEXT_OVERLAY = 1500   # 1.5s — lower floor for on-screen text beats
_MIN_BEAT_MS_CUT_ACTION   = 500    # exception: 1 cut+action "impact" beat per video

# ── Motif-level repetition (FIX 5) ────────────────────────────────────────────
# The "passage/doorway motif" family covers the most overused visual trope in
# automated horror/drama: Claude defaults to doorways, corridors, and thresholds
# when it can't think of a better shot for a tense or atmospheric beat.
_PASSAGE_MOTIF_FAMILY: frozenset[str] = frozenset({"doorway", "corridor", "threshold"})
_VALID_MOTIFS: frozenset[str] = frozenset({
    "doorway", "corridor", "face", "hands", "object", "clock", "phone",
    "photo", "exterior", "text", "screen", "reflection", "document", "room", "other",
})
_MOTIF_MAX_PASSAGE_TOTAL  = 4   # ≤4 passage beats in the entire video
_MOTIF_WINDOW             = 10  # sliding window for per-motif repetition check
_MOTIF_MAX_SAME_IN_WINDOW = 2   # ≤2 of the same non-"other" motif in any 10-beat window


def _script_hook(voice_script: str, length: int = 300) -> str:
    """Return the narration opening with timing markers stripped, for quality prompts.

    Truncation is boundary-aware: it prefers the last sentence end at or before
    ``length``, falls back to the last word boundary, and never cuts a word in
    half — a raw character slice was producing mid-word excerpts that confused
    the Viewer Experience Validator.
    """
    text = _MARKER_STRIP_RE.sub("", voice_script or "").strip()
    if len(text) <= length:
        return text

    sentence_end = -1
    for match in _SENTENCE_END_RE.finditer(text):
        if match.end() > length:
            break
        sentence_end = match.end()
    if sentence_end > 0:
        return text[:sentence_end].strip()

    word_boundary = text.rfind(" ", 0, length)
    if word_boundary > 0:
        return text[:word_boundary].strip()

    return text[:length].strip()


def _keyword_family(query: str) -> frozenset[str] | None:
    """Return the overused-keyword family a search query matches, if any."""
    q = query.lower()
    for family in _REPETITION_KEYWORD_FAMILIES:
        if any(keyword in q for keyword in family):
            return frozenset(family)
    return None


def _subject(query: str) -> str:
    """Extract the first two significant words of a search query as its 'subject'."""
    words = [w for w in _SUBJECT_WORD_RE.findall(query.lower()) if w not in _SUBJECT_STOPWORDS]
    return " ".join(words[:2])


def _cleanup_micro_beats(sections: list[dict], script_format: str) -> list[dict]:
    """Merge beats shorter than the minimum duration into their neighbour.

    Rules applied in order:
      - text_overlay beats: minimum 1.5 s (generous — on-screen text needs time to read)
      - cut+action beats:   allowed below 2 s for at most 1 beat per video (impact cut)
      - all other beats:    minimum 2.0 s

    Each micro-beat is absorbed by the immediately preceding beat (extending its
    audio_end_ms). The very first beat, if micro, is absorbed by the next instead.
    beat_order / section_order are renumbered sequentially after all merges so
    downstream code sees a gap-free sequence.

    Args:
        sections:     Beat-section dicts with ``audio_start_ms``, ``audio_end_ms``,
                      ``visual_type``, and ``effect`` fields.
        script_format: Format key — reserved for future format-aware floors.

    Returns:
        Possibly-shorter section list with no micro-beats (except the allowed
        cut+action exception).
    """
    if not sections:
        return sections

    result = list(sections)
    exception_budget = 1  # at most 1 cut+action beat allowed under the normal 2 s floor

    very_short_before = sum(
        1 for s in result
        if (s.get("audio_end_ms", 0) - s.get("audio_start_ms", 0)) < _MIN_BEAT_MS_NORMAL
    )

    changed = True
    while changed and len(result) > 1:
        changed = False
        for i in range(len(result)):
            s       = result[i]
            dur_ms  = s.get("audio_end_ms", 0) - s.get("audio_start_ms", 0)
            vtype   = s.get("visual_type", "b-roll")
            effect  = s.get("effect", "slow_zoom")

            min_ms = _MIN_BEAT_MS_TEXT_OVERLAY if vtype == "text_overlay" else _MIN_BEAT_MS_NORMAL
            if dur_ms >= min_ms:
                continue

            # One cut+action beat per video may stay below 2 s (impact-cut exception)
            if effect == "cut" and vtype == "action" and exception_budget > 0:
                exception_budget -= 1
                logger.debug(
                    "Micro-beat %s (%.2fs) kept as cut+action exception",
                    s.get("beat_order", s.get("section_order", i)), dur_ms / 1000,
                )
                continue

            absorber_idx = (i - 1) if i > 0 else (i + 1)
            if absorber_idx >= len(result):
                continue  # single-element list after prior merges, give up

            absorber = result[absorber_idx]
            if absorber_idx < i:
                absorber["audio_end_ms"] = s["audio_end_ms"]
            else:
                absorber["audio_start_ms"] = s["audio_start_ms"]
            absorber["duration_sec"] = (
                (absorber["audio_end_ms"] - absorber["audio_start_ms"]) / 1000
            )
            logger.debug(
                "Micro-beat %s (%.2fs) absorbed into beat %s — new absorber duration=%.2fs",
                s.get("beat_order", s.get("section_order", i)), dur_ms / 1000,
                absorber.get("beat_order", absorber.get("section_order", absorber_idx)),
                absorber["duration_sec"],
            )
            result.pop(i)
            changed = True
            break

    # Renumber so section_order / beat_order are sequential and gap-free
    for new_order, s in enumerate(result):
        s["section_order"] = new_order
        if "beat_order" in s:
            s["beat_order"] = new_order

    durations_ms   = [s.get("audio_end_ms", 0) - s.get("audio_start_ms", 0) for s in result]
    min_dur_ms     = min(durations_ms, default=0)
    avg_dur_ms     = sum(durations_ms) / len(durations_ms) if durations_ms else 0
    very_short_after = sum(
        1 for d in durations_ms
        if d < _MIN_BEAT_MS_NORMAL
    )
    logger.info(
        "Micro-beat cleanup: beats_before=%d beats_after=%d merged=%d "
        "very_short_before=%d very_short_after=%d min_dur=%.2fs avg_dur=%.2fs",
        len(sections), len(result), len(sections) - len(result),
        very_short_before, very_short_after,
        min_dur_ms / 1000, avg_dur_ms / 1000,
    )
    return result


def _detect_and_fix_repetition(sections: list[dict]) -> int:
    """Detect repeated/near-repeated visuals and re-fetch them before final assembly.

    This is the deterministic, Python-side complement to the Media Validation
    Agent — a last guard against the "repetitive dark corridor slideshow" failure
    mode, using fixed rules so detection is repeatable:

      1. Environment repetition (primary, storyboard beats): if more than
         ``_ENVIRONMENT_REPETITION_THRESHOLD`` beats within a sliding window of
         ``_ENVIRONMENT_REPETITION_WINDOW`` share the same Claude-assigned
         ``environment`` enum value (e.g. "underwater"), the later beats are
         flagged. This catches lexically-different-but-visually-identical
         queries ("underwater cavern" vs "bioluminescent cave") that the
         keyword matcher below would miss — the coarse "other" value is
         ignored as too generic to be a meaningful signal.
      2. Keyword-family repetition (fallback, legacy sections without an
         ``environment`` field): if ``_REPETITION_THRESHOLD`` or more beats
         within a sliding window of ``_REPETITION_WINDOW`` use a search_query from
         the same overused family (corridor/hallway, dark/empty room, forest,
         office, silhouette/shadow, underwater/ocean/abyss/...), the middle/later
         beats are flagged.
      3. Duplicate media: the same fetched ``media_url`` reused across beats.
      4. Consecutive same-subject queries: back-to-back beats whose search_query
         shares the same first two significant words.

    Flagged beats are repaired by swapping in their own Claude-authored
    ``fallback_query`` (a deliberately different visual angle) and re-fetching —
    no extra Claude calls needed.

    Args:
        sections: Fully fetched, media-validated beat/section dicts (mutated in place).

    Returns:
        Number of beats that were flagged and successfully re-fetched.
    """
    flagged: set[int] = set()
    family_hits: Counter = Counter()
    environment_hits: Counter = Counter()
    motif_hits: Counter = Counter()

    # 0. Motif repetition — passage motif (doorway/corridor/threshold) overuse.
    # A beat without a ``motif`` field (legacy sections) is treated as "other" and
    # never flagged here. Passage overuse is checked globally (total cap) and per
    # sliding window (consecutive-context cap).
    motifs = [s.get("motif", "other") for s in sections]
    passage_idxs = [i for i, m in enumerate(motifs) if m in _PASSAGE_MOTIF_FAMILY]
    if len(passage_idxs) > _MOTIF_MAX_PASSAGE_TOTAL:
        for idx in passage_idxs[_MOTIF_MAX_PASSAGE_TOTAL:]:
            flagged.add(idx)
        logger.info(
            "Repetition detection: %d passage motif beats (limit %d) — flagging excess %d",
            len(passage_idxs), _MOTIF_MAX_PASSAGE_TOTAL,
            len(passage_idxs) - _MOTIF_MAX_PASSAGE_TOTAL,
        )

    for i in range(len(sections)):
        window_motifs = motifs[i:i + _MOTIF_WINDOW]
        counts = Counter(m for m in window_motifs if m and m != "other")
        for motif, count in counts.items():
            if count > _MOTIF_MAX_SAME_IN_WINDOW:
                matches = [i + j for j, m in enumerate(window_motifs) if m == motif]
                for idx in matches[_MOTIF_MAX_SAME_IN_WINDOW:]:
                    flagged.add(idx)
                motif_hits[motif] += 1

    if motif_hits:
        top = ", ".join(f"{motif} (x{c})" for motif, c in motif_hits.most_common())
        logger.info("Repetition detection: overused motifs flagged — %s", top)

    # 1. Environment repetition — deterministic, runs first since it is the
    # more reliable signal for storyboard beats (Claude-assigned enum vs.
    # free-text keyword matching).
    environments = [s.get("environment") for s in sections]
    for i in range(len(sections)):
        window = environments[i:i + _ENVIRONMENT_REPETITION_WINDOW]
        counts = Counter(e for e in window if e and e not in _ENVIRONMENT_IGNORED)
        for env, count in counts.items():
            if count > _ENVIRONMENT_REPETITION_THRESHOLD:
                matches = [i + j for j, e in enumerate(window) if e == env]
                for idx in matches[1:]:
                    flagged.add(idx)
                environment_hits[env] += 1

    if environment_hits:
        top = ", ".join(f"{env} (x{count})" for env, count in environment_hits.most_common())
        logger.info("Repetition detection: overused environments flagged — %s", top)

    families = [_keyword_family(s.get("search_query", "")) for s in sections]
    for i in range(len(sections)):
        window = families[i:i + _REPETITION_WINDOW]
        counts = Counter(f for f in window if f is not None)
        for family, count in counts.items():
            if count >= _REPETITION_THRESHOLD:
                matches = [i + j for j, f in enumerate(window) if f == family]
                for idx in matches[1:]:
                    flagged.add(idx)
                family_hits[family] += 1

    seen_urls: dict[str, int] = {}
    for i, s in enumerate(sections):
        url = s.get("media_url", "")
        if not url:
            continue
        if url in seen_urls:
            flagged.add(i)
        else:
            seen_urls[url] = i

    subjects = [_subject(s.get("search_query", "")) for s in sections]
    for i in range(1, len(sections)):
        if subjects[i] and subjects[i] == subjects[i - 1]:
            flagged.add(i)

    if family_hits:
        top = ", ".join(f"{'/'.join(sorted(family))}" for family in family_hits)
        logger.info("Repetition detection: overused keyword families flagged — %s", top)

    if not flagged:
        logger.info("Repetition detection: no repeated/near-repeated visuals found")
        return 0

    fixed = 0
    for idx in sorted(flagged):
        s = sections[idx]
        fallback = (s.get("fallback_query") or "").strip()
        if not fallback or fallback.lower() == s.get("search_query", "").lower():
            continue
        old_url = s.get("media_url", "")
        s["search_query"] = fallback
        if "visual_intent" in s:
            fetch_for_beat(s)
        else:
            fetch_for_section(s)
        fixed += 1
        logger.info(
            "Repetition fix: beat %s — query=%r old_media=%s new_media=%s",
            s.get("beat_order", s.get("section_order", idx)), fallback,
            old_url[:60], s.get("media_url", "")[:60],
        )

    logger.info(
        "Repetition detection complete: %d beat(s) flagged, %d re-fetched",
        len(flagged), fixed,
    )
    return fixed


def _check_viewer_experience(
    sections: list[dict],
    shorts: list[dict],
    standard_subs: list[dict],
    audio: AudioFile,
    channel: Channel,
    channel_style: str,
    script: Script,
    language: str,
    script_format: str = "youtube_long",
    strict_quality_gate: bool = False,
    media_stop_reason: str = "UNKNOWN",
) -> tuple[bool, list[dict]]:
    """Run the Viewer Experience Validator with one repair pass; return render decision.

    Repair routing (attempt 1 → repair → attempt 2 → final decision):
      visuals  : _detect_and_fix_repetition (non-blocking in default mode)
      captions : rebuild standard_subs with stricter 8-word cap (non-blocking)
      pacing   : _cleanup_micro_beats (non-blocking)
      intro / audio / script : log warning only, never trigger a repair

    Blocking decision (via ``decide_render_allowed``):
      strict_quality_gate=False (default):
          Only technical blockers prevent rendering — subjective viewer issues
          (visual repetition, pacing, intro style, audio critique) never block.
      strict_quality_gate=True:
          Any HIGH-severity issue remaining after repair blocks the render.

    Claude outages fail-open: if the Claude call fails on every attempt, the
    validator approves the render so a transient API error never stops production.

    ``sections`` and ``standard_subs`` are mutated in place so the caller's
    Remotion builder picks up repaired data without rebuilding anything.

    Args:
        media_stop_reason: How media validation ended — threaded into the render
            decision reason string for observability.

    Returns:
        ``(render_allowed, remaining_issues)`` — ``remaining_issues`` is the list
        of blocking_issues from the last Claude call (empty when approved).
    """
    total_words = sum(len(c.get("text", "").split()) for c in standard_subs)
    avg_words   = total_words / len(standard_subs) if standard_subs else 0.0
    hook        = _script_hook(script.voice_script)
    whisper     = audio.whisper_transcript or []
    last_issues: list[dict] = []

    for attempt in range(1, _MAX_VIEWER_REPAIR_PASSES + 1):
        try:
            review = assess_viewer_experience(
                sections=sections,
                shorts_count=len(shorts),
                caption_count=len(standard_subs),
                avg_caption_words=avg_words,
                total_duration_ms=audio.duration_ms,
                channel_niche=channel.niche or "",
                channel_tone=channel.tone or "",
                channel_style=channel_style,
                script_hook=hook,
            )
        except Exception as exc:
            logger.error(
                "Viewer Experience Validator failed (attempt %d/%d, language=%s): %s — "
                "proceeding with technical-blockers-only check",
                attempt, _MAX_VIEWER_REPAIR_PASSES, language, exc,
            )
            break  # fall through to render decision with last_issues as-is

        status = review.get("status", "APPROVED")
        issues = review.get("blocking_issues", [])
        last_issues = issues
        logger.info(
            "Viewer Experience Validator: attempt=%d/%d status=%s issues=%d language=%s — %s",
            attempt, _MAX_VIEWER_REPAIR_PASSES,
            status, len(issues), language, review.get("overall_comment", ""),
        )
        for issue in issues:
            logger.warning(
                "Viewer experience issue [%s]: %s -> %s",
                issue.get("category", "?"), issue.get("issue", ""), issue.get("fix", ""),
            )

        if status == "APPROVED" or not issues:
            last_issues = []
            break  # fall through to render decision

        if attempt == _MAX_VIEWER_REPAIR_PASSES:
            break  # final pass done, fall through to render decision

        # ── Repair pass (only runs between attempt N and attempt N+1) ─────────
        categories = {i.get("category", "other") for i in issues}
        logger.info(
            "Viewer Experience Validator: repair pass attempt=%d/%d language=%s categories=%s",
            attempt, _MAX_VIEWER_REPAIR_PASSES, language, sorted(categories),
        )

        if "visuals" in categories:
            logger.info("Viewer repair [visuals]: running repetition fix for language=%s", language)
            _detect_and_fix_repetition(sections)

        if "captions" in categories:
            logger.info(
                "Viewer repair [captions]: rebuilding captions with cap=8 for language=%s", language,
            )
            new_subs  = build_standard_subtitles(whisper, max_words_override=8)
            standard_subs.clear()
            standard_subs.extend(new_subs)
            total_words = sum(len(c.get("text", "").split()) for c in standard_subs)
            avg_words   = total_words / len(standard_subs) if standard_subs else 0.0

        if "pacing" in categories:
            logger.info(
                "Viewer repair [pacing]: merging micro-beats for language=%s", language,
            )
            new_sections = _cleanup_micro_beats(sections, script_format)
            sections[:] = new_sections

        for warn_cat in ("intro", "audio", "script"):
            if warn_cat in categories:
                logger.warning(
                    "Viewer repair [%s]: non-actionable category — logging only for language=%s",
                    warn_cat, language,
                )

    # ── Final render decision ──────────────────────────────────────────────────
    technical_blockers = _collect_technical_blockers(sections, standard_subs, audio)
    render_allowed, render_reason = decide_render_allowed(
        strict_quality_gate=strict_quality_gate,
        technical_blockers=technical_blockers,
        viewer_issues=last_issues,
        media_stop_reason=media_stop_reason,
    )

    if render_allowed:
        logger.info(
            "Viewer Experience Validator: RENDER ALLOWED language=%s reason=%s "
            "viewer_issues=%d technical_blockers=none",
            language, render_reason, len(last_issues),
        )
    else:
        logger.error(
            "Viewer Experience Validator: RENDER BLOCKED language=%s reason=%s",
            language, render_reason,
        )

    return render_allowed, last_issues


def run_video_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 5 video pipeline for one piece of content.

    Processes each language independently. A single-language failure is logged
    and skipped — the pipeline continues for remaining languages.
    Re-entrant: already-completed phases are detected and skipped automatically.

    Args:
        content_id: UUID of content with status ``AUDIO_DONE`` or ``GENERATING_VIDEO``.
        db:         SQLAlchemy session managed by the caller.

    Returns:
        ``True``  — at least one language was successfully rendered.
        ``False`` — all languages failed.
    """
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.error("Content %s not found", content_id)
        return False

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content_id)
        return False

    if content.status not in ("AUDIO_DONE", "GENERATING_VIDEO"):
        logger.debug(
            "Content %s status=%s — skipping video generation",
            content_id, content.status,
        )
        return False

    if content.status == "AUDIO_DONE":
        content.status = "GENERATING_VIDEO"
        db.commit()

    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    runway_enabled        = config.runway_enabled                if config else False
    channel_style         = config.video_style_type              if config else "documentary"
    channel_color_grade   = config.video_color_grade             if config else "desaturated"
    karaoke_color         = config.subtitle_karaoke_active_color if config else "#FFD700"
    shorts_label_style    = config.shorts_part_label_style       if config else "default"
    script_format         = config.script_format                 if config else "youtube_long"
    allow_legacy_fallback = config.allow_legacy_fallback         if config else False
    strict_quality_gate   = config.strict_quality_gate           if config else False

    scripts_by_lang: dict[str, Script] = {
        s.language: s
        for s in db.query(Script)
        .filter(Script.content_id == content_id, Script.validated.is_(True))
        .all()
    }
    audio_by_lang: dict[str, AudioFile] = {
        a.language: a
        for a in db.query(AudioFile)
        .filter(AudioFile.content_id == content_id)
        .all()
    }

    if not scripts_by_lang:
        logger.error("No validated scripts for content %s", content_id)
        content.status = "FAILED"
        db.commit()
        return False

    successful = 0

    for language, script in scripts_by_lang.items():
        audio = audio_by_lang.get(language)
        if not audio:
            logger.warning(
                "No audio file for language=%s, content=%s — skipping", language, content_id
            )
            logger.error("Agent5 [FAIL] language=%s content=%s status=SKIPPED_NO_AUDIO", language, content_id)
            continue

        try:
            ok = _process_language(
                content_id=content_id,
                language=language,
                script=script,
                audio=audio,
                channel=channel,
                runway_enabled=runway_enabled,
                channel_style=channel_style,
                channel_color_grade=channel_color_grade,
                karaoke_color=karaoke_color,
                shorts_label_style=shorts_label_style,
                script_format=script_format,
                allow_legacy_fallback=allow_legacy_fallback,
                strict_quality_gate=strict_quality_gate,
                db=db,
            )
            if ok:
                successful += 1
        except Exception as exc:
            logger.error(
                "Video generation failed for language=%s, content=%s: %s",
                language, content_id, exc,
            )
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=UNKNOWN_FAILED reason=%s",
                language, content_id, type(exc).__name__,
            )
            db.rollback()

    if successful > 0:
        content.status = "VIDEO_DONE"
        logger.info(
            "Video generation complete for content %s (%d language(s))", content_id, successful
        )
    else:
        content.status = "FAILED"
        logger.error("Video generation failed for ALL languages — content %s", content_id)

    db.commit()
    return successful > 0


# ── Per-language pipeline ──────────────────────────────────────────────────────

def _process_language(
    content_id: uuid.UUID,
    language: str,
    script: Script,
    audio: AudioFile,
    channel: Channel,
    runway_enabled: bool,
    channel_style: str,
    channel_color_grade: str,
    karaoke_color: str,
    shorts_label_style: str,
    script_format: str,
    allow_legacy_fallback: bool,
    strict_quality_gate: bool,
    db: Session,
) -> bool:
    """Run the video pipeline for one language, skipping already-completed phases.

    Args:
        allow_legacy_fallback: When the Storyboard Agent fails, ``False`` (the
            default) stops generation for this language with an explicit error
            instead of silently degrading to the legacy section splitter.
            ``True`` restores the previous silent-fallback behavior.
        strict_quality_gate: When ``False`` (default), render even if the Viewer
            Experience Validator reports non-blocking issues (intro/audio/captions/
            pacing categories). When ``True``, any unresolved issue blocks the render.

    Returns:
        True on success, False on any critical failure.
    """
    cid_str    = str(content_id)
    media_root = Path(settings.media_path).resolve()
    props_dir  = media_root / "remotion_props"

    # ── Log point 1: startup diagnostic ───────────────────────────────────────
    _existing_sections = (
        db.query(VideoSection)
        .filter(VideoSection.content_id == content_id, VideoSection.language == language)
        .count()
    )
    _existing_renders = (
        db.query(VideoRender)
        .filter(VideoRender.content_id == content_id, VideoRender.language == language)
        .count()
    )
    _whisper_words = len(audio.whisper_transcript or [])
    _script_words  = len((script.voice_script or "").split())
    logger.info(
        "Agent5 [START] language=%s content=%s "
        "script_words=%d audio_duration_ms=%d whisper_words=%d "
        "existing_sections=%d existing_renders=%d "
        "allow_legacy_fallback=%s strict_quality_gate=%s schema_version=%s",
        language, content_id,
        _script_words, audio.duration_ms, _whisper_words,
        _existing_sections, _existing_renders,
        allow_legacy_fallback, strict_quality_gate, _STORYBOARD_SCHEMA_VERSION,
    )

    # ── Phase check 1: already fully rendered? ─────────────────────────────────
    if _is_rendered(content_id, language, cid_str, media_root, db):
        logger.info("Agent5 [DONE] language=%s content=%s status=ALREADY_RENDERED", language, content_id)
        return True

    # ── Phase check 2: props on disk → skip steps 1-8 ─────────────────────────
    main_props_file = props_dir / f"{cid_str}_{language}_main.json"
    if main_props_file.exists():
        if _props_contain_uhd_url(main_props_file):
            logger.warning(
                "Props file for language=%s contains UHD URL — deleting and regenerating",
                language,
            )
            for stale in props_dir.glob(f"{cid_str}_{language}_*.json"):
                stale.unlink(missing_ok=True)
        else:
            logger.info("Props found on disk for language=%s — skipping to render", language)
            return _render_from_existing_props(
                content_id, language, audio, cid_str, props_dir, db
            )

    # ── Phase check 3: sections in DB → skip steps 1-2 ────────────────────────
    db_sections = _load_sections_from_db(content_id, language, db)
    if db_sections:
        logger.info(
            "Sections already in DB for language=%s (%d) — skipping to stock fetch",
            language, len(db_sections),
        )
        # ── Log point 2: reused-section diagnostic ─────────────────────────────
        _log_sections_state(db_sections, "DB_SECTIONS_REUSED", language)
        sections = db_sections
        using_storyboard = any(s.get("visual_intent") for s in sections)
    else:
        # ── 1. Storyboard Agent (preferred) ───────────────────────────────────
        # Claude designs visual beats from the narration; Python deterministically
        # maps them onto real Whisper timestamps (storyboard.split_into_beats).
        beats = split_into_beats(
            voice_script=script.voice_script,
            duration_ms=audio.duration_ms,
            channel=channel,
            script_format=script_format,
            whisper_transcript=audio.whisper_transcript or [],
            allow_legacy_fallback=allow_legacy_fallback,
        )
        using_storyboard = beats is not None

        if using_storyboard:
            _raw_beat_count = len(beats)
            sections = beats
            # Remove micro-beats immediately after timestamp mapping so downstream
            # phases (stock fetch, assembly validation, Remotion builder) never see
            # a beat with a sub-minimum duration.
            sections = _cleanup_micro_beats(sections, script_format)
            # ── Log point 3: storyboard generation diagnostic ──────────────────
            overlay_count = sum(1 for b in sections if b.get("visual_type") == "text_overlay")
            logger.info(
                "Agent5 [STORYBOARD] language=%s raw_beats=%d after_cleanup=%d "
                "text_overlay=%d schema_version=%s",
                language, _raw_beat_count, len(sections),
                overlay_count, _STORYBOARD_SCHEMA_VERSION,
            )
            _log_sections_state(sections, "AFTER_STORYBOARD", language)
        elif allow_legacy_fallback:
            # ── Fallback: legacy Section Splitter → Section Validator ─────────
            # Only reached when the channel explicitly opted into the old
            # silent-fallback behavior via channel_config.allow_legacy_fallback.
            logger.warning(
                "Storyboard unavailable for language=%s — allow_legacy_fallback=True is "
                "configured, falling back to legacy section splitter",
                language,
            )
            sections = split_into_sections(
                video_script=script.video_script,
                voice_script=script.voice_script,
                duration_ms=audio.duration_ms,
                channel_niche=channel.niche or "",
                channel_tone=channel.tone or "",
                whisper_transcript=audio.whisper_transcript or [],
            )
            if not sections:
                logger.error("Section Splitter produced no sections for language=%s", language)
                logger.error("Agent5 [FAIL] language=%s content=%s status=STORYBOARD_FAILED reason=section_splitter_empty", language, content_id)
                return False

            sections = validate_sections(
                sections=sections,
                channel_niche=channel.niche or "",
                channel_tone=channel.tone or "",
                runway_enabled=runway_enabled,
            )
        else:
            # ── Fail loud: storyboard failed and legacy fallback is disabled ──
            # allow_legacy_fallback=False (default) means a storyboard failure
            # must stop generation with an explicit error rather than silently
            # degrading to the legacy splitter — silent fallback previously
            # masked a 100% storyboard failure rate.
            logger.error(
                "Storyboard generation failed for language=%s and allow_legacy_fallback=False "
                "— stopping video generation for this language (fallback_reason=storyboard_failed)",
                language,
            )
            logger.error("Agent5 [FAIL] language=%s content=%s status=STORYBOARD_FAILED reason=storyboard_generation_failed", language, content_id)
            return False

        # ── 2. Save video_sections to DB ──────────────────────────────────────
        _save_video_sections(content_id, language, sections, db)
        db.commit()   # commit now — render failures must not roll sections back

    # ── 3. Stock fetcher ──────────────────────────────────────────────────────
    sections = fetch_all_beats(sections) if using_storyboard else fetch_all_sections(sections)
    # ── Log point 4: after stock fetch ────────────────────────────────────────
    _log_media_state(sections, "AFTER_STOCK_FETCH", language)

    # ── 4. Media Validation Agent — incremental, stateful, bounded ────────────
    # Only runs for storyboard beats; legacy sections skip (no beat-level metadata).
    if using_storyboard:
        sections, media_state = validate_and_replace_media(
            beats=sections,
            channel_niche=channel.niche or "",
            channel_tone=channel.tone or "",
            script_format=script_format,
        )
        # ── Log point 5: after media validation ───────────────────────────────
        _log_media_state(sections, "AFTER_MEDIA_VALIDATION", language)
    else:
        media_state = {
            "stop_reason":        "NOT_RUN",
            "passes_run":         0,
            "total_replacements": 0,
            "approved_count":     len(sections),
            "failed_count":       0,
            "dirty_remaining":    0,
        }

    # ── 4b. Visual repetition detection (anti-slideshow guard) ────────────────
    _detect_and_fix_repetition(sections)
    # ── Log point 6: after repetition fix ─────────────────────────────────────
    _log_media_state(sections, "AFTER_REPETITION_FIX", language)

    # ── 5. Assembly Validation ────────────────────────────────────────────────
    sections, assembly_dirty = validate_assembly(
        sections=sections,
        total_duration_ms=audio.duration_ms,
        channel_niche=channel.niche or "",
        channel_tone=channel.tone or "",
        channel_style=channel_style,
    )
    # ── Log point 7: after assembly validation ─────────────────────────────────
    logger.info(
        "Agent5 [ASSEMBLY_DONE] language=%s content=%s beats=%d assembly_dirty=%d",
        language, content_id, len(sections), len(assembly_dirty),
    )

    # If assembly validator flagged specific beats, run one targeted media pass
    if assembly_dirty and using_storyboard:
        logger.info(
            "Assembly Validator flagged %d beat(s) for incremental re-validation "
            "for language=%s",
            len(assembly_dirty), language,
        )
        sections, _assembly_media_state = validate_and_replace_media(
            beats=sections,
            channel_niche=channel.niche or "",
            channel_tone=channel.tone or "",
            script_format=script_format,
            max_passes=1,
        )
        # Carry the most relevant stop reason forward
        if _assembly_media_state["total_replacements"] > 0:
            media_state = _assembly_media_state

    # ── 6. Shorts Cutter ──────────────────────────────────────────────────────
    shorts = cut_shorts(
        sections=sections,
        shorts_breakpoints=audio.shorts_breakpoints or [],
        language=language,
        label_style=shorts_label_style,
    )

    # ── 7. Subtitles ──────────────────────────────────────────────────────────
    whisper       = audio.whisper_transcript or []
    standard_subs = build_standard_subtitles(whisper)
    karaoke_subs  = build_karaoke_subtitles(whisper, active_color=karaoke_color)
    # ── Log point 8: after subtitle generation ─────────────────────────────────
    _std_avg_words = (
        sum(len(c.get("text", "").split()) for c in standard_subs) / len(standard_subs)
        if standard_subs else 0.0
    )
    _std_max_words = max(
        (len(c.get("text", "").split()) for c in standard_subs), default=0
    )
    _kar_avg_words = (
        sum(len(c.get("text", "").split()) for c in karaoke_subs) / len(karaoke_subs)
        if karaoke_subs else 0.0
    )
    logger.info(
        "Agent5 [SUBTITLES] language=%s content=%s "
        "standard_captions=%d avg_words=%.1f max_words=%d "
        "karaoke_chunks=%d kar_avg_words=%.1f shorts=%d",
        language, content_id,
        len(standard_subs), _std_avg_words, _std_max_words,
        len(karaoke_subs), _kar_avg_words,
        len(shorts),
    )

    # ── 7b. Viewer Experience Validator (advisory, 2 passes) ─────────────────
    viewer_allowed, viewer_issues = _check_viewer_experience(
        sections=sections,
        shorts=shorts,
        standard_subs=standard_subs,
        audio=audio,
        channel=channel,
        channel_style=channel_style,
        script=script,
        language=language,
        script_format=script_format,
        strict_quality_gate=strict_quality_gate,
        media_stop_reason=media_state["stop_reason"],
    )
    if not viewer_allowed:
        logger.error("Agent5 [FAIL] language=%s content=%s status=QUALITY_GATE_BLOCKED", language, content_id)
        return False

    # ── 8. Remotion builder ───────────────────────────────────────────────────
    main_props_path = build_main_props(
        content_id=cid_str,
        language=language,
        audio_file_path=audio.file_path,
        duration_ms=audio.duration_ms,
        sections=sections,
        standard_subtitles=standard_subs,
        shorts=shorts,
        karaoke_subtitles=karaoke_subs,
        channel_style=channel_style,
        channel_color_grade=channel_color_grade,
    )

    short_props_pairs: list[tuple[dict, str]] = []
    for short in shorts:
        path = build_short_props(
            content_id=cid_str,
            language=language,
            audio_file_path=audio.file_path,
            short=short,
            karaoke_subtitles=karaoke_subs,
            channel_style=channel_style,
            channel_color_grade=channel_color_grade,
        )
        short_props_pairs.append((short, path))

    # ── Log point 10 / 12: pre-render decision summary ────────────────────────
    _audio_path_exists = Path(audio.file_path).exists() if audio.file_path else False
    _final_blockers    = _collect_technical_blockers(sections, standard_subs, audio)
    _render_allowed, _render_reason = decide_render_allowed(
        strict_quality_gate=strict_quality_gate,
        technical_blockers=_final_blockers,
        viewer_issues=viewer_issues,
        media_stop_reason=media_state["stop_reason"],
    )
    logger.info(
        "Agent5 [PRE_RENDER] language=%s content=%s "
        "strict_quality_gate=%s technical_blockers=%s media_stop=%s "
        "viewer_warnings=%d render_allowed=%s reason=%s "
        "beats=%d duration_ms=%d audio_path_ok=%s "
        "standard_captions=%d karaoke_chunks=%d shorts=%d "
        "main_props=%s",
        language, content_id,
        strict_quality_gate, _final_blockers or "none", media_state["stop_reason"],
        len(viewer_issues), _render_allowed, _render_reason,
        len(sections), audio.duration_ms, _audio_path_exists,
        len(standard_subs), len(karaoke_subs), len(shorts),
        main_props_path,
    )

    # ── 9. Remotion renderer ──────────────────────────────────────────────────
    _run_renders(
        content_id=content_id,
        language=language,
        cid_str=cid_str,
        audio=audio,
        main_props_path=main_props_path,
        short_props_pairs=short_props_pairs,
        db=db,
    )

    # ── Log point 11: success ─────────────────────────────────────────────────
    _renders_created = (
        db.query(VideoRender)
        .filter(VideoRender.content_id == content_id, VideoRender.language == language)
        .count()
    )
    logger.info(
        "Agent5 [DONE] language=%s content=%s status=SUCCESS renders_created=%d",
        language, content_id, _renders_created,
    )
    return True


# ── Phase-skip helpers ─────────────────────────────────────────────────────────

def _is_rendered(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    media_root: Path,
    db: Session,
) -> bool:
    """Return True if the main MP4 exists on disk AND a VideoRender row is in DB."""
    row = (
        db.query(VideoRender)
        .filter(
            VideoRender.content_id == content_id,
            VideoRender.language == language,
            VideoRender.format == "main",
        )
        .first()
    )
    if not row:
        return False
    mp4 = media_root / "video" / cid_str / f"{language}_main.mp4"
    return mp4.exists()


def _load_sections_from_db(
    content_id: uuid.UUID, language: str, db: Session
) -> list[dict]:
    """Load VideoSection rows as dicts compatible with the stock fetcher.

    Storyboard beats persist their extra fields (visual_intent, visual_type,
    visual_category, environment, fallback_query, transition_to_next, overlay_text,
    overlay_position) as JSON in the otherwise-unused ``generation_prompt`` column.
    They are deserialized back here so a re-entrant run keeps using the storyboard
    flow (beat-aware fetch, media validation loop, ...) instead of falling back.
    """
    rows = (
        db.query(VideoSection)
        .filter(
            VideoSection.content_id == content_id,
            VideoSection.language == language,
        )
        .order_by(VideoSection.section_order)
        .all()
    )
    result = []
    for s in rows:
        section = {
            "section_order":   s.section_order,
            "beat_order":      s.section_order,
            "script_text":     s.script_text,
            "audio_start_ms":  s.audio_start_ms,
            "audio_end_ms":    s.audio_end_ms,
            "duration_sec":    (s.audio_end_ms - s.audio_start_ms) / 1000,
            "visual_source":   s.visual_source,
            "search_query":    s.search_query or "",
            "suggested_visual": "b-roll",
            "effect":          s.effect or "slow_zoom",
            "color_grade":     s.color_grade or "desaturated",
            "validation_status": "PASS",
            "subagent_rounds": s.subagent_rounds,
            "best_attempt_used": s.best_attempt_used,
        }

        if s.generation_prompt:
            try:
                extras = json.loads(s.generation_prompt)
            except (json.JSONDecodeError, TypeError):
                extras = None
            if isinstance(extras, dict) and "visual_intent" in extras:
                section.update(extras)

        result.append(section)
    return result


def _render_from_existing_props(
    content_id: uuid.UUID,
    language: str,
    audio: AudioFile,
    cid_str: str,
    props_dir: Path,
    db: Session,
) -> bool:
    """Render main + all shorts from props files that are already on disk.

    Skips any individual render whose VideoRender row already exists in DB.
    """
    main_props_path = str(props_dir / f"{cid_str}_{language}_main.json")

    # Main render
    if not _render_exists(content_id, language, "main", None, db):
        main_result = render_main_video(
            content_id=cid_str,
            language=language,
            props_path=main_props_path,
            duration_ms=audio.duration_ms,
        )
        db.add(VideoRender(
            content_id=content_id,
            language=language,
            format="main",
            short_order=None,
            duration_seconds=main_result["duration_seconds"],
            hook_modified=False,
            render_time_seconds=main_result["render_time_seconds"],
        ))
        db.commit()
    else:
        logger.info("Main render already done for language=%s — skipping", language)

    # Shorts: discover from existing props files
    short_prop_files = sorted(
        props_dir.glob(f"{cid_str}_{language}_short_*.json"),
        key=lambda p: int(p.stem.rsplit("_", 1)[1]),
    )
    for sp_path in short_prop_files:
        short_index = int(sp_path.stem.rsplit("_", 1)[1])

        if _render_exists(content_id, language, "short", short_index, db):
            logger.info(
                "Short %d render already done for language=%s — skipping",
                short_index, language,
            )
            continue

        sp = json.loads(sp_path.read_text())
        duration_ms = sp.get("duration_ms", 0)

        short_result = render_short(
            content_id=cid_str,
            language=language,
            short_index=short_index,
            props_path=str(sp_path),
            duration_ms=duration_ms,
            hook_modified=True,
        )
        db.add(VideoRender(
            content_id=content_id,
            language=language,
            format="short",
            short_order=short_index,
            duration_seconds=short_result["duration_seconds"],
            hook_modified=True,
            render_time_seconds=short_result["render_time_seconds"],
        ))
        db.commit()

    logger.info("Render from existing props complete for language=%s", language)
    return True


def _render_exists(
    content_id: uuid.UUID,
    language: str,
    fmt: str,
    short_order: int | None,
    db: Session,
) -> bool:
    """Check if a VideoRender row already exists for this combination."""
    q = db.query(VideoRender).filter(
        VideoRender.content_id == content_id,
        VideoRender.language   == language,
        VideoRender.format     == fmt,
    )
    if short_order is not None:
        q = q.filter(VideoRender.short_order == short_order)
    return q.first() is not None


# ── Render execution ───────────────────────────────────────────────────────────

def _run_renders(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    audio: AudioFile,
    main_props_path: str,
    short_props_pairs: list[tuple[dict, str]],
    db: Session,
) -> None:
    """Render main video + all shorts, committing each VideoRender row individually."""
    main_result = render_main_video(
        content_id=cid_str,
        language=language,
        props_path=main_props_path,
        duration_ms=audio.duration_ms,
    )
    db.add(VideoRender(
        content_id=content_id,
        language=language,
        format="main",
        short_order=None,
        duration_seconds=main_result["duration_seconds"],
        hook_modified=False,
        render_time_seconds=main_result["render_time_seconds"],
    ))
    db.commit()

    for short, props_path in short_props_pairs:
        short_result = render_short(
            content_id=cid_str,
            language=language,
            short_index=short["short_index"],
            props_path=props_path,
            duration_ms=int(short["duration_sec"] * 1000),
            hook_modified=True,
        )
        db.add(VideoRender(
            content_id=content_id,
            language=language,
            format="short",
            short_order=short["short_index"],
            duration_seconds=short_result["duration_seconds"],
            hook_modified=True,
            render_time_seconds=short_result["render_time_seconds"],
        ))
        db.commit()

    logger.info(
        "language=%s done: 1 main + %d short(s) for content %s",
        language, len(short_props_pairs), content_id,
    )


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _props_contain_uhd_url(props_file: Path) -> bool:
    """Return True if any URL in the props JSON exceeds FHD resolution.

    Checks for known UHD/4K filename patterns that crash Remotion's
    OffthreadVideo proxy.  Fast string scan — no full JSON parse needed.
    """
    try:
        raw = props_file.read_text()
        uhd_markers = ("_4096_", "_2160_", "_3840_", "_uhd_", "_4k_", "2160p", "4096p")
        return any(m in raw for m in uhd_markers)
    except Exception:
        return False


def _save_video_sections(
    content_id: uuid.UUID,
    language: str,
    sections: list[dict],
    db: Session,
) -> None:
    """Persist validated sections (or storyboard beats) to video_sections (upsert by order).

    Storyboard-beat-only fields (visual_intent, visual_type, visual_category,
    environment, fallback_query, transition_to_next, overlay_text, overlay_position)
    are JSON-serialized into the otherwise-unused ``generation_prompt`` column so
    they survive re-entrancy without requiring a schema migration. Legacy sections
    (no ``visual_intent``) store ``generation_prompt=None`` as before.
    """
    db.query(VideoSection).filter(
        VideoSection.content_id == content_id,
        VideoSection.language   == language,
    ).delete()

    for s in sections:
        is_beat = "visual_intent" in s
        generation_prompt = json.dumps(_beat_extras(s), ensure_ascii=False) if is_beat else None

        db.add(VideoSection(
            content_id=content_id,
            language=language,
            section_order=s["section_order"],
            script_text=s.get("script_text", ""),
            audio_start_ms=s.get("audio_start_ms", 0),
            audio_end_ms=s.get("audio_end_ms", 0),
            visual_source=s.get("visual_source", "pexels"),
            search_query=s.get("search_query"),
            generation_prompt=generation_prompt,
            effect=s.get("effect"),
            color_grade=s.get("color_grade"),
            runway_used=s.get("visual_source") == "runway",
            subagent_rounds=s.get("subagent_rounds", 1),
            best_attempt_used=s.get("best_attempt_used", False),
        ))

    db.flush()   # caller commits after returning
    logger.info(
        "Saved %d video section(s) for language=%s, content=%s",
        len(sections), language, content_id,
    )


def _beat_extras(section: dict) -> dict:
    """Collect storyboard-beat-only fields for JSON storage in generation_prompt."""
    return {
        "visual_intent":      section.get("visual_intent", ""),
        "visual_type":        section.get("visual_type", "b-roll"),
        "visual_category":    section.get("visual_category", "place"),
        "environment":        section.get("environment", "other"),
        "motif":              section.get("motif", "other"),
        "fallback_query":     section.get("fallback_query", ""),
        "transition_to_next": section.get("transition_to_next", "cut"),
        "overlay_text":       section.get("overlay_text", ""),
        "overlay_position":   section.get("overlay_position", "none"),
    }
