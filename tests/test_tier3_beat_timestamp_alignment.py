"""Offline runtime proofs for remediation-roadmap Tier 3.

No network boundary is invoked. The tests exercise the real deterministic
hint hardening, duplicate-run collapse, timestamp mapper, storyboard prompt
assembly, and per-section timing logger.
"""

from __future__ import annotations

import copy
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.services import visual_orchestrator as vo
from app.agents.agent4_visuals.subagents import storyboard


def _beat(order: int, start_hint: str, end_hint: str, **overrides) -> dict:
    beat = {
        "beat_order": order,
        "start_hint": start_hint,
        "end_hint": end_hint,
        "visual_intent": f"Concrete visual {order}",
        "visual_type": "action",
        "visual_category": "person",
        "environment": "open_landscape",
        "flux_prompt": f"Concrete historically accurate image prompt {order}",
        "effect": "slow_zoom",
        "color_grade": "cold_blue",
        "transition_to_next": "cut",
        "motif": "hands",
        "beat_intensity": "medium",
        "suggested_duration_sec": 1.2,
    }
    beat.update(overrides)
    return beat


class DuplicateHintRunCollapseTest(unittest.TestCase):
    def test_collapses_normalized_run_and_caps_summed_duration(self) -> None:
        duplicate = [
            _beat(
                index + 1,
                "Hannibal swore the oath while frost",
                "oath while frost bit every hand",
                flux_prompt=f"candidate prompt {index}",
                transition_to_next="crossfade" if index == 4 else "cut",
            )
            for index in range(5)
        ]
        # Case and punctuation differences normalize to the same pair.
        duplicate[1]["start_hint"] = "HANNIBAL swore the oath while frost!"
        beats = [
            _beat(0, "Before dawn every soldier gathered beside", "every soldier gathered beside the river"),
            *duplicate,
            _beat(6, "At sunrise the exhausted army finally", "exhausted army finally crossed the pass"),
        ]

        with self.assertLogs(storyboard.logger, level="INFO") as logs:
            collapsed = storyboard._collapse_duplicate_hint_runs(beats)

        self.assertEqual(len(collapsed), 3)
        self.assertEqual([beat["beat_order"] for beat in collapsed], [0, 1, 2])
        self.assertEqual(collapsed[1]["flux_prompt"], "candidate prompt 0")
        self.assertEqual(collapsed[1]["suggested_duration_sec"], 4.0)
        self.assertEqual(collapsed[1]["transition_to_next"], "crossfade")
        self.assertTrue(any("STORYBOARD_DUPLICATE_HINT_RUN_COLLAPSED" in line for line in logs.output))

    def test_collapsed_oath_fixture_maps_through_real_timestamp_chain(self) -> None:
        words = (
            "Before dawn every soldier gathered beside the river "
            "Hannibal swore the oath while frost bit every hand "
            "At sunrise the exhausted army finally crossed the pass"
        ).split()
        transcript = [
            {"word": word, "start": index * 0.4, "end": (index + 1) * 0.4}
            for index, word in enumerate(words)
        ]
        duplicate = [
            _beat(
                index + 1,
                "Hannibal swore the oath while frost",
                "oath while frost bit every hand",
            )
            for index in range(5)
        ]
        collapsed = storyboard._collapse_duplicate_hint_runs([
            _beat(0, "Before dawn every soldier gathered beside", "every soldier gathered beside the river"),
            *duplicate,
            _beat(6, "At sunrise the exhausted army finally", "exhausted army finally crossed the pass"),
        ])

        with self.assertLogs(storyboard.logger, level="WARNING") as logs:
            mapped = storyboard.map_storyboard_beats_to_timestamps(
                collapsed, transcript, len(words) * 400, allow_legacy_fallback=False,
            )

        self.assertIsNotNone(mapped)
        self.assertEqual(len(mapped), 3)
        self.assertTrue(all(not beat["script_text_missing"] for beat in mapped))
        self.assertIn("Hannibal swore the oath", mapped[1]["script_text"])
        self.assertTrue(any("total=3 exact=3 fuzzy=0 fallback=0" in line for line in logs.output))
        self.assertFalse(any("STORYBOARD_SCRIPT_TEXT_EMPTY_FALLBACK" in line for line in logs.output))


