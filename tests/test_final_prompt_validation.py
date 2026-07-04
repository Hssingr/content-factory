"""Runtime proof for Phase 2.2 final Flux prompt validation.

No live API calls: ``fal_client`` is stubbed only because importing the visual
orchestrator imports the Flux provider wrapper. The validation path itself uses
real internal logic: ``_check_final_prompt_issues`` calls the production
``validate_storyboard`` implementation and filters the final-prompt check set.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from uuid import uuid4

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals.services.visual_orchestrator import _check_final_prompt_issues


def beat(order: int, prompt: str, **overrides) -> dict:
    data = {
        "beat_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": "The hallway detail changes as Eli notices the door.",
        "visual_intent": "Eli notices a specific hallway door detail",
        "visual_type": "action",
        "visual_category": "person",
        "environment": "corridor_interior",
        "flux_prompt": prompt,
        "effect": "slow_zoom",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "doorway",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
    }
    data.update(overrides)
    return data


class FinalPromptValidationTest(unittest.TestCase):
    def test_clean_final_prompt_has_no_filtered_issues(self) -> None:
        issues = _check_final_prompt_issues(
            [
                beat(
                    0,
                    "Eli in a narrow interior hallway beside a half open bedroom door, "
                    "peeling wallpaper, visible brass handle, practical lamp glow, "
                    "documentary photograph, sharp focus, no readable text",
                )
            ],
            content_id=uuid4(),
            stage="unit",
            language="en",
        )
        self.assertEqual(issues, [])

    def test_major_final_prompt_issue_is_returned_for_blocking(self) -> None:
        issues = _check_final_prompt_issues(
            [
                beat(
                    0,
                    "A sign that reads MISSING ELI in a cinematic hallway atmosphere",
                )
            ],
            content_id=uuid4(),
            stage="unit",
            language="en",
        )
        majors = [issue for issue in issues if issue["severity"] == "MAJOR"]
        self.assertTrue(majors)
        self.assertIn("forbidden_flux_word", {issue["check"] for issue in majors})
        self.assertIn("ai_text_rendering_requested", {issue["check"] for issue in majors})

    def test_minor_final_prompt_subset_includes_duplicate_checks_only(self) -> None:
        prompt = (
            "Eli in a narrow interior hallway beside a half open bedroom door, "
            "peeling wallpaper, visible brass handle, practical lamp glow, "
            "documentary photograph, sharp focus, no readable text"
        )
        near_prompt = prompt.replace("visible brass handle", "visible brass doorknob")
        issues = _check_final_prompt_issues(
            [
                beat(0, prompt),
                beat(1, prompt, motif="hands", effect="cut"),
                beat(2, near_prompt, motif="object", effect="pan"),
            ],
            content_id=uuid4(),
            stage="unit",
            language="en",
        )
        checks = {issue["check"] for issue in issues}
        self.assertIn("flux_prompt_exact_duplicate", checks)
        self.assertIn("flux_prompt_near_duplicate", checks)
        self.assertTrue(checks <= {
            "forbidden_flux_word",
            "subject_presence",
            "environment_presence",
            "low_information_prompt",
            "flux_prompt_exact_duplicate",
            "flux_prompt_near_duplicate",
            "ai_text_rendering_requested",
        })


if __name__ == "__main__":
    unittest.main()
