"""Storyboard + media validation gate — deterministic, no Claude/fal.ai calls.

Two entrypoints, one shared `StoryboardIssue` shape and severity convention:

``validate_storyboard(beats)`` — text/structure checks (storyboard quality,
  flux_prompt text quality, reuse-pattern checks). Runs BEFORE any fal.ai
  call, via `visual_orchestrator._check_storyboard_issues()`. For the parent
  path, MAJOR issues trigger one full-storyboard retry in
  `split_into_beats()`; the child path has no equivalent regeneration
  primitive and logs/proceeds immediately.

``validate_media_assets(beats, content_id)`` — media existence/integrity/
  reuse checks (Phase 4E-F). Runs AFTER generation, via
  `visual_orchestrator._check_media_assets()`, since a file's existence
  cannot be checked before it exists. Local filesystem reads only
  (`Path.exists()`/`stat()`) — no image decoding, no AI/Claude/fal.ai calls.

Both are shared by the parent storyboard path and the child remap path
through their respective single call sites — there is exactly one
implementation of each, not a parent variant and a child variant.

MINOR issues are always logged at WARNING and never block the pipeline.
MAJOR issues from `validate_media_assets()` are logged at ERROR and also
never block the pipeline — no retry/regeneration mechanism exists for a
missing or corrupt media file (see Phase 4E-E's remediation classification
for the future-work options that would change this); they are observability
only, exactly like the child path's storyboard MAJOR findings.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import TypedDict

from app.config import settings

logger = logging.getLogger(__name__)

# Flux prompt words that indicate mood-only descriptions with no physical subject.
# Any flux_generated beat containing one of these triggers a MAJOR issue.
FORBIDDEN_FLUX_WORDS: frozenset[str] = frozenset({
    "atmospheric", "cinematic", "mysterious", "eerie", "ominous",
    "dramatic", "moody", "haunting", "brooding", "foreboding",
    "unsettling", "ethereal",
    # "dark" as a mood descriptor — but "dark room" / "dark hallway" are legitimate
    # physical descriptions, so we only flag the standalone adjective form.
    # Detection: "dark" must appear NOT followed by a concrete noun within 2 tokens.
    # To avoid false positives (e.g. "dark room", "dark alley") we flag the word
    # only when it appears as a modifier for atmosphere words, not for places.
    # Practical approach: flag "dark" only when accompanied by other mood words.
    # The set below contains the pure mood words; "dark" is checked separately.
})

# "dark" triggers only when the flux_prompt ALSO contains another mood word.
# Lone "dark" (e.g. "dark wooden door") is not forbidden.
_DARK_REQUIRES_COOCCURRENCE: frozenset[str] = frozenset({
    "atmosphere", "atmospheric", "moody", "ominous", "haunting", "mysterious",
    "eerie", "brooding", "foreboding", "unsettling", "ethereal",
})

# ── Visual repetition / scene duplication / slideshow-risk thresholds ──────────
# No single motif (recurring subject/object) may repeat more than this many
# times within any sliding window of this size. Matches the product intent
# already written into the storyboard prompt ("No single motif may repeat
# more than 2 times in any 10-beat window", system_prompt.py) but never
# enforced in Python until this check.
_MOTIF_WINDOW_SIZE = 10
_MOTIF_MAX_PER_WINDOW = 2

# Two beats within this many positions of each other that share environment,
# motif, AND camera effect simultaneously are flagged as a near-duplicate shot
# (a stronger, multi-field signal than the single-field environment checks
# below, which only ever look at one dimension at a time).
_NEAR_DUPLICATE_PROXIMITY = 2

# A run of this many or more consecutive beats sharing the same value for any
# single field (environment, motif, or effect) is flagged as AI-slideshow-risk
# monotony — deliberately longer than the existing 3-beat
# consecutive_same_environment check, and deliberately checked across all
# three fields (not environment alone).
_SLIDESHOW_RUN_LENGTH = 5

# Checks counted toward the VISUAL_REPEAT_RATE diagnostic.
_REPEAT_CHECK_NAMES: frozenset[str] = frozenset({
    "motif_repetition_in_window", "near_duplicate_beat",
})

# ── Flux prompt text-quality thresholds ────────────────────────────────────────
# Words that describe mood/style/quality but name no concrete visual subject.
# Distinct from FORBIDDEN_FLUX_WORDS above: those are an automatic MAJOR
# failure on their own; these are only evidence toward a MINOR
# subject_presence/low_information_prompt finding when they dominate a prompt.
_STYLE_ONLY_WORDS: frozenset[str] = frozenset({
    "cinematic", "dramatic", "epic", "atmospheric", "beautiful", "stunning",
    "amazing", "incredible", "gorgeous", "striking", "vivid", "moody",
    "ethereal", "breathtaking", "captivating", "atmosphere",
})

# Generic visual filler — present in almost every "bad" example prompt and in
# none of the "good" ones. Counted toward low_information_prompt's filler
# ratio and excluded from the subject_presence word count.
_GENERIC_FILLER_WORDS: frozenset[str] = frozenset({
    "high", "quality", "image", "scene", "shot", "great", "good", "nice",
    "wonderful", "fantastic", "awesome",
}) | _STYLE_ONLY_WORDS

# Technical/compositional boilerplate that appears in nearly every real
# flux_prompt (per the prompt's own "TECHNICAL" build-order step) — present
# regardless of whether the prompt has a real subject, so excluded from the
# subject-word count rather than penalized.
_TECHNICAL_BOILERPLATE_WORDS: frozenset[str] = frozenset({
    "photorealistic", "sharp", "focus", "blur", "motion", "people", "text",
    "logos", "wide", "close-up", "overhead", "eye-level", "frame", "camera",
    "no",
})

_SUBJECT_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "in", "on", "with", "and", "at", "is", "to",
    "by", "from", "this", "that", "it", "as", "or", "for",
})

# Phase 14.7 — phrases that ask an image model to render specific readable
# text (as opposed to just describing a document/poster/sign as a physical
# prop, which is fine). A quoted phrase of letters is the strongest signal —
# image models render quoted text literally, and it always comes back as
# illegible gibberish. The phrase list below catches the same intent spelled
# without quote marks ("the text reads...", "a sign that says...").
_QUOTED_TEXT_RE = re.compile(r'"[A-Za-z][^"]{1,80}"')
_AI_TEXT_RENDERING_PHRASES: tuple[str, ...] = (
    "the text reads", "text that reads", "label reading", "label that reads",
    "words that say", "the words", "stamped with the words",
    "engraved with the words", "written on it", "handwritten text saying",
    "caption reading", "headline reads", "sign that reads", "sign reading",
    "name tag reading", "that reads",
)

# A flux_prompt with fewer than this many "real subject" words (after
# stripping stopwords, style-only words, generic filler, and technical
# boilerplate) is flagged as lacking a concrete subject.
_MIN_SUBJECT_WORDS = 2

# A flux_prompt shorter than this many total words, OR with a filler-word
# ratio at or above this threshold, is flagged as low-information.
_MIN_PROMPT_WORDS = 4
_MAX_FILLER_RATIO = 0.50

# Keywords associated with each `environment` enum value, used to check that
# a flux_prompt actually establishes the place/context its structured
# `environment` field claims. "other" makes no commitment and is exempt.
_ENVIRONMENT_KEYWORDS: dict[str, frozenset[str]] = {
    "underwater":         frozenset({"underwater", "water", "pool", "ocean", "sea", "submerged", "diving", "aquatic", "caustics"}),
    "indoor_office":      frozenset({"office", "desk", "cubicle", "boardroom", "workplace", "meeting", "blinds"}),
    "indoor_domestic":    frozenset({"home", "house", "living", "bedroom", "kitchen", "apartment", "domestic", "sofa", "room"}),
    "forest_nature":      frozenset({"forest", "woods", "trees", "jungle", "nature", "wilderness", "trail", "leaf", "canopy", "moss"}),
    "urban_street":       frozenset({"street", "city", "urban", "sidewalk", "alley", "downtown", "road", "corner"}),
    "corridor_interior":  frozenset({"corridor", "hallway", "hall", "passage", "tiled"}),
    "abstract_dark":      frozenset({"shadow", "void", "abstract", "texture", "concrete", "wall", "geometric"}),
    "open_landscape":     frozenset({"field", "landscape", "horizon", "open", "meadow", "plain", "countryside", "grass"}),
    "laboratory":         frozenset({"lab", "laboratory", "beaker", "clinical", "research", "equipment", "bench"}),
    "industrial":         frozenset({"warehouse", "factory", "industrial", "machinery", "steel", "plant", "beams"}),
    "vehicle":            frozenset({"car", "vehicle", "dashboard", "windshield", "cockpit", "cabin", "truck", "train", "steering"}),
}

# Two flux_generated beats whose word-token sets overlap at or above this
# Jaccard ratio are flagged as near-duplicates.
_NEAR_DUPLICATE_JACCARD_THRESHOLD = 0.80

# Checks counted toward the FLUX_PROMPT_QUALITY diagnostic.
_FLUX_QUALITY_CHECK_NAMES: frozenset[str] = frozenset({
    "subject_presence", "environment_presence", "low_information_prompt",
})

# Checks counted toward the FLUX_DUPLICATE_RATE diagnostic.
_FLUX_DUPLICATE_CHECK_NAMES: frozenset[str] = frozenset({
    "flux_prompt_exact_duplicate", "flux_prompt_near_duplicate",
})

# ── Child remap reuse thresholds (Phase 4E-E) ──────────────────────────────────
# A run of this many or more consecutive beats sharing the same non-empty
# media_url is flagged as reuse clustering. Only ever observable on the child
# remap path: by the time validate_storyboard() runs, parent beats always
# have media_url="" (Flux hasn't run yet — see _run_visual_pass ordering),
# while reused child beats already carry the parent's real cache/ path. No
# child-specific code is needed to scope this check; the data makes it so.
_REUSE_CLUSTER_RUN_LENGTH = 3

# If this fraction or more of flux_generated beats already have a non-empty
# media_url at validation time (i.e. reused from the parent rather than
# pending a new image), the short is flagged as over-relying on recycled
# parent footage with too little of its own visual identity.
_REUSE_RATIO_THRESHOLD = 0.70

# ── Media asset thresholds (Phase 4E-F) ────────────────────────────────────────
# The local-path prefix every real generated/cached image must use. Anything
# else (remote URL, malformed string) is a MAJOR finding — see CLAUDE.md
# "No remote media URLs in Remotion props."
_LOCAL_CACHE_PREFIX = "cache/"

# Sentinel written onto a beat's media_url when Flux generation failed
# (flux_generator.py owns the canonical definition; duplicated here per this
# codebase's existing convention of a local copy per file rather than a
# shared constants module — see storyboard.py and video.py for the same
# pattern).
_TEXT_CARD_SENTINEL = "__text_card__"

# media_type values currently supported by the pipeline. Anything else is a
# MAJOR finding (e.g. a future "video" type landing before render support
# for it exists).
_VALID_MEDIA_TYPES: frozenset[str] = frozenset({"image"})

# Text-card hard-failure sentinel. Deliberate remotion_text_card beats should
# normally have generated background images under cache/. The sentinel is allowed
# only when Flux background generation failed and Remotion must render the text-card
# fallback without a media clip.


class StoryboardIssue(TypedDict):
    """One validation finding returned by validate_storyboard()."""
    severity:    str   # "MAJOR" | "MINOR"
    beat_order:  int
    check:       str
    description: str


def validate_storyboard(beats: list[dict]) -> list[StoryboardIssue]:
    """Run all storyboard quality checks on a merged beat list.

    Pure Python — no API calls, no I/O. Returns every issue found; empty list
    means the storyboard is clean. Callers are responsible for acting on MAJOR
    issues (retry) and MINOR issues (log-only).

    Args:
        beats: Merged, timestamp-mapped beat dicts from split_into_beats().

    Returns:
        List of StoryboardIssue dicts, each with severity, beat_order, check,
        and description. Empty list means all checks passed.
    """
    if not beats:
        return []

    issues: list[StoryboardIssue] = []

    # ── MAJOR check 1: cover frame uses dark_contrast ─────────────────────────
    # beat_order=0 is the first image the viewer sees — must be clearly visible.
    first = beats[0]
    if first.get("color_grade") == "dark_contrast":
        issues.append(StoryboardIssue(
            severity="MAJOR",
            beat_order=0,
            check="cover_frame_dark_contrast",
            description=(
                "beat_order=0 (cover frame) uses color_grade 'dark_contrast'. "
                "The cover frame must be clearly visible — use 'neutral', 'warm_amber', "
                "or 'desaturated' instead. dark_contrast renders near-black on a "
                "well-lit image; on a naturally dark scene it produces pure black."
            ),
        ))

    # ── MAJOR check 2: cover frame is a text card ─────────────────────────────
    if first.get("media_strategy") == "remotion_text_card":
        issues.append(StoryboardIssue(
            severity="MAJOR",
            beat_order=0,
            check="cover_frame_text_card",
            description=(
                "beat_order=0 (cover frame) is a remotion_text_card. "
                "The first image must be a photorealistic visual — text cards cannot "
                "serve as cover frames because they have no image thumbnail for "
                "platform previews."
            ),
        ))

    # ── MAJOR check 3: first two beats are both text cards ────────────────────
    if (
        len(beats) >= 2
        and beats[0].get("media_strategy") == "remotion_text_card"
        and beats[1].get("media_strategy") == "remotion_text_card"
    ):
        issues.append(StoryboardIssue(
            severity="MAJOR",
            beat_order=0,
            check="opening_text_card_pair",
            description=(
                "Both beat_order=0 and beat_order=1 are remotion_text_card. "
                "The opening must lead with at least one photorealistic visual image "
                "before any text card. Text-only openings lose viewers immediately."
            ),
        ))

    # ── MAJOR check 4: generic/mood-only flux_prompts ─────────────────────────
    for beat in beats:
        strategy = beat.get("media_strategy", "flux_generated")
        if strategy != "flux_generated":
            continue  # text_card background prompts are generated/derived downstream

        prompt_lower = (beat.get("flux_prompt") or "").lower()
        if not prompt_lower:
            continue

        prompt_words = set(prompt_lower.split())

        # Check forbidden mood words
        found_forbidden = FORBIDDEN_FLUX_WORDS & prompt_words
        # Check "dark" co-occurrence with atmospheric words
        if "dark" in prompt_words and (_DARK_REQUIRES_COOCCURRENCE & prompt_words):
            found_forbidden = found_forbidden | {"dark"}

        if found_forbidden:
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=beat.get("beat_order", 0),
                check="forbidden_flux_word",
                description=(
                    f"flux_prompt for beat_order={beat.get('beat_order', 0)} contains "
                    f"forbidden mood word(s): {sorted(found_forbidden)}. "
                    "flux_prompts must describe what IS IN THE FRAME (subject, composition, "
                    "lighting) — not mood, atmosphere, or feelings. "
                    "Rewrite to answer 'what exact thing would a camera be pointing at?'"
                ),
            ))

    # ── MAJOR check 19: flux_prompt asks the image model to render readable
    # text (Phase 14.7) — a document/poster/calendar/sign/name-tag prompt
    # that asks Flux to render specific words/letters/numbers always comes
    # back as illegible gibberish. Distinct from check 4: that only catches
    # an explicit mood word; this catches a request for literal rendered
    # text regardless of mood-word presence. Applies to flux_generated beats
    # only — text_card backgrounds are exempt the same way check 4 is, since
    # Phase 14.4's own derivation/sanitization (and Phase 14.7's text-prop
    # sanitization) run downstream of this validation pass, on a prompt this
    # check has not seen yet.
    issues.extend(_find_ai_text_rendering_issues(beats))

    # ── MINOR check 5: environment over-saturation (>35% of beats) ───────────
    env_counts: Counter[str] = Counter(
        b.get("environment", "other") for b in beats
    )
    total = len(beats)
    for env, count in env_counts.items():
        if count / total > 0.35:
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=-1,  # applies to the whole storyboard
                check="environment_over_saturation",
                description=(
                    f"Environment '{env}' appears in {count}/{total} beats "
                    f"({100*count/total:.0f}% > 35% threshold). "
                    "Diversify environments to prevent slideshow monotony."
                ),
            ))

    # ── MINOR check 6: 3+ consecutive beats with the same environment ─────────
    for i in range(len(beats) - 2):
        if (
            beats[i].get("environment")
            == beats[i + 1].get("environment")
            == beats[i + 2].get("environment")
        ):
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=beats[i].get("beat_order", i),
                check="consecutive_same_environment",
                description=(
                    f"3 consecutive beats (beat_order={beats[i].get('beat_order', i)}–"
                    f"{beats[i+2].get('beat_order', i+2)}) all use environment "
                    f"'{beats[i].get('environment')}'. Break the run with a different setting."
                ),
            ))

    # ── MINOR check 7: text card saturation (>30% of beats) ──────────────────
    text_card_count = sum(
        1 for b in beats if b.get("media_strategy") == "remotion_text_card"
    )
    if text_card_count / total > 0.30:
        issues.append(StoryboardIssue(
            severity="MINOR",
            beat_order=-1,
            check="text_card_saturation",
            description=(
                f"remotion_text_card in {text_card_count}/{total} beats "
                f"({100*text_card_count/total:.0f}% > 30% threshold). "
                "Too many text cards makes the video feel like a slideshow of titles. "
                "Replace some with photorealistic flux_generated visuals."
            ),
        ))

    # ── MINOR check 8: 3+ consecutive low-intensity beats ────────────────────
    for i in range(len(beats) - 2):
        if all(
            beats[i + j].get("beat_intensity") == "low" for j in range(3)
        ):
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=beats[i].get("beat_order", i),
                check="low_intensity_run",
                description=(
                    f"3 consecutive low-intensity beats starting at "
                    f"beat_order={beats[i].get('beat_order', i)}. "
                    "After 2 low beats, the next must be medium or high — "
                    "extended low-intensity runs create the 'slideshow' effect."
                ),
            ))

    # ── MINOR check 9: motif repetition within a sliding window ──────────────
    # Enforces a product rule already written into the storyboard prompt
    # ("No single motif may repeat more than 2 times in any 10-beat window")
    # but never checked in Python until now.
    issues.extend(_find_motif_repetition_issues(beats))

    # ── MINOR check 10: near-duplicate beats (scene duplication) ─────────────
    # Multi-field signal: two nearby beats sharing environment AND motif AND
    # camera effect simultaneously are a stronger "this looks like the same
    # shot" signal than any single-field check above.
    issues.extend(_find_near_duplicate_issues(beats))

    # ── MINOR check 11: AI slideshow risk (long single-field runs) ───────────
    # Broader than consecutive_same_environment: checks environment, motif,
    # AND effect independently, and at a longer run length (5+ vs 3).
    issues.extend(_find_slideshow_risk_issues(beats))

    # ── MINOR checks 12-14: flux_prompt text quality (subject/environment/
    # information presence) — distinct from check 4's forbidden-word gate,
    # which only catches an explicit mood word, not a prompt that is simply
    # thin or generic without using any forbidden term.
    issues.extend(_find_flux_prompt_quality_issues(beats))

    # ── MINOR checks 15-16: flux_prompt duplication (exact + near) ──────────
    issues.extend(_find_flux_prompt_duplicate_issues(beats))

    # ── MINOR check 17: reuse clustering (same reused image, consecutive) ───
    # Only ever fires on the child remap path — see threshold comment above.
    issues.extend(_find_reuse_clustering_issues(beats))

    # ── MINOR check 18: excessive reuse ratio (too little new visual content) ─
    issues.extend(_find_excessive_reuse_issues(beats))

    if issues:
        majors = [iss for iss in issues if iss["severity"] == "MAJOR"]
        minors = [iss for iss in issues if iss["severity"] == "MINOR"]
        logger.debug(
            "validate_storyboard: %d MAJOR, %d MINOR issues found in %d beats",
            len(majors), len(minors), total,
        )

    # ── Diagnostics: VISUAL_REPEAT_RATE / AI_SLIDESHOW_RISK ───────────────────
    # Always logged (not just on findings) so the rate/risk level is visible
    # even when clean — these are the "diagnostics to maintain" CLAUDE.md §29
    # names this implementation now backs with real validator output.
    repeat_flagged_beats = {
        iss["beat_order"] for iss in issues if iss["check"] in _REPEAT_CHECK_NAMES
    }
    visual_repeat_rate = (len(repeat_flagged_beats) / total * 100) if total else 0.0
    logger.info(
        "VISUAL_REPEAT_RATE beats=%d repeat_flagged=%d rate=%.1f%%",
        total, len(repeat_flagged_beats), visual_repeat_rate,
    )

    slideshow_issues = [iss for iss in issues if iss["check"] == "ai_slideshow_risk"]
    if slideshow_issues:
        logger.warning(
            "AI_SLIDESHOW_RISK risk=HIGH runs=%d beat_orders=%s",
            len(slideshow_issues), [iss["beat_order"] for iss in slideshow_issues],
        )
    else:
        logger.info("AI_SLIDESHOW_RISK risk=LOW runs=0")

    # ── Diagnostics: FLUX_PROMPT_QUALITY / FLUX_DUPLICATE_RATE ───────────────
    # Always logged, scoped to flux_generated beats only (text_card beats have
    # no flux_prompt to score).
    flux_generated_total = sum(
        1 for b in beats if b.get("media_strategy", "flux_generated") == "flux_generated"
    )
    quality_flagged = {
        iss["beat_order"] for iss in issues if iss["check"] in _FLUX_QUALITY_CHECK_NAMES
    }
    quality_rate = (
        len(quality_flagged) / flux_generated_total * 100
    ) if flux_generated_total else 0.0
    logger.info(
        "FLUX_PROMPT_QUALITY flux_beats=%d flagged=%d rate=%.1f%%",
        flux_generated_total, len(quality_flagged), quality_rate,
    )

    duplicate_flagged = {
        iss["beat_order"] for iss in issues if iss["check"] in _FLUX_DUPLICATE_CHECK_NAMES
    }
    duplicate_rate = (
        len(duplicate_flagged) / flux_generated_total * 100
    ) if flux_generated_total else 0.0
    logger.info(
        "FLUX_DUPLICATE_RATE flux_beats=%d flagged=%d rate=%.1f%%",
        flux_generated_total, len(duplicate_flagged), duplicate_rate,
    )

    # ── Diagnostics: CHILD_SHORT_REUSE_RATE / CHILD_SHORT_REUSE_CLUSTERING ───
    # Always logged. Named CHILD_SHORT_* because reuse-from-parent is a child
    # remap concept; the underlying computation runs for every
    # validate_storyboard() call (parent included) but is naturally always
    # zero/LOW for parents (see threshold comment above) — no child-specific
    # branch was needed to produce these correctly.
    reused_now = sum(
        1 for b in beats
        if b.get("media_strategy", "flux_generated") == "flux_generated"
        and (b.get("media_url") or "").startswith("cache/")
    )
    reuse_rate_now = (reused_now / flux_generated_total * 100) if flux_generated_total else 0.0
    logger.info(
        "CHILD_SHORT_REUSE_RATE flux_beats=%d reused=%d rate=%.1f%%",
        flux_generated_total, reused_now, reuse_rate_now,
    )

    clustering_issues = [iss for iss in issues if iss["check"] == "reuse_clustering"]
    if clustering_issues:
        logger.warning(
            "CHILD_SHORT_REUSE_CLUSTERING risk=HIGH runs=%d beat_orders=%s",
            len(clustering_issues), [iss["beat_order"] for iss in clustering_issues],
        )
    else:
        logger.info("CHILD_SHORT_REUSE_CLUSTERING risk=LOW runs=0")

    return issues


def _find_ai_text_rendering_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag a flux_generated beat whose prompt asks the image model to
    render specific readable text (Phase 14.7).

    A quoted phrase of letters, or a "the text reads"/"sign that says"-style
    instruction, asks Flux to literally render words — image models cannot
    do this reliably and the result is always illegible. Readable text for
    a document/poster/calendar/sign beat must come from a Remotion overlay
    instead (`flux_generator.derive_text_prop_overlay()`), not from the
    generated image itself.

    Exempt: `remotion_text_card` beats — their background prompt is derived/
    sanitized downstream of this validation pass (Phase 14.4's
    `derive_text_card_background_prompt()`), which this check has not seen
    yet at validation time. Same exemption shape as check 4
    (`forbidden_flux_word`).
    """
    issues: list[StoryboardIssue] = []
    for beat in beats:
        if beat.get("media_strategy", "flux_generated") != "flux_generated":
            continue
        prompt = str(beat.get("flux_prompt", "") or "")
        if not prompt:
            continue
        lowered = prompt.lower()
        quoted_match = _QUOTED_TEXT_RE.search(prompt)
        phrase_match = next((p for p in _AI_TEXT_RENDERING_PHRASES if p in lowered), None)
        if quoted_match or phrase_match:
            evidence = f'quoted text {quoted_match.group(0)!r}' if quoted_match else f"phrase {phrase_match!r}"
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=beat.get("beat_order", 0),
                check="ai_text_rendering_requested",
                description=(
                    f"flux_prompt for beat_order={beat.get('beat_order', 0)} appears to ask "
                    f"the image model to render specific readable text ({evidence}). Image "
                    "models cannot render legible text reliably — it always comes back as "
                    "gibberish. Describe the physical prop only (document/poster/calendar/sign "
                    "as a blank or non-legible object) and render any needed readable text as "
                    f"a Remotion overlay instead. prompt={prompt[:200]!r}"
                ),
            ))
    return issues


