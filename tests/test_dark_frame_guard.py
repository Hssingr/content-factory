"""Runtime proof for the dark-frame guard (audit G-6).

`dark_contrast` renders as CSS `contrast(140%) brightness(65%)` — on a source
image with no bright light it produces a near-black frame (two confirmed on
the run-9500c231 contact sheet). The storyboard prompt has always required a
well-lit source for this grade; this guard is that rule's deterministic
Python enforcement: `_build_beat_section()` downgrades an unlit
`dark_contrast` beat to `desaturated` in place (logged), and validator check
20 (`dark_contrast_unlit_prompt`, MINOR) keeps the invariant observable for
any beat that bypasses beat building.

The chain proof drives the real `map_storyboard_beats_to_timestamps` →
`_build_beat_section` path on a real synthetic transcript — nothing internal
stubbed; no external API exists on this path.
"""

import logging
import unittest

from app.agents.agent4_visuals.subagents.storyboard import (
    _build_beat_section,
    map_storyboard_beats_to_timestamps,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    has_bright_lighting_evidence,
    validate_storyboard,
)

_WORD_MS = 400


def _beat(order: int, **overrides) -> dict:
    beat = {
        "beat_order": order,
        "start_hint": "",
        "end_hint": "",
        "visual_intent": f"intent {order}",
        "visual_type": "b-roll",
        "environment": "indoor_domestic",
        "flux_prompt": f"concrete subject {order}, wide shot, photorealistic",
        "effect": "slow_zoom",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "motif": "object",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
    }
    beat.update(overrides)
    return beat


class TestBrightLightingEvidence(unittest.TestCase):
    def test_positive_prompts(self):
        for prompt in (
            "Worn front door, morning sunlight through venetian blinds",
            "Stack of court documents under a brass desk lamp",
            "Empty waiting room, fluorescent overhead panels, wide shot",
            "Cobblestone alley at golden hour, long shadows",
            "Kitchen table, warm window light from the left",
            "Corridor, well-lit, polished tiled floor",
        ):
            self.assertTrue(has_bright_lighting_evidence(prompt), prompt)

    def test_negative_prompts(self):
        for prompt in (
            "Abandoned basement interior, pitch black corners, cobwebs",
            "Moonlit alley at midnight, wet cobblestones",
            "Shadowy forest interior, dense canopy, no people",
            "",
        ):
            self.assertFalse(has_bright_lighting_evidence(prompt), prompt)


class TestBeatBuildDowngrade(unittest.TestCase):
    def test_unlit_dark_contrast_downgrades_and_logs(self):
        raw = _beat(0, color_grade="dark_contrast",
                    flux_prompt="Abandoned basement interior, pitch black corners")
        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level=logging.INFO
        ) as logs:
            out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertEqual(out["color_grade"], "desaturated")
        self.assertTrue(
            any("DARK_CONTRAST_GRADE_DOWNGRADED" in m for m in logs.output), logs.output
        )

    def test_well_lit_dark_contrast_is_kept(self):
        raw = _beat(0, color_grade="dark_contrast",
                    flux_prompt="Office desk, bright fluorescent overhead panels")
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertEqual(out["color_grade"], "dark_contrast")

    def test_other_grades_untouched(self):
        raw = _beat(0, color_grade="neutral",
                    flux_prompt="Abandoned basement interior, pitch black corners")
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertEqual(out["color_grade"], "neutral")

    def test_sanitized_text_prop_prompt_with_lighting_evidence_keeps_grade(self):
        # Elimination Mandate (D2.2/D2.3): the text-prop sanitizer no longer
        # rewrites the subject or appends boilerplate lighting language — it
        # returns Claude's own flux_prompt verbatim plus one no-readable-text
        # clause. The guard must still evaluate the FINAL (sanitized) prompt:
        # since the original prompt already carries lighting evidence, that
        # evidence survives verbatim through sanitization, so the grade
        # survives too.
        raw = _beat(0, color_grade="dark_contrast",
                    flux_prompt="old case file document under a bright desk lamp",
                    visual_intent="a case file document")
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertIn("bright desk lamp", out["flux_prompt"])
        self.assertEqual(out["color_grade"], "dark_contrast")

    def test_sanitized_text_prop_prompt_without_lighting_evidence_downgrades(self):
        # Same verbatim-passthrough sanitizer: an original prompt with NO
        # lighting evidence has none after sanitization either (no more
        # auto-injected "natural practical lighting" boilerplate to
        # artificially exempt every text-prop beat), so the guard correctly
        # downgrades it like any other unlit dark_contrast beat.
        raw = _beat(0, color_grade="dark_contrast",
                    flux_prompt="old case file document on a desk",
                    visual_intent="a case file document")
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertNotIn("lighting", out["flux_prompt"])
        self.assertEqual(out["color_grade"], "desaturated")


class TestChainProof(unittest.TestCase):
    """Real mapping chain: the downgrade lands on the mapped section dicts."""

    def test_mapped_sections_carry_downgraded_grade(self):
        words = ("alpha bravo charlie delta echo foxtrot "
                 "golf hotel india juliet kilo lima").split()
        transcript = [
            {"word": w, "start": i * _WORD_MS / 1000.0, "end": (i + 1) * _WORD_MS / 1000.0}
            for i, w in enumerate(words)
        ]
        beats = [
            _beat(0, start_hint="alpha bravo charlie delta echo foxtrot",
                  end_hint="alpha bravo charlie delta echo foxtrot"),
            _beat(1, start_hint="golf hotel india juliet kilo lima",
                  end_hint="golf hotel india juliet kilo lima",
                  color_grade="dark_contrast",
                  flux_prompt="Shadowy basement interior, pitch black corners"),
        ]
        sections = map_storyboard_beats_to_timestamps(
            beats, transcript, duration_ms=len(words) * _WORD_MS,
            allow_legacy_fallback=True,
        )
        self.assertEqual(sections[1]["color_grade"], "desaturated")
        self.assertEqual(sections[0]["color_grade"], "neutral")


class TestValidatorCheck20(unittest.TestCase):
    """Defense-in-depth check fires only on beats that bypassed beat building."""

    def test_unlit_dark_contrast_flagged_minor(self):
        beats = [
            _beat(0),
            _beat(1, color_grade="dark_contrast",
                  flux_prompt="Shadowy basement interior, pitch black corners"),
        ]
        issues = validate_storyboard(beats)
        hits = [i for i in issues if i["check"] == "dark_contrast_unlit_prompt"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "MINOR")
        self.assertEqual(hits[0]["beat_order"], 1)

    def test_well_lit_dark_contrast_not_flagged(self):
        beats = [
            _beat(0),
            _beat(1, color_grade="dark_contrast",
                  flux_prompt="Basement workbench under bright incandescent bulb"),
        ]
        checks = {i["check"] for i in validate_storyboard(beats)}
        self.assertNotIn("dark_contrast_unlit_prompt", checks)


if __name__ == "__main__":
    unittest.main()
