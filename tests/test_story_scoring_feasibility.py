"""Runtime proof for roadmap 4.5 story scoring feasibility rename.

No external APIs are called. These tests exercise the real deterministic scoring
service and statically inspect the Agent 2 scoring schema/prompt.
"""

from __future__ import annotations

import unittest

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import scoring


def strong_scores() -> dict[str, int]:
    return {dimension: 80 for dimension in scoring._DIMENSION_WEIGHTS}


class StoryScoringFeasibilityTest(unittest.TestCase):
    def test_scoring_uses_image_generation_feasibility_not_stock_media(self) -> None:
        self.assertIn("image_generation_feasibility", scoring._DIMENSION_WEIGHTS)
        self.assertNotIn("stock_media_feasibility", scoring._DIMENSION_WEIGHTS)
        self.assertAlmostEqual(sum(scoring._DIMENSION_WEIGHTS.values()), 1.0)

        self.assertIn("image_generation_feasibility", system_prompt._SCORING_DIMENSIONS)
        self.assertNotIn("stock_media_feasibility", system_prompt._SCORING_DIMENSIONS)
        self.assertIn("image_generation_feasibility", system_prompt._SINGLE_STORY_SCORING_SYSTEM_PROMPT)
        self.assertNotIn("Pexels", system_prompt._SINGLE_STORY_SCORING_SYSTEM_PROMPT)
        self.assertNotIn("Unsplash", system_prompt._SINGLE_STORY_SCORING_SYSTEM_PROMPT)
        self.assertNotIn("Pixabay", system_prompt._SINGLE_STORY_SCORING_SYSTEM_PROMPT)

    def test_image_generation_feasibility_floor_rejects_low_score(self) -> None:
        scores = strong_scores()
        scores["image_generation_feasibility"] = 39

        result = scoring.score_story_assessment({"scores": scores})

        self.assertIn("image_generation_feasibility", result["dimension_scores"])
        self.assertNotIn("stock_media_feasibility", result["dimension_scores"])
        self.assertIn("image_generation_feasibility 39 < 40", result["failed_gates"])
        accepted, reason = scoring.decide_story_acceptance(result)
        self.assertFalse(accepted)
        self.assertIn("image_generation_feasibility 39 < 40", reason)

    def test_legacy_stock_media_alias_removed(self) -> None:
        """Fresh full-system audit §4: the legacy key aliases are deleted — no
        producer of stock_media_feasibility exists (every assessment comes from
        score_story_for_gate's forced tool-use schema, which requires the
        canonical names). A legacy key is now simply ignored and the canonical
        dimension scores 0, failing its floor loudly instead of silently
        aliasing."""
        scores = strong_scores()
        scores.pop("image_generation_feasibility")
        scores["stock_media_feasibility"] = 80

        result = scoring.score_story_assessment({"scores": scores})

        self.assertEqual(result["dimension_scores"]["image_generation_feasibility"], 0)
        self.assertIn("image_generation_feasibility 0 < 40", result["failed_gates"])


if __name__ == "__main__":
    unittest.main()