def _find_motif_repetition_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag any motif that repeats more than `_MOTIF_MAX_PER_WINDOW` times
    within any `_MOTIF_WINDOW_SIZE`-beat sliding window.

    Contiguous runs of windows violating the same motif are merged into a
    single issue (one per run, not one per overlapping window) to avoid
    duplicate-issue spam over a long run.
    """
    issues: list[StoryboardIssue] = []
    n = len(beats)
    i = 0
    while i < n:
        window = beats[i:i + _MOTIF_WINDOW_SIZE]
        if len(window) < 2:
            break
        counts = Counter(b.get("motif", "other") for b in window)
        violation_motif = next(
            (motif for motif, count in counts.items() if count > _MOTIF_MAX_PER_WINDOW),
            None,
        )
        if violation_motif is None:
            i += 1
            continue

        start = i
        j = i + 1
        while j < n:
            next_window = beats[j:j + _MOTIF_WINDOW_SIZE]
            next_counts = Counter(b.get("motif", "other") for b in next_window)
            if next_counts.get(violation_motif, 0) > _MOTIF_MAX_PER_WINDOW:
                j += 1
            else:
                break

        issues.append(StoryboardIssue(
            severity="MINOR",
            beat_order=beats[start].get("beat_order", start),
            check="motif_repetition_in_window",
            description=(
                f"motif '{violation_motif}' appears more than {_MOTIF_MAX_PER_WINDOW} times "
                f"in a {_MOTIF_WINDOW_SIZE}-beat window starting at beat_order="
                f"{beats[start].get('beat_order', start)}. Vary the recurring subject/object."
            ),
        ))
        i = j

    return issues


def _find_near_duplicate_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag beats whose environment, motif, AND effect all match another beat
    within `_NEAR_DUPLICATE_PROXIMITY` positions — a likely visually-redundant
    shot. At most one issue per beat (the first match found), not one per pair.
    """
    issues: list[StoryboardIssue] = []
    n = len(beats)
    for i in range(n):
        a = beats[i]
        a_env, a_motif, a_effect = a.get("environment"), a.get("motif"), a.get("effect")
        if not (a_env and a_motif and a_effect):
            continue
        for j in range(i + 1, min(i + 1 + _NEAR_DUPLICATE_PROXIMITY, n)):
            b = beats[j]
            if (
                b.get("environment") == a_env
                and b.get("motif") == a_motif
                and b.get("effect") == a_effect
            ):
                issues.append(StoryboardIssue(
                    severity="MINOR",
                    beat_order=a.get("beat_order", i),
                    check="near_duplicate_beat",
                    description=(
                        f"beat_order={a.get('beat_order', i)} and beat_order="
                        f"{b.get('beat_order', j)} share the same environment "
                        f"('{a_env}'), motif ('{a_motif}'), and camera effect "
                        f"('{a_effect}') within {_NEAR_DUPLICATE_PROXIMITY} beats of "
                        "each other — likely a visually redundant shot. Vary the "
                        "setting, subject, or camera treatment."
                    ),
                ))
                break

    return issues


