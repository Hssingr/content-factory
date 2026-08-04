"""Runtime proof for the harness's score display, `_format_gate_score()`.

Design changed twice since this test was first written:
  1. Originally the harness always printed "Score : ?" because
     ``run_discovery()``'s 3rd return value was the RAW assessment dict
     (only key ``"scores"``), with no top-level ``overall_score`` —
     ``_format_gate_score()`` was fixed to re-derive the weighted score via
     ``score_story_assessment()``.
  2. The score gate itself was then changed to never block the pipeline
     (operator decision, CLAUDE.md §9.3's ``run_discovery`` entry):
     ``run_discovery()``'s 3rd return value is now ALREADY the weighted
     ``story_score`` dict (``overall_score``/``dimension_scores``/
     ``failed_gates``), so ``_format_gate_score()`` no longer re-derives
     anything — it reads the value straight through and also surfaces the
     gate verdict (``PASSED``/``BELOW_FLOOR``) so the harness's CLI output
     matches what the Telegram message shows.

Uses real ``story_score``-shaped dicts (``score_story_assessment()``'s real
output shape), not a mock.
"""

from __future__ import annotations

import unittest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "test_pipeline") not in sys.path:
    sys.path.insert(0, str(ROOT / "test_pipeline"))

from test_full_pipeline import DISCOVERY_NONE_MESSAGE, _format_gate_score
from app.agents.agent2_discovery.services.scoring import score_story_assessment


def _real_story_score(**overrides) -> dict:
    """A real, fully-weighted story_score dict — the exact shape
    run_discovery() now returns as its 3rd tuple value."""
    dimension_scores = {
        "visual_storytelling_potential": 80,
        "scroll_stopper_potential": 75,
        "emotional_stakes": 70,
        "central_mystery": 65,
        "social_media_clickability": 72,
        "conflict_or_contradiction": 60,
        "image_generation_feasibility": 55,
        "rights_ip_risk": 10,
    }
    story_score = score_story_assessment({"scores": dimension_scores})
    story_score.update(overrides)
    return story_score


class FormatGateScoreTest(unittest.TestCase):
    def test_discovery_none_message_no_longer_blames_score_gate(self) -> None:
        # The score gate never causes run_discovery() to return None
        # (operator decision) — the None-result message must not imply it
        # does, and must say what actually can cause a None result instead.
        self.assertNotIn("failed score gates", DISCOVERY_NONE_MESSAGE)
        self.assertIn("never blocks", DISCOVERY_NONE_MESSAGE)

    def test_none_story_score_returns_question_mark(self) -> None:
        self.assertEqual(_format_gate_score(None), "?")

    def test_empty_dict_story_score_returns_question_mark(self) -> None:
        self.assertEqual(_format_gate_score({}), "?")

    def test_real_story_score_shows_actual_overall_score(self) -> None:
        story_score = _real_story_score()
        result = _format_gate_score(story_score)
        self.assertIn(str(story_score["overall_score"]), result)
        self.assertNotEqual(result, "?")

    def test_passing_score_shows_passed_verdict(self) -> None:
        story_score = _real_story_score()
        self.assertEqual(story_score["failed_gates"], [])  # sanity: fixture clears every floor
        self.assertIn("PASSED", _format_gate_score(story_score))

    def test_below_floor_score_shows_below_floor_verdict(self) -> None:
        story_score = _real_story_score(
            overall_score=50.0, failed_gates=["overall_score 50.0 < 65"],
        )
        self.assertIn("BELOW_FLOOR", _format_gate_score(story_score))


if __name__ == "__main__":
    unittest.main()
