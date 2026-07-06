"""Storyboard validation gate — deterministic, no Claude/fal.ai calls.

``validate_storyboard(beats)`` — text/structure checks (storyboard quality,
  flux_prompt text quality, reuse-pattern checks). Runs BEFORE any fal.ai
  call, via `visual_orchestrator._check_storyboard_issues()`. For the parent
  path, MAJOR issues trigger one full-storyboard retry in
  `split_into_beats()`; the child path has no equivalent regeneration
  primitive and logs/proceeds immediately. Shared by the parent storyboard
  path and the child remap path through their respective single call
  sites — there is exactly one implementation, not a parent variant and a
  child variant.

MINOR issues are always logged at WARNING and never block the pipeline.

Media existence/integrity checks (post-generation, since a file's existence
cannot be checked before it exists) live in
`app/agents/agent4_visuals/services/media_validation.py`'s
`validate_visual_media_assets()` — the single media validator (roadmap 6.4 /
audit V-7, AR-2). A separate `validate_media_assets()` used to exist in this
module too; it duplicated the same concern (per-language, observability-only,
in-memory `beats` list) and was folded into `validate_visual_media_assets()`
(which also gained its persistence round-trip check) and deleted.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TypedDict

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

# ── Dark-frame guard (audit G-6) ───────────────────────────────────────────────
# The `dark_contrast` grade renders as CSS `contrast(140%) brightness(65%)`
# (MediaSection.tsx GRADE_FILTER): on a well-lit source image it reads as a
# stylized high-contrast look; on a naturally dark source it produces a
# near-black frame — two were confirmed on the run-9500c231 contact sheet.
# The storyboard prompt already instructs "ALWAYS generate a well-lit, bright
# source image" for dark_contrast; this is that rule's deterministic Python
# enforcement (business rules live in Python). A prompt with none of the
# bright-lighting evidence below cannot be trusted with the grade —
# `_build_beat_section()` downgrades it to `desaturated` in place. The bias
# is deliberately safe in both directions: a false positive (e.g. "dim lamp"
# matching "lamp") merely keeps the grade the model chose; a false negative
# downgrades to a visible grade.
_BRIGHT_LIGHTING_WORDS: frozenset[str] = frozenset({
    "daylight", "daylit", "sunlight", "sunlit", "sunrise", "sunset",
    "bright", "brightly", "fluorescent", "fluorescents", "incandescent",
    "candlelight", "lamplight", "lamp", "lamps", "lantern", "skylight",
    "skylights", "floodlight", "floodlights", "spotlight", "neon", "glow",
    "glowing", "illuminated", "overcast", "firelight", "headlights",
    "streetlight", "streetlights",
})
_BRIGHT_LIGHTING_PHRASES: tuple[str, ...] = (
    "golden hour", "well lit", "well-lit", "light through", "side light",
    "window light", "morning light", "afternoon light", "ambient light",
    "natural light", "practical lighting",
)
_LIGHTING_WORD_RE = re.compile(r"[a-z]+")


def has_bright_lighting_evidence(flux_prompt: str) -> bool:
    """True when a flux_prompt names a concrete bright light source/quality.

    Deterministic keyword/phrase check — the gate for whether a beat may keep
    the `dark_contrast` color grade (see the dark-frame guard note above).
    """
    lowered = str(flux_prompt or "").lower()
    if any(phrase in lowered for phrase in _BRIGHT_LIGHTING_PHRASES):
        return True
    tokens = set(_LIGHTING_WORD_RE.findall(lowered))
    return bool(tokens & _BRIGHT_LIGHTING_WORDS)

# ── Visual repetition / scene duplication / slideshow-risk thresholds ──────────
# No single motif (recurring subject/object) may repeat more than this many
# times within any sliding window of this size. Matches the product intent
# already written into the storyboard prompt ("No single motif may repeat
# more than 2 times in any 10-beat window", system_prompt.py) but never
# enforced in Python until this check.
_MOTIF_WINDOW_SIZE = 10
_MOTIF_MAX_PER_WINDOW = 2

# Phase 2.3 / audit G-2: document-ish frames are especially likely to become
# low-variety, text-prop-heavy slideshows. They are a MAJOR finding once they
# exceed either global saturation or local-window saturation.
_DOCUMENT_SATURATION_RATIO = 0.15
_DOCUMENT_WINDOW_SIZE = 10
_DOCUMENT_MAX_PER_WINDOW = 2

# Beats anywhere in the same storyboard/Short that share environment, motif,
# AND camera effect simultaneously are flagged as near-duplicate shots (a
# stronger, multi-field signal than the single-field environment checks below,
# which only ever look at one dimension at a time).

# A run of this many or more consecutive beats sharing the same value for any
# single field (environment, motif, or effect) is flagged as AI-slideshow-risk
# monotony — deliberately longer than the existing 3-beat
# consecutive_same_environment check, and deliberately checked across all
# three fields (not environment alone).
_SLIDESHOW_RUN_LENGTH = 5
_VISUAL_MONOTONY_CONSECUTIVE_ENV_THRESHOLD = 10
_VISUAL_MONOTONY_SLIDESHOW_THRESHOLD = 2

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

# If more than this fraction of flux_generated beats already have a non-empty
# media_url at validation time (i.e. reused from the parent rather than
# pending a new image), the short is over budget and must be remediated.
_REUSE_RATIO_THRESHOLD = 0.60


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

    # Checks 2 (cover_frame_text_card) and 3 (opening_text_card_pair) are
    # retired: text cards are removed product-wide (subtitles-only rendering,
    # audit G-0/G-8) — the conditions they guarded can no longer be produced.

    # ── MINOR check 20: dark_contrast on a prompt with no bright lighting ─────
    # Defense-in-depth behind the dark-frame guard: `_build_beat_section()`
    # already downgrades such beats to `desaturated` in place, so on the
    # normal parent/child paths this check stays silent — it fires only if a
    # beat reached validation without passing through beat building (a future
    # code path, or hand-built fixtures), keeping the invariant observable.
    for beat in beats:
        if (
            beat.get("color_grade") == "dark_contrast"
            and not has_bright_lighting_evidence(beat.get("flux_prompt") or "")
        ):
            issues.append(StoryboardIssue(
                severity="MINOR",
                beat_order=beat.get("beat_order", 0),
                check="dark_contrast_unlit_prompt",
                description=(
                    f"beat_order={beat.get('beat_order', 0)} uses color_grade "
                    "'dark_contrast' but its flux_prompt names no bright light "
                    "source (daylight/lamp/fluorescent/golden hour/…). "
                    "dark_contrast on a dark source renders a near-black frame — "
                    "beat building should have downgraded this to 'desaturated'."
                ),
            ))

    # ── MAJOR check 4: generic/mood-only flux_prompts ─────────────────────────
    for beat in beats:
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
    # text regardless of mood-word presence. (Text cards are removed
    # product-wide — subtitles-only rendering — so every beat is checked.)
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

    # Check 7 (text_card_saturation) is retired: text cards are removed
    # product-wide (subtitles-only rendering, audit G-0/G-8).

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

    # ── MAJOR check 21: document saturation ─────────────────────────────────
    # Audit G-2: too many document-ish beats make the visual track read as a
    # stack of paper/screen frames. Applies to either motif=document or
    # visual_type=document, per roadmap 2.3.
    issues.extend(_find_document_saturation_issues(beats))

    # ── MINOR check 10: near-duplicate beats (scene duplication) ─────────────
    # Multi-field signal: two nearby beats sharing environment AND motif AND
    # camera effect simultaneously are a stronger "this looks like the same
    # shot" signal than any single-field check above.
    issues.extend(_find_near_duplicate_issues(beats))

    # ── MINOR check 11: AI slideshow risk (long single-field runs) ───────────
    # Broader than consecutive_same_environment: checks environment, motif,
    # AND effect independently, and at a longer run length (5+ vs 3).
    issues.extend(_find_slideshow_risk_issues(beats))

    # ── MAJOR check 22: aggregate visual monotony ───────────────────────────
    # Individual repetition findings stay MINOR for diagnostics, but once the
    # aggregate crosses the audit thresholds it must become one retry-triggering
    # MAJOR. Document saturation is already MAJOR; this check groups it with
    # the other repetition signals so the retry reason is explicit.
    issues.extend(_find_visual_monotony_issues(issues))

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


def _find_visual_monotony_issues(issues: list[StoryboardIssue]) -> list[StoryboardIssue]:
    """Escalate aggregate repetition signals into one MAJOR retry reason."""
    consecutive_env_count = sum(
        1 for issue in issues if issue["check"] == "consecutive_same_environment"
    )
    slideshow_count = sum(
        1 for issue in issues if issue["check"] == "ai_slideshow_risk"
    )
    document_saturation_count = sum(
        1 for issue in issues if issue["check"] == "document_saturation"
    )

    reasons: list[str] = []
    if consecutive_env_count > _VISUAL_MONOTONY_CONSECUTIVE_ENV_THRESHOLD:
        reasons.append(
            "consecutive_same_environment findings="
            f"{consecutive_env_count} > {_VISUAL_MONOTONY_CONSECUTIVE_ENV_THRESHOLD}"
        )
    if slideshow_count > _VISUAL_MONOTONY_SLIDESHOW_THRESHOLD:
        reasons.append(
            "ai_slideshow_risk findings="
            f"{slideshow_count} > {_VISUAL_MONOTONY_SLIDESHOW_THRESHOLD}"
        )
    if document_saturation_count:
        reasons.append(
            f"document_saturation fired {document_saturation_count} time(s)"
        )

    if not reasons:
        return []

    return [StoryboardIssue(
        severity="MAJOR",
        beat_order=-1,
        check="visual_monotony",
        description=(
            "Aggregate visual repetition exceeds retry threshold: "
            + "; ".join(reasons)
            + ". Regenerate with stronger environment, motif, and composition variety."
        ),
    )]


def _find_ai_text_rendering_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag a flux_generated beat whose prompt asks the image model to
    render specific readable text (Phase 14.7).

    A quoted phrase of letters, or a "the text reads"/"sign that says"-style
    instruction, asks Flux to literally render words — image models cannot
    do this reliably and the result is always illegible. Under subtitles-only
    rendering there is no overlay substitute either: the prompt must describe
    the physical prop without legible text, and the narration itself conveys
    what the text said. Every beat is checked (text cards are removed
    product-wide, so no exemption exists anymore).
    """
    issues: list[StoryboardIssue] = []
    for beat in beats:
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
                    "as a blank or non-legible object); the narration already conveys what "
                    f"the text said. prompt={prompt[:200]!r}"
                ),
            ))
    return issues


