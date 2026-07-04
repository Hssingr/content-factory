"""Runtime proof for text-prop subject framing guidance (audit G-3 / roadmap 2.9).

Text cards are gone (subtitles-only rendering); the fix for text-bearing
props (documents, posters, signs, calendars, ...) is composition, not just
sanitization: frame the shot at an angle/distance/lighting/detail where
legible text is naturally irrelevant. The storyboard prompt (Claude-facing)
now teaches this as a named, generalizable taxonomy; the deterministic
Python sanitizer (`derive_text_prop_prompt`, Phase 14.7) injects the same
taxonomy as a POSITIVE framing clause — belt-and-suspenders alongside the
existing negative "no readable text" clause, not a replacement for it.

Everything here is pure Python (no Claude/fal.ai call on this path) — driven
directly, nothing stubbed.
"""

import unittest

from app.agents.agent4_visuals.services.flux_generator import (
    _TEXT_PROP_FRAMING_CLAUSES,
    _TEXT_PROP_NO_TEXT_CLAUSE,
    _select_text_prop_framing,
    derive_text_prop_prompt,
    is_text_prop_beat,
)
from app.agents.agent4_visuals.subagents.storyboard import _build_beat_section
from app.agents.agent4_visuals.system_prompt import PROMPT_VERSION, _STORYBOARD_SYSTEM_PROMPT


def _beat(order: int, **overrides) -> dict:
    beat = {
        "beat_order": order,
        "section_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": "narration",
        "visual_intent": "a case file document on a desk",
        "visual_type": "document",
        "environment": "indoor_office",
        "flux_prompt": "a case file document on a desk, photorealistic",
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "document",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
        "media_type": "image",
    }
    beat.update(overrides)
    return beat


class TestSelectTextPropFraming(unittest.TestCase):
    def test_returns_one_of_the_four_clauses(self):
        beat = _beat(0)
        framing = _select_text_prop_framing(beat, "document")
        self.assertIn(framing, _TEXT_PROP_FRAMING_CLAUSES)

    def test_deterministic_same_beat_same_clause(self):
        beat = _beat(3, environment="urban_street")
        first = _select_text_prop_framing(beat, "sign")
        second = _select_text_prop_framing(beat, "sign")
        self.assertEqual(first, second)

    def test_different_beats_can_select_different_clauses(self):
        # Not asserting inequality for a specific pair (hash collisions are
        # legal) — instead confirm the selection varies across a spread of
        # inputs, proving it isn't hardcoded to always return element 0.
        seen = {
            _select_text_prop_framing(_beat(i, environment=env), "document")
            for i, env in enumerate(
                ["indoor_office", "urban_street", "laboratory", "vehicle", "forest_nature"]
            )
        }
        self.assertGreater(len(seen), 1)

    def test_four_distinct_clauses_covering_the_taxonomy(self):
        self.assertEqual(len(_TEXT_PROP_FRAMING_CLAUSES), 4)
        self.assertEqual(len(set(_TEXT_PROP_FRAMING_CLAUSES)), 4)


class TestDeriveTextPropPromptIncludesFraming(unittest.TestCase):
    def test_prompt_contains_both_framing_and_no_text_clause(self):
        beat = _beat(0)
        prompt = derive_text_prop_prompt(beat)
        framing = _select_text_prop_framing(beat, "case file")
        self.assertIn(framing, prompt)
        self.assertIn(_TEXT_PROP_NO_TEXT_CLAUSE, prompt)

    def test_prompt_still_names_the_concrete_prop(self):
        beat = _beat(0, visual_intent="an old missing person poster on a pole",
                     flux_prompt="a missing person poster")
        prompt = derive_text_prop_prompt(beat)
        self.assertIn("missing person poster", prompt)

    def test_repeated_calls_are_byte_identical(self):
        beat = _beat(5, environment="laboratory", visual_intent="a lab report on a bench")
        self.assertEqual(derive_text_prop_prompt(beat), derive_text_prop_prompt(beat))


class TestChainProofThroughBeatBuilding(unittest.TestCase):
    """The real _build_beat_section() path — proves the framing clause
    survives the full beat-construction chain, not just the sanitizer alone."""

    def test_text_prop_beat_gets_framing_clause_in_final_prompt(self):
        raw = _beat(0, flux_prompt='a case file document that reads "CONFIDENTIAL"')
        self.assertTrue(is_text_prop_beat(raw))
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertNotIn('"CONFIDENTIAL"', out["flux_prompt"])
        self.assertTrue(
            any(clause in out["flux_prompt"] for clause in _TEXT_PROP_FRAMING_CLAUSES)
        )

    def test_non_text_prop_beat_is_unaffected(self):
        raw = _beat(
            0,
            visual_intent="a forest path at dawn",
            flux_prompt="a forest path at dawn, photorealistic, wide shot",
            motif="exterior",
            environment="forest_nature",
        )
        self.assertFalse(is_text_prop_beat(raw))
        out = _build_beat_section(raw, 0, 0, 3000, "narration")
        self.assertEqual(out["flux_prompt"], raw["flux_prompt"])


class TestStoryboardPromptTaxonomy(unittest.TestCase):
    """Static proof that the Claude-facing prompt teaches the same taxonomy,
    generalized (not just the four worked examples)."""

    def test_prompt_names_all_four_techniques(self):
        for technique in ("ANGLE", "DISTANCE", "LIGHTING", "DETAIL"):
            self.assertIn(technique, _STORYBOARD_SYSTEM_PROMPT)

    def test_prompt_states_generality_not_just_examples(self):
        self.assertIn("apply it to any text-bearing prop", _STORYBOARD_SYSTEM_PROMPT)

    def test_prompt_still_forbids_readable_text_requests(self):
        self.assertIn("cannot render legible text", _STORYBOARD_SYSTEM_PROMPT)

    def test_version_bumped(self):
        self.assertEqual(PROMPT_VERSION, "4.2")


if __name__ == "__main__":
    unittest.main()