def _find_slideshow_risk_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag runs of `_SLIDESHOW_RUN_LENGTH`+ consecutive beats sharing the
    same value for environment, motif, or effect — broader and longer-run
    than the existing single-field `consecutive_same_environment` check.
    """
    issues: list[StoryboardIssue] = []
    n = len(beats)
    for field in ("environment", "motif", "effect"):
        i = 0
        while i < n:
            value = beats[i].get(field)
            j = i
            while j + 1 < n and beats[j + 1].get(field) == value:
                j += 1
            run_length = j - i + 1
            if run_length >= _SLIDESHOW_RUN_LENGTH and value:
                issues.append(StoryboardIssue(
                    severity="MINOR",
                    beat_order=beats[i].get("beat_order", i),
                    check="ai_slideshow_risk",
                    description=(
                        f"{run_length} consecutive beats (beat_order="
                        f"{beats[i].get('beat_order', i)}–{beats[j].get('beat_order', j)}) "
                        f"all share {field}='{value}' — long unbroken runs of the same "
                        f"{field} create an AI-slideshow feel. Break the run with a "
                        "different value."
                    ),
                ))
            i = j + 1

    return issues


def _find_flux_prompt_quality_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag flux_generated beats whose `flux_prompt` is weak text: missing a
    concrete subject, never establishing its stated environment, or mostly
    generic filler. Skips beats with no `flux_prompt` (text_card beats) — that
    absence is not a text-quality problem, just not applicable.
    """
    issues: list[StoryboardIssue] = []

    for beat in beats:
        if beat.get("media_strategy", "flux_generated") != "flux_generated":
            continue

        raw_prompt = (beat.get("flux_prompt") or "").strip()
        if not raw_prompt:
            continue

        order = beat.get("beat_order", 0)
        words = [w.strip(",.()") for w in raw_prompt.lower().split()]
        words = [w for w in words if w]

        # ── subject_presence ──────────────────────────────────────────────
        subject_words = [
            w for w in words
            if w not in _SUBJECT_STOPWORDS
            and w not in _GENERIC_FILLER_WORDS
            and w not in _TECHNICAL_BOILERPLATE_WORDS
        ]
        if len(subject_words) < _MIN_SUBJECT_WORDS:
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=order,
                check="subject_presence",
                description=(
                    f"flux_prompt for beat_order={order} has only "
                    f"{len(subject_words)} concrete-subject word(s) after removing "
                    "style/mood/technical filler — it likely names no physical "
                    f"subject. prompt={raw_prompt[:120]!r}"
                ),
            ))

        # ── environment_presence ──────────────────────────────────────────
        env = beat.get("environment")
        env_keywords = _ENVIRONMENT_KEYWORDS.get(env)
        if env_keywords and not (env_keywords & set(words)):
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=order,
                check="environment_presence",
                description=(
                    f"flux_prompt for beat_order={order} declares environment="
                    f"'{env}' but contains none of that environment's expected "
                    f"keywords — the prompt may not actually establish the place/"
                    f"context. prompt={raw_prompt[:120]!r}"
                ),
            ))

        # ── low_information_prompt ─────────────────────────────────────────
        if len(words) < _MIN_PROMPT_WORDS:
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=order,
                check="low_information_prompt",
                description=(
                    f"flux_prompt for beat_order={order} has only {len(words)} "
                    f"word(s) — too short to specify a real image. "
                    f"prompt={raw_prompt[:120]!r}"
                ),
            ))
        else:
            filler_count = sum(1 for w in words if w in _GENERIC_FILLER_WORDS)
            filler_ratio = filler_count / len(words)
            if filler_ratio >= _MAX_FILLER_RATIO:
                issues.append(StoryboardIssue(
                    severity="MINOR",
                    beat_order=order,
                    check="low_information_prompt",
                    description=(
                        f"flux_prompt for beat_order={order} is "
                        f"{filler_ratio*100:.0f}% generic filler words "
                        f"(>= {_MAX_FILLER_RATIO*100:.0f}% threshold) — mostly "
                        f"mood/quality adjectives with little real visual detail. "
                        f"prompt={raw_prompt[:120]!r}"
                    ),
                ))

    return issues