def _is_documentish_beat(beat: dict) -> bool:
    """True when the beat is document-heavy by motif or visual type."""
    return (
        str(beat.get("motif") or "").lower() == "document"
        or str(beat.get("visual_type") or "").lower() == "document"
    )


def _find_document_saturation_issues(beats: list[dict]) -> list[StoryboardIssue]:
    """Flag global or local overuse of document-ish beats as MAJOR.

    A beat is document-ish when either its ``motif`` or ``visual_type`` is
    exactly ``document``. The rule fires when document-ish beats exceed 15% of
    the storyboard globally, or more than 2 beats in any 10-beat window.
    """
    issues: list[StoryboardIssue] = []
    total = len(beats)
    if not total:
        return issues

    document_indices = [i for i, beat in enumerate(beats) if _is_documentish_beat(beat)]
    document_count = len(document_indices)
    if document_count / total > _DOCUMENT_SATURATION_RATIO:
        issues.append(StoryboardIssue(
            severity="MAJOR",
            beat_order=-1,
            check="document_saturation",
            description=(
                f"Document-ish beats (motif=document or visual_type=document) appear in "
                f"{document_count}/{total} beats ({100 * document_count / total:.0f}% > "
                f"{100 * _DOCUMENT_SATURATION_RATIO:.0f}% threshold). Too many document "
                "frames create a text-prop slideshow; replace most with reactions, "
                "aftermath, locations, objects, or spatial tension."
            ),
        ))

    n = len(beats)
    i = 0
    while i < n:
        window = beats[i:i + _DOCUMENT_WINDOW_SIZE]
        if len(window) < 2:
            break
        count = sum(1 for beat in window if _is_documentish_beat(beat))
        if count <= _DOCUMENT_MAX_PER_WINDOW:
            i += 1
            continue

        start = i
        j = i + 1
        while j < n:
            next_window = beats[j:j + _DOCUMENT_WINDOW_SIZE]
            next_count = sum(1 for beat in next_window if _is_documentish_beat(beat))
            if next_count > _DOCUMENT_MAX_PER_WINDOW:
                j += 1
            else:
                break

        issues.append(StoryboardIssue(
            severity="MAJOR",
            beat_order=beats[start].get("beat_order", start),
            check="document_saturation",
            description=(
                f"{count} document-ish beats appear in the {_DOCUMENT_WINDOW_SIZE}-beat "
                f"window starting at beat_order={beats[start].get('beat_order', start)} "
                f"(>{_DOCUMENT_MAX_PER_WINDOW} allowed). Break the document run with "
                "non-document visuals that show human reaction, consequence, setting, "
                "or evidence without readable text."
            ),
        ))
        i = j

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
    """Flag beats whose environment, motif, AND effect all match anywhere in
    the same beat list — a likely visually-redundant shot. At most one issue
    per beat (the first match found), not one per pair.
    """
    issues: list[StoryboardIssue] = []
    n = len(beats)
    for i in range(n):
        a = beats[i]
        a_env, a_motif, a_effect = a.get("environment"), a.get("motif"), a.get("effect")
        if not (a_env and a_motif and a_effect):
            continue
        for j in range(i + 1, n):
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
                        f"('{a_effect}') in the same storyboard — likely a visually "
                        "redundant shot. Vary the "
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
    generic filler. Skips beats with no `flux_prompt` — that absence is not a
    text-quality problem, just not applicable.
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
    """Flag a storyboard where more than `_REUSE_RATIO_THRESHOLD` of its
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
    if ratio <= _REUSE_RATIO_THRESHOLD:
        return []

    return [StoryboardIssue(
        severity="MAJOR",
        beat_order=-1,
        check="excessive_reuse_ratio",
        description=(
            f"{reused}/{len(flux_generated)} beats ({100*ratio:.0f}% > "
            f"{100*_REUSE_RATIO_THRESHOLD:.0f}% budget) already reuse a parent "
            "image at validation time — this short may be mostly recycled "
            "parent footage with too little of its own visual identity."
        ),
    )]



# ── Duplicate-prompt repair (audit G-5.3) ──────────────────────────────────────
# Two beats with byte-identical `flux_prompt` text hash to the SAME Flux cache
# file (image_router/flux_generator's cache key is sha256(prompt) within one
# content_id's cache directory) — a surviving `flux_prompt_exact_duplicate`/
# `flux_prompt_near_duplicate`/`near_duplicate_beat` finding is not just a
# validator note, it is a guaranteed duplicate or near-duplicate image. MAJOR
# findings already trigger a storyboard retry; these three checks are MINOR by
# design (a handful of similar beats is not worth a Sonnet re-generation), so
# nothing today fixes them once the retry pass is exhausted. This
# deterministic, zero-cost repair runs immediately before Flux generation: it
# appends a composition-slot variation (camera distance + angle, drawn from a
# fixed rotation — never random, never an AI call) to exactly the beat_order
# each check itself flags — the later occurrence for the two flux_prompt
# checks (`_find_flux_prompt_duplicate_issues` always names the first
# occurrence of a prompt as "the original" and flags every later match), the
# earlier of the two positions for `near_duplicate_beat` (its own convention —
# see `_find_near_duplicate_issues`). Callers that will regenerate already-
# resolved beats in the same run may opt into repairing those prompts too;
# callers preserving existing media keep the default pending-only behavior so
# stored prompt metadata never drifts from an already persisted image.
_DUPLICATE_REPAIR_CHECKS: frozenset[str] = frozenset({
    "flux_prompt_exact_duplicate", "flux_prompt_near_duplicate", "near_duplicate_beat",
})

_COMPOSITION_DISTANCES: tuple[str, ...] = (
    "close-up framing", "medium shot framing", "wide shot framing", "extreme close-up framing",
)
_COMPOSITION_ANGLES: tuple[str, ...] = (
    "eye-level angle", "low-angle upward view", "high-angle downward view",
    "three-quarter angle view", "profile side view", "over-the-shoulder angle",
)
# Distance x angle — 24 distinct, purely technical/compositional phrases
# (no mood words, consistent with the storyboard prompt's own COMPOSITION
# field), cycled by occurrence order so repeated runs are deterministic.
_COMPOSITION_ROTATION: tuple[str, ...] = tuple(
    f"{distance}, {angle}"
    for distance in _COMPOSITION_DISTANCES
    for angle in _COMPOSITION_ANGLES
)


def composition_slot_variation(occurrence: int) -> str:
    """Return the deterministic composition variation for a duplicate repair.

    Shared by prompt-level duplicate repair and post-generation pixel collision
    rerolls so both anti-duplicate paths draw from one stable rotation.
    """
    return _COMPOSITION_ROTATION[occurrence % len(_COMPOSITION_ROTATION)]


def find_duplicate_prompt_beat_orders(beats: list[dict]) -> dict[int, str]:
    """Return ``{beat_order: check}`` for beats validate_storyboard() flags as
    a visual duplicate — the exact set of beats ``repair_duplicate_flux_prompts()``
    would act on. Reuses ``validate_storyboard()`` itself so the repair target
    list can never drift from what the validator actually detects.
    """
    targets: dict[int, str] = {}
    for issue in validate_storyboard(beats):
        if issue["check"] in _DUPLICATE_REPAIR_CHECKS and issue["beat_order"] not in targets:
            targets[issue["beat_order"]] = issue["check"]
    return targets


def repair_duplicate_flux_prompts(
    beats: list[dict],
    *,
    include_resolved: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Deterministically vary the flux_prompt of beats flagged as visual duplicates.

    By default, only beats still PENDING Flux generation (``media_url`` empty)
    are modified. A caller that is about to regenerate already-resolved beats
    in the same run (the child Short portrait path) passes
    ``include_resolved=True`` so those prompts receive the same append-only
    variation before the new provider call.

    Args:
        beats: Full beat list, in beat_order sequence, as it will be passed to
            Flux generation.
        include_resolved: When True, repair duplicate prompts even if the beat
            currently has ``media_url`` because the caller will regenerate it.

    Returns:
        ``(repaired_beats, repairs)`` — a new list (beats are shallow-copied,
        originals untouched) and a list of ``{beat_order, check, variation}``
        dicts describing every repair applied, for the caller to log. Empty
        repairs list means no beat needed repair (including the common case
        of zero duplicate findings).
    """
    targets = find_duplicate_prompt_beat_orders(beats)
    if not targets:
        return beats, []

    repaired = [dict(beat) for beat in beats]
    repairs: list[dict] = []
    occurrence = 0

    for beat in repaired:
        order = beat.get("beat_order")
        check = targets.get(order)
        if check is None:
            continue
        if beat.get("media_url") and not include_resolved:
            continue  # existing image is being preserved — never rewrite metadata

        variation = composition_slot_variation(occurrence)
        occurrence += 1

        original_prompt = str(beat.get("flux_prompt", "") or "").rstrip(". ")
        beat["flux_prompt"] = f"{original_prompt}, {variation}." if original_prompt else f"{variation}."
        repairs.append({"beat_order": order, "check": check, "variation": variation})

    return repaired, repairs
