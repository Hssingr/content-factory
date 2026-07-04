"""Runtime proof for check 18 enforcing the child Short reuse budget."""

from __future__ import annotations

import unittest

from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard


def beat(order: int, *, reused: bool) -> dict:
    return {
        "beat_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": "The short moves to a concrete clue.",
        "visual_intent": "specific hallway clue with visible detail",
        "visual_type": "action",
        "visual_category": "object",
        "environment": "corridor_interior",
        "flux_prompt": (
            f"Hallway clue number {order} beside a practical lamp, brass handle, "
            "documentary photograph, sharp focus, no readable text"
        ),
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "doorway",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": f"cache/parent/{order}.jpg" if reused else "",
    }


def reuse_issues(beats: list[dict]) -> list[dict]:
    return [issue for issue in validate_storyboard(beats) if issue["check"] == "excessive_reuse_ratio"]


class ExcessiveReuseBudgetValidatorTest(unittest.TestCase):
    def test_allows_reuse_at_sixty_percent_budget(self) -> None:
        beats = [beat(i, reused=i < 3) for i in range(5)]
        self.assertEqual(reuse_issues(beats), [])

    def test_reuse_above_sixty_percent_is_major(self) -> None:
        beats = [beat(i, reused=i < 4) for i in range(5)]
        issues = reuse_issues(beats)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "MAJOR")
        self.assertIn("4/5", issues[0]["description"])
        self.assertIn("60% budget", issues[0]["description"])


if __name__ == "__main__":
    unittest.main()
