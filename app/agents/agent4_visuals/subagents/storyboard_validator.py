"""Storyboard validation gate — pure Python, no Claude calls.

Runs after storyboard generation and before any fal.ai image generation.
MAJOR issues trigger a per-segment retry (max 1) in split_into_beats().
MINOR issues are logged at WARNING and never block the pipeline.
"""

from __future__ import annotations

import logging
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
            continue  # text_card beats have no flux_prompt requirement

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

    if issues:
        majors = [iss for iss in issues if iss["severity"] == "MAJOR"]
        minors = [iss for iss in issues if iss["severity"] == "MINOR"]
        logger.debug(
            "validate_storyboard: %d MAJOR, %d MINOR issues found in %d beats",
            len(majors), len(minors), total,
        )

    return issues
