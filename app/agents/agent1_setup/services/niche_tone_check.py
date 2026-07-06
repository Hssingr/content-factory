"""Niche/tone contradiction detection (roadmap 4a / audit P1-9,
code_report/forensic_output_audit_borrasca_run.md).

A real production run showed `Channel.tone="documentary"` configured
alongside `Channel.niche="Reddit horror story narration"`. Every script and
storyboard prompt received "Channel tone: documentary" and correctly obeyed
it — register threading works exactly as designed — producing a neutral,
observational script for a channel whose actual content is horror
narration. Nothing validated niche<->tone coherence at configuration time.

This module is a pure, deterministic, keyword-based heuristic that flags —
never blocks — this specific class of contradiction at setup time. It makes
no AI call and touches no database; see
`activation_readiness.check_activation_readiness()`, which surfaces findings
from this module as non-blocking WARNING-severity entries.
"""

from __future__ import annotations

# Niche keywords implying the story content itself trades on tension/dread —
# a register that a neutral/informational tone would contradict.
_TENSION_NICHE_KEYWORDS = (
    "horror", "thriller", "suspense", "creepy", "scary", "dread",
    "true crime", "crime", "murder", "missing", "disappearance",
    "paranormal", "cult", "serial killer", "unsolved", "conspiracy",
    "nosleep", "mystery", "haunted", "supernatural",
)

# Tone values that contradict a tension-coded niche when configured together.
_NEUTRAL_INFORMATIONAL_TONES = frozenset({
    "documentary", "educational", "informational", "neutral", "analytical",
    "news", "calm",
})


def detect_niche_tone_contradiction(niche: str, tone: str) -> list[dict]:
    """Return contradiction findings between `niche` and `tone`.

    Pure, deterministic, keyword-based — no AI call, no DB access. Returns
    an empty list when no contradiction is detected, including when either
    value is empty/whitespace. Every finding is a flag, never a block.

    Args:
        niche: Channel.niche free-form text.
        tone:  Channel.tone free-form text.

    Returns:
        List of ``{"code": str, "message": str}`` dicts — empty when clean.
    """
    if not niche or not tone:
        return []

    niche_lower = niche.lower()
    tone_normalized = tone.strip().lower()

    matched_keywords = sorted(
        kw for kw in _TENSION_NICHE_KEYWORDS if kw in niche_lower
    )
    if not matched_keywords:
        return []

    if tone_normalized not in _NEUTRAL_INFORMATIONAL_TONES:
        return []

    return [{
        "code": "niche_tone_contradiction",
        "message": (
            f"Channel niche ({niche!r}) reads as tension/dread-driven content "
            f"(matched keyword(s): {', '.join(matched_keywords)}), but tone is "
            f"configured as {tone!r} — a neutral/informational register. Every "
            "script and storyboard prompt receives this tone verbatim and "
            "correctly obeys it, which produces flat, neutral narration for "
            "tension-driven content instead of the dread/suspense the niche "
            "implies. Consider a tone that matches the niche (e.g. "
            "'suspenseful', 'dread-filled', 'tense', 'ominous')."
        ),
    }]