def _find_flux_prompt_duplicate_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag flux_generated beats whose `flux_prompt` exactly or near-duplicates
    an earlier beat's prompt within the same storyboard — at most one issue
    per beat (the first/earliest match), not one per pair.
    """
    issues: list[StoryboardIssue] = []
    seen_normalized: dict[str, int] = {}
    seen_tokens: list[tuple[int, frozenset[str]]] = []

    for idx, beat in enumerate(beats):
        if beat.get("media_strategy", "flux_generated") != "flux_generated":
            continue

        raw_prompt = (beat.get("flux_prompt") or "").strip().lower()
        if not raw_prompt:
            continue

        order = beat.get("beat_order", idx)
        normalized = " ".join(raw_prompt.split())

        first_idx = seen_normalized.get(normalized)
        if first_idx is not None:
            first_order = beats[first_idx].get("beat_order", first_idx)
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=order,
                check="flux_prompt_exact_duplicate",
                description=(
                    f"flux_prompt for beat_order={order} is identical to "
                    f"beat_order={first_order}'s — duplicate prompts generate "
                    "the same image twice. Vary subject, setting, or framing."
                ),
            ))
            continue

        seen_normalized[normalized] = idx
        tokens = frozenset(normalized.split())

        for prior_idx, prior_tokens in seen_tokens:
            union = tokens | prior_tokens
            similarity = (len(tokens & prior_tokens) / len(union)) if union else 0.0
            if similarity >= _NEAR_DUPLICATE_JACCARD_THRESHOLD:
                prior_order = beats[prior_idx].get("beat_order", prior_idx)
                issues.append(StoryboardIssue(
                    severity="MINOR",
                    beat_order=order,
                    check="flux_prompt_near_duplicate",
                    description=(
                        f"flux_prompt for beat_order={order} is {similarity*100:.0f}% "
                        f"similar (word overlap) to beat_order={prior_order}'s — "
                        "likely to generate a near-identical image. Vary subject, "
                        "setting, or camera treatment."
                    ),
                ))
                break

        seen_tokens.append((idx, tokens))

    return issues


def _find_reuse_clustering_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag runs of `_REUSE_CLUSTER_RUN_LENGTH`+ consecutive beats sharing the
    same non-empty `media_url` — e.g. four child-short beats in a row all
    reusing the identical parent image. One issue per run, not per beat.
    """
    issues: list[StoryboardIssue] = []
    n = len(beats)
    i = 0
    while i < n:
        url = beats[i].get("media_url")
        if not url:
            i += 1
            continue
        j = i
        while j + 1 < n and beats[j + 1].get("media_url") == url:
            j += 1
        run_length = j - i + 1
        if run_length >= _REUSE_CLUSTER_RUN_LENGTH:
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=beats[i].get("beat_order", i),
                check="reuse_clustering",
                description=(
                    f"{run_length} consecutive beats (beat_order="
                    f"{beats[i].get('beat_order', i)}–{beats[j].get('beat_order', j)}) "
                    "all reuse the identical image — the same visual repeated "
                    "back-to-back. Vary which parent beats are reused, or "
                    "generate a new image for some of this run."
                ),
            ))
        i = j + 1

    return issues


