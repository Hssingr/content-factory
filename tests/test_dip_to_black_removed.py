"""Runtime proof that "dip_to_black" is retired end-to-end (roadmap Phase A2,
operator video-output audit): a real production run showed ~13 dip_to_black
beats per parent video, each rendering ~0.67s of full black while narration
continued — indistinguishable from a rendering failure to a viewer.

Covers the Python/storyboard-generation side (schema no longer offers it,
_safe_enum normalizes a stray value away, the real _build_beat_section()
chain never lets it through). The Remotion/render side (MediaSection.tsx
treating any already-persisted "dip_to_black" row as a plain crossfade) is
TypeScript with no test runner configured in this repo — verified instead
via `tsc --noEmit` (clean) and direct source review; see CLAUDE.md's Phase
A2 entry.
"""

from __future__ import annotations

import unittest

from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.subagents import storyboard


class SchemaNoLongerOffersDipToBlackTest(unittest.TestCase):
    def test_beat_schema_enum_excludes_dip_to_black(self) -> None:
        enum = system_prompt._BEAT_SCHEMA["properties"]["transition_to_next"]["enum"]
        self.assertNotIn("dip_to_black", enum)
        # The rest of the enum is untouched — this was a subtraction only.
        self.assertEqual(
            set(enum), {"cut", "crossfade", "whip_pan", "zoom_blur", "match_cut", "none"},
        )

    def test_prompt_text_no_longer_lists_dip_to_black_as_choosable(self) -> None:
        # The one line documenting the enum to Claude; a stray future edit
        # re-adding "dip_to_black" here without touching the schema would
        # desync prompt guidance from what's actually enforced.
        self.assertIn(
            "transition_to_next — cut | crossfade | whip_pan | zoom_blur | match_cut | none",
            system_prompt._STORYBOARD_SYSTEM_PROMPT,
        )


class SafeEnumNormalizationTest(unittest.TestCase):
    def test_valid_transitions_excludes_dip_to_black(self) -> None:
        self.assertNotIn("dip_to_black", storyboard._VALID_TRANSITIONS)

    def test_stray_dip_to_black_value_normalizes_to_default(self) -> None:
        """A value that somehow arrives as "dip_to_black" (e.g. a stale
        in-memory dict from before this fix, or a non-forced-tool-use code
        path) must be coerced to the default, never passed through."""
        result = storyboard._safe_enum(
            "dip_to_black", storyboard._VALID_TRANSITIONS, storyboard._DEFAULT_TRANSITION,
        )
        self.assertEqual(result, "cut")


class BuildBeatSectionRealChainTest(unittest.TestCase):
    """Runtime proof (CLAUDE.md §19.4): the REAL _build_beat_section() —
    the function that turns a raw (possibly Claude-returned or otherwise
    stale) beat dict into the persisted beat-section dict — never lets
    "dip_to_black" survive into what gets saved."""

    def test_dip_to_black_beat_normalizes_to_cut_through_real_build_beat_section(self) -> None:
        raw_beat = {
            "beat_order": 0,
            "flux_prompt": "Empty council chamber, documentary photograph",
            "effect": "slow_zoom",
            "color_grade": "neutral",
            "transition_to_next": "dip_to_black",
            "motif": "room",
        }
        built = storyboard._build_beat_section(raw_beat, 0, 0, 4000, "Narration text.")
        self.assertEqual(built["transition_to_next"], "cut")

    def test_valid_transition_still_passes_through_unchanged(self) -> None:
        raw_beat = {
            "beat_order": 0,
            "flux_prompt": "Empty council chamber, documentary photograph",
            "effect": "slow_zoom",
            "color_grade": "neutral",
            "transition_to_next": "crossfade",
            "motif": "room",
        }
        built = storyboard._build_beat_section(raw_beat, 0, 0, 4000, "Narration text.")
        self.assertEqual(built["transition_to_next"], "crossfade")


if __name__ == "__main__":
    unittest.main()