class ShortHintExpansionTest(unittest.TestCase):
    def test_short_hints_expand_from_adjacent_quote_stripped_words(self) -> None:
        segment = (
            'The "frozen wind cut through every soldier" as Hannibal raised '
            "his hand before the silent army"
        )
        beats = [_beat(0, "frozen wind cut", "silent army")]

        hardened, stats = storyboard._harden_hints(beats, segment)

        self.assertEqual(hardened[0]["start_hint"], "frozen wind cut through every soldier")
        self.assertEqual(hardened[0]["end_hint"], "his hand before the silent army")
        self.assertEqual(stats, {"total_hints": 2, "valid_hints": 2, "invalid_hints": 0})

    def test_already_valid_hints_are_idempotent(self) -> None:
        segment = "Before dawn every soldier gathered beside the river in silence"
        original = [_beat(
            0,
            "Before dawn every soldier gathered beside",
            "every soldier gathered beside the river",
        )]
        hardened, stats = storyboard._harden_hints(copy.deepcopy(original), segment)
        hardened_twice, second_stats = storyboard._harden_hints(copy.deepcopy(hardened), segment)

        self.assertEqual(hardened, original)
        self.assertEqual(hardened_twice, hardened)
        self.assertEqual(stats, second_stats)

    def test_expansion_uses_forward_occurrence_after_a_valid_prior_beat(self) -> None:
        segment = (
            "Echo rises through the first empty stone hall then silence falls before "
            "echo rises beside the final frozen river bank"
        )
        beats = [
            _beat(0, "Echo rises through the first empty", "first empty stone hall then silence"),
            _beat(1, "echo rises", "frozen river bank"),
        ]

        hardened, stats = storyboard._harden_hints(beats, segment)

        self.assertEqual(
            hardened[1]["start_hint"], "echo rises beside the final frozen",
        )
        self.assertEqual(hardened[1]["end_hint"], "beside the final frozen river bank")
        self.assertEqual(stats["invalid_hints"], 0)

    def test_unlocatable_short_hint_remains_visible_as_invalid_telemetry(self) -> None:
        beats = [_beat(0, "words not present", "also absent")]
        hardened, stats = storyboard._harden_hints(beats, "A completely different narration segment")
        self.assertEqual(hardened[0]["start_hint"], "words not present")
        self.assertEqual(stats["invalid_hints"], 2)


class StoryboardAlignmentPromptTest(unittest.TestCase):
    def test_real_prompt_assembly_contains_both_alignment_rules(self) -> None:
        captured = {}

        def fake_paid_boundary(**kwargs):
            captured.update(kwargs)
            return {"beats": []}, {"input_tokens": 10, "output_tokens": 10}

        with patch.object(
            system_prompt, "call_claude_structured_with_usage", side_effect=fake_paid_boundary,
        ):
            system_prompt.generate_storyboard_batch(
                "[SECTION 1]", "The narrator hears ice crack beneath the army.",
                1, 1, SimpleNamespace(niche="history", tone="tense"), target_beat_count=1,
            )

        prompt = captured["system_prompt"]
        self.assertGreaterEqual(tuple(map(int, system_prompt.PROMPT_VERSION.split("."))), (4, 9))
        self.assertIn("Consecutive beats must NEVER use the same start_hint/end_hint pair", prompt)
        self.assertIn("sensory reveal", prompt)
        self.assertIn("must depict that reveal in THIS beat", prompt)


class SectionAnchoredLogLevelTest(unittest.TestCase):
    def test_successful_section_remap_is_visible_at_info(self) -> None:
        source = [
            {"section_type": "intro", "section_index": None, "start_ms": 0, "end_ms": 1000},
        ]
        target = [
            {"section_type": "intro", "section_index": None, "start_ms": 0, "end_ms": 2000},
        ]
        beats = [{"beat_order": 0, "audio_start_ms": 0, "audio_end_ms": 1000}]

        with self.assertLogs(vo.logger, level="INFO") as logs:
            vo._remap_beats_timing(beats, 2000, 1000, source, target)

        self.assertTrue(any("VISUAL_TIMING_SECTION_ANCHORED" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
