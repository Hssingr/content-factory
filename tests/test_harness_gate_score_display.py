"""Runtime proof for the harness "Score : ?" fix (roadmap Phase A5, operator
video-output audit): STEP 1 always printed "Score : ?" because
run_discovery()'s 3rd return value (the raw assessment dict, only key
"scores") has no top-level "overall_score" key — that weighted value is
computed by score_story_assessment() *inside* discovery.py and never
returned. _format_gate_score() recomputes it with the same real, pure,
deterministic scoring function (no paid call).

Uses the real score_story_assessment() — the actual production scoring
function, not a mock of it — with real assessment-shaped dicts.
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

from test_full_pipeline import _format_gate_score
from app.agents.agent2_discovery.services.scoring import score_story_assessment


def _real_assessment(**overrides) -> dict:
    scores = {
        "visual_storytelling_potential": 80,
        "scroll_stopper_potential": 75,
        "emotional_stakes": 70,
        "central_mystery": 65,
        "social_media_clickability": 72,
        "conflict_or_contradiction": 60,
        "image_generation_feasibility": 55,
        "rights_ip_risk": 10,
    }
    scores.update(overrides)
    return {"scores": scores}


class FormatGateScoreTest(unittest.TestCase):
    def test_none_assessment_returns_question_mark(self) -> None:
        self.assertEqual(_format_gate_score(None), "?")

    def test_empty_dict_assessment_returns_question_mark(self) -> None:
        self.assertEqual(_format_gate_score({}), "?")

    def test_real_assessment_returns_matching_weighted_overall_score(self) -> None:
        assessment = _real_assessment()
        expected = str(score_story_assessment(assessment)["overall_score"])

        # This is the actual regression: previously the harness read
        # assessment.get("overall_score", "?") directly, which is always "?"
        # since the raw discovery-time assessment dict only has "scores".
        self.assertNotIn("overall_score", assessment)

        result = _format_gate_score(assessment)
        self.assertEqual(result, expected)
        self.assertNotEqual(result, "?")

    def test_result_is_a_real_number_not_a_placeholder(self) -> None:
        result = _format_gate_score(_real_assessment())
        # Must parse as a number — proves it's the real recomputed score,
        # not a stringified fallback/placeholder.
        float(result)


if __name__ == "__main__":
    unittest.main()
