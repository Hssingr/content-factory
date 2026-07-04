"""Runtime proof for aggregate visual-monotony escalation.

No external APIs are involved. These tests exercise the real
validate_storyboard boundary so the MAJOR visual_monotony issue is the
same shape consumed by the storyboard retry path.
"""

from __future__ import annotations

import unittest

from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard


ENVIRONMENTS = [
    "indoor_office",
    "urban_street",
    "laboratory",
    "vehicle",
    "corridor_interior",
    "open_landscape",
]
MOTIFS = ["object", "doorway", "hands", "clock", "phone", "photo"]
EFFECTS = ["cut", "pan", "push_in", "zoom_out", "fade_in", "parallax"]


def beat(order: int, *, env: str | None = None, motif: str | None = None, effect: str | None = None) -> dict:
    env = env or ENVIRONMENTS[order % len(ENVIRONMENTS)]
    motif = motif or MOTIFS[order % len(MOTIFS)]
    effect = effect or EFFECTS[order % len(EFFECTS)]
    return {
        "beat_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": "The investigation moves to another concrete clue.",
        "visual_intent": f"specific clue in {env} with distinct composition",
        "visual_type": "action",
        "visual_category": "object",
        "environment": env,
        "flux_prompt": (
            f"Distinct {env} physical clue number {order} beside a practical lamp, "
            f"foreground {motif}, textured surface, documentary photograph, sharp focus, "
            "no readable text"
        ),
        "effect": effect,
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": motif,
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
    }


def issues_for(check: str, beats: list[dict]) -> list[dict]:
    return [issue for issue in validate_storyboard(beats) if issue["check"] == check]


class VisualMonotonyValidatorTest(unittest.TestCase):
    def test_consecutive_same_environment_above_ten_escalates_to_major(self) -> None:
        beats = [
            beat(i, env="indoor_office", motif=MOTIFS[i % len(MOTIFS)], effect=EFFECTS[i % len(EFFECTS)])
            for i in range(13)
        ]

        monotony = issues_for("visual_monotony", beats)

        self.assertEqual(len(monotony), 1)
        self.assertEqual(monotony[0]["severity"], "MAJOR")
        self.assertIn("consecutive_same_environment findings=11 > 10", monotony[0]["description"])

    def test_consecutive_same_environment_at_ten_does_not_escalate(self) -> None:
        beats = [
            beat(i, env="indoor_office", motif=MOTIFS[i % len(MOTIFS)], effect=EFFECTS[i % len(EFFECTS)])
            for i in range(12)
        ]

        self.assertEqual(issues_for("visual_monotony", beats), [])

    def test_ai_slideshow_risk_above_two_escalates_to_major(self) -> None:
        beats: list[dict] = []
        order = 0
        for env in ("indoor_office", "urban_street", "laboratory"):
            for offset in range(5):
                beats.append(
                    beat(
                        order,
                        env=env,
                        motif=MOTIFS[(order + offset) % len(MOTIFS)],
                        effect=EFFECTS[(order + offset) % len(EFFECTS)],
                    )
                )
                order += 1

        self.assertEqual(len(issues_for("ai_slideshow_risk", beats)), 3)
        monotony = issues_for("visual_monotony", beats)

        self.assertEqual(len(monotony), 1)
        self.assertEqual(monotony[0]["severity"], "MAJOR")
        self.assertIn("ai_slideshow_risk findings=3 > 2", monotony[0]["description"])

    def test_document_saturation_also_adds_visual_monotony_major(self) -> None:
        beats = [beat(i) for i in range(20)]
        for i in (0, 6, 12, 18):
            beats[i]["motif"] = "document"

        self.assertTrue(issues_for("document_saturation", beats))
        monotony = issues_for("visual_monotony", beats)

        self.assertEqual(len(monotony), 1)
        self.assertEqual(monotony[0]["severity"], "MAJOR")
        self.assertIn("document_saturation fired", monotony[0]["description"])


if __name__ == "__main__":
    unittest.main()
