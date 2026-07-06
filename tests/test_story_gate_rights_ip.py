"""Phase 5 P3-5 proof: rights/IP risk is an operator story-gate signal.

No external APIs are called. These tests exercise the real deterministic scoring
math and Telegram message builder; the Claude scoring boundary is checked
statically through the schema/prompt contract.
"""

from __future__ import annotations

import unittest

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import scoring


def strong_scores() -> dict[str, int]:
    return {dimension: 80 for dimension in scoring._DIMENSION_WEIGHTS}


class StoryGateRightsIpTest(unittest.TestCase):
    def test_static_scoring_contract_requires_rights_ip_risk(self) -> None:
        self.assertIn("rights_ip_risk", system_prompt._SCORING_DIMENSIONS)
        self.assertIn("rights_ip_risk", system_prompt._SINGLE_STORY_SCORING_SCHEMA["properties"]["scores"]["required"])
        self.assertIn("operator-review", system_prompt._SINGLE_STORY_SCORING_SYSTEM_PROMPT)
        self.assertIn("famous authored fiction", system_prompt._SINGLE_STORY_SCORING_SYSTEM_PROMPT)
        self.assertNotIn("rights_ip_risk", scoring._DIMENSION_WEIGHTS)
        self.assertAlmostEqual(sum(scoring._DIMENSION_WEIGHTS.values()), 1.0)

    def test_high_rights_ip_risk_flags_operator_but_does_not_reject(self) -> None:
        scores = strong_scores()
        scores["rights_ip_risk"] = 95

        result = scoring.score_story_assessment({"scores": scores})
        accepted, reason = scoring.decide_story_acceptance(result)

        self.assertEqual(result["dimension_scores"]["rights_ip_risk"], 95)
        self.assertEqual(result["overall_score"], 80.0)
        self.assertEqual(result["failed_gates"], [])
        self.assertTrue(accepted)
        self.assertIn("operator_review_flags=1", reason)
        self.assertEqual(len(result["operator_review_flags"]), 1)
        self.assertIn("operator decision required", result["operator_review_flags"][0])

    def test_telegram_surfaces_rights_ip_risk_separately_not_as_top_signal(self) -> None:
        scores = strong_scores()
        scores["rights_ip_risk"] = 100
        scores["visual_storytelling_potential"] = 88
        scores["social_media_clickability"] = 87

        message = system_prompt.build_telegram_message(
            title="Known Authored Story",
            url="https://example.test/story",
            assessment={"scores": scores},
            target_languages=["en"],
            user_language="en",
        )

        self.assertIn("Rights/IP review", message)
        self.assertIn("rights_ip_risk 100/100", message)
        signal_line = next(line for line in message.splitlines() if "*Top signals:*" in line)
        self.assertNotIn("Rights Ip Risk", signal_line)
        self.assertIn("Visual Storytelling Potential", signal_line)


if __name__ == "__main__":
    unittest.main()