def _find_excessive_reuse_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag a storyboard where `_REUSE_RATIO_THRESHOLD` or more of its
    flux_generated beats already have a non-empty media_url at validation
    time (i.e. reused rather than pending a new image) — too little of the
    short's own visual identity, mostly recycled parent footage.

    One aggregate issue for the whole storyboard (beat_order=-1), matching
    the existing convention for storyboard-wide ratio checks (e.g.
    environment_over_saturation).
    """
    flux_generated = [
        b for b in beats if b.get("media_strategy", "flux_generated") == "flux_generated"
    ]
    if not flux_generated:
        return []

    reused = sum(1 for b in flux_generated if (b.get("media_url") or "").startswith("cache/"))
    ratio = reused / len(flux_generated)
    if ratio < _REUSE_RATIO_THRESHOLD:
        return []

    return [StoryboardIssue(
        severity="MINOR",
        beat_order=-1,
        check="excessive_reuse_ratio",
        description=(
            f"{reused}/{len(flux_generated)} beats ({100*ratio:.0f}% >= "
            f"{100*_REUSE_RATIO_THRESHOLD:.0f}% threshold) already reuse a parent "
            "image at validation time — this short may be mostly recycled "
            "parent footage with too little of its own visual identity."
        ),
    )]


def validate_media_assets(beats: list[dict], content_id: str) -> list[StoryboardIssue]:
    """Run media existence/integrity/reuse checks on already-generated beats.

    Unlike `validate_storyboard()`, this must run AFTER generation — a file's
    existence cannot be checked before it exists. Pure local filesystem reads
    (`Path.exists()`/`stat()`) only; no image decoding, no AI/Claude/fal.ai
    calls. Shared by both the parent and child paths via the single call site
    `visual_orchestrator._check_media_assets()`.

    Args:
        beats:      Beat dicts with `media_url`/`media_type` already resolved
                    (post-`generate_all_beat_images()` for parents,
                    post-`generate_pending_beat_images()` for children).
        content_id: The *current* content's id (str) — used to distinguish a
                    beat's own freshly-generated media from a reused parent
                    asset (whose `cache/{other_id}/...` path names a
                    different content id).

    Returns:
        List of StoryboardIssue dicts, all MAJOR (this function does not
        produce MINOR findings). Empty list means every beat's media
        reference is present, well-formed, and exists on disk.
    """
    if not beats:
        return []

    issues: list[StoryboardIssue] = []
    media_root = Path(settings.media_path)

    for beat in beats:
        order = beat.get("beat_order", 0)
        strategy = beat.get("media_strategy", "flux_generated")

        media_type = beat.get("media_type", "image")
        if media_type not in _VALID_MEDIA_TYPES:
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=order,
                check="media_type_unsupported",
                description=(
                    f"beat_order={order} has media_type={media_type!r}, not in "
                    f"the supported set {sorted(_VALID_MEDIA_TYPES)}."
                ),
            ))

        url = beat.get("media_url")
        if url is None or url == "":
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=order,
                check="media_url_missing" if url is None else "media_url_empty",
                description=(
                    f"beat_order={order} (media_strategy={strategy!r}) has no "
                    "media_url at the point media validation runs — generation "
                    "should already be complete by here."
                ),
            ))
            continue

        if url == _TEXT_CARD_SENTINEL:
            # A real, expected outcome of a failed Flux/text-card background
            # generation — not a validator finding on its own. Successful
            # deliberate text cards should normally carry cache/ background media.
            continue

        if not url.startswith(_LOCAL_CACHE_PREFIX):
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=order,
                check="media_url_malformed",
                description=(
                    f"beat_order={order} media_url={url[:80]!r} does not start "
                    f"with {_LOCAL_CACHE_PREFIX!r} and is not the text_card "
                    "sentinel — looks like a remote URL or otherwise invalid "
                    "local file reference."
                ),
            ))
            continue

        owner_id = url.split("/", 2)[1] if url.count("/") >= 2 else ""
        is_reused = bool(owner_id) and owner_id != content_id
        path = media_root / url

        if not path.is_file():
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=order,
                check="reused_media_missing" if is_reused else "media_file_missing_on_disk",
                description=(
                    f"beat_order={order} media_url={url!r} does not exist on disk "
                    f"at {path} ({'reused parent asset' if is_reused else 'own generated asset'})."
                ),
            ))
            continue

        try:
            size = path.stat().st_size
        except OSError as exc:
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=order,
                check="media_file_unreadable",
                description=f"beat_order={order} media_url={url!r} could not be read: {exc}",
            ))
            continue

        if size == 0:
            issues.append(StoryboardIssue(
                severity="MAJOR",
                beat_order=order,
                check="reused_media_empty" if is_reused else "media_file_empty",
                description=(
                    f"beat_order={order} media_url={url!r} is a zero-byte file "
                    f"({'reused parent asset' if is_reused else 'own generated asset'})."
                ),
            ))

    if issues:
        logger.debug(
            "validate_media_assets: %d MAJOR issue(s) found in %d beats (content=%s)",
            len(issues), len(beats), content_id,
        )

    return issues
