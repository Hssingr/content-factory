"""Runtime proof for Phase 2.3 document-saturation validation.

No external APIs are involved. These tests exercise the real
``validate_storyboard`` function so the MAJOR issue shape is the same one used
by parent retry and child-remap logging paths.
"""

from __future__ import annotations

import unittest

from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard


def beat(order: int, *, motif: str = "object", visual_type: str = "action") -> dict:
    return {
        "beat_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": "The scene changes around a hallway clue.",
        "visual_intent": "specific hallway clue with visible physical detail",
        "visual_type": visual_type,
        "visual_category": "object",
        "environment": "corridor_interior",
        "flux_prompt": (
            "Interior hallway detail beside a half open bedroom door, peeling "
            "wallpaper, brass handle, practical lamp glow, documentary photograph, "
            "sharp focus, no readable text"
        ),
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": motif,
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
    }


def document_issues(beats: list[dict]) -> list[dict]:
    return [issue for issue in validate_storyboard(beats) if issue["check"] == "document_saturation"]


class DocumentSaturationValidatorTest(unittest.TestCase):
    def test_allows_document_rate_at_or_below_threshold(self) -> None:
        beats = [beat(i) for i in range(20)]
        for i in (0, 8, 16):
            beats[i]["motif"] = "document"

        self.assertEqual(document_issues(beats), [])

    def test_global_document_rate_above_fifteen_percent_is_major(self) -> None:
        beats = [beat(i) for i in range(20)]
        for i in (0, 6, 12, 18):
            beats[i]["motif"] = "document"

        issues = document_issues(beats)
        self.assertTrue(issues)
        self.assertTrue(all(issue["severity"] == "MAJOR" for issue in issues))
        self.assertIn("4/20", issues[0]["description"])

    def test_local_window_above_two_documents_is_major_even_at_global_threshold(self) -> None:
        beats = [beat(i) for i in range(20)]
        for i in (0, 1, 2):
            beats[i]["visual_type"] = "document"

        issues = document_issues(beats)
        self.assertTrue(issues)
        self.assertTrue(any("10-beat window" in issue["description"] for issue in issues))
        self.assertTrue(all(issue["severity"] == "MAJOR" for issue in issues))

    def test_visual_type_document_counts_even_when_motif_is_not_document(self) -> None:
        beats = [beat(i) for i in range(10)]
        beats[3]["visual_type"] = "document"
        beats[3]["motif"] = "object"
        beats[7]["visual_type"] = "document"
        beats[7]["motif"] = "object"

        issues = document_issues(beats)
        self.assertTrue(issues)
        self.assertTrue(all(issue["severity"] == "MAJOR" for issue in issues))


if __name__ == "__main__":
    unittest.main()
