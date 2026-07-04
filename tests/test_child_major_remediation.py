"""Runtime proof for deterministic child storyboard MAJOR remediation.

No paid API is stubbed here because this proof calls only deterministic local
logic: validate_storyboard() and remediate_child_major_storyboard_issues().
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals.subagents.storyboard import remediate_child_major_storyboard_issues
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard


class ChildMajorRemediationTest(unittest.TestCase):
    def test_forbidden_prompt_words_are_regenerated_from_visual_intent(self) -> None:
        beats = [
            {
                "beat_order": 0,
                "visual_intent": "Mara studies a broken brass door chain on an apartment threshold",
                "visual_type": "action",
                "visual_category": "object",
                "environment": "corridor_interior",
                "motif": "doorway",
                "flux_prompt": 'Mysterious cinematic hallway atmosphere with a sign that reads "LEAVE NOW"',
                "media_url": "",
                "media_type": "image",
                "beat_intensity": "high",
            }
        ]

        major_issues = [issue for issue in validate_storyboard(beats) if issue["severity"] == "MAJOR"]
        self.assertTrue({issue["check"] for issue in major_issues} & {"forbidden_flux_word", "ai_text_rendering_requested"})

        repaired, repairs = remediate_child_major_storyboard_issues(beats, major_issues)

        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["beat_order"], 0)
        self.assertIn("Mara studies a broken brass door chain", repaired[0]["flux_prompt"])
        self.assertIn("interior corridor", repaired[0]["flux_prompt"])
        self.assertIn("portrait composition", repaired[0]["flux_prompt"])
        self.assertNotIn("mysterious", repaired[0]["flux_prompt"].lower())
        self.assertNotIn("cinematic", repaired[0]["flux_prompt"].lower())
        self.assertNotIn("LEAVE NOW", repaired[0]["flux_prompt"])
        self.assertEqual(beats[0]["flux_prompt"], 'Mysterious cinematic hallway atmosphere with a sign that reads "LEAVE NOW"')

        remaining_majors = [issue for issue in validate_storyboard(repaired) if issue["severity"] == "MAJOR"]
        self.assertEqual(remaining_majors, [])

    def test_global_major_without_beat_target_is_left_for_log_and_proceed(self) -> None:
        beats = [
            {
                "beat_order": 0,
                "visual_intent": "Mara studies a doorway",
                "environment": "corridor_interior",
                "motif": "doorway",
                "flux_prompt": "Vertical documentary photograph of Mara beside a hallway door, practical lamp light, no readable text",
                "media_url": "cache/parent/0.jpg",
                "media_type": "image",
            }
        ]
        major_issues = [
            {"severity": "MAJOR", "beat_order": -1, "check": "excessive_reuse_ratio", "description": "global"}
        ]

        repaired, repairs = remediate_child_major_storyboard_issues(beats, major_issues)

        self.assertIs(repaired, beats)
        self.assertEqual(repairs, [])


if __name__ == "__main__":
    unittest.main()
