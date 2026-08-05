"""Offline runtime proofs for remediation-roadmap Tier 5.

No provider client is invoked: routing is exercised as a pure decision, image
checks use local PIL fixtures, and storyboard/remap checks use pure helpers.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import Settings
from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.services import image_router, media_validation
from app.agents.agent4_visuals.subagents import storyboard


class Tier5ImageRoutingTest(unittest.TestCase):
    def test_operator_enabled_defaults_route_person_beat_to_dev(self) -> None:
        defaults = Settings(_env_file=None)
        self.assertTrue(defaults.image_routing_enabled)
        self.assertTrue(defaults.image_routing_allow_dev)
        self.assertFalse(defaults.image_routing_allow_pro)

        route = image_router.select_route(
            {
                "beat_order": 4,
                "visual_category": "person",
                "beat_intensity": "medium",
                "media_strategy": "flux_generated",
                "media_url": "",
            },
            "offline-test",
            routing_enabled=defaults.image_routing_enabled,
            allow_dev=defaults.image_routing_allow_dev,
        )
        self.assertEqual(route.model_key, "dev")
        self.assertEqual(route.reason, "heuristic_qualified_dev")


class Tier5EraAwareDiversityTest(unittest.TestCase):
    def _ledger(self) -> dict:
        beats = [
            {
                "environment": "open_landscape",
                "visual_type": "b-roll",
                "motif": "exterior",
            }
            for _ in range(6)
        ]
        ledger: dict = {}
        storyboard._update_ledger(ledger, beats)
        return ledger

    def test_era_lock_keeps_environment_findings_telemetry_only(self) -> None:
        summary = storyboard._summarize_batch_for_continuity(
            "SECTION 1",
            [
                {
                    "environment": "open_landscape",
                    "visual_type": "b-roll",
                    "motif": "exterior",
                }
            ],
            self._ledger(),
            era_locked=True,
        )
        self.assertNotIn("FORBIDDEN environments", summary)
        self.assertIn("environment totals are telemetry only", summary)
        self.assertIn("environment=other", summary)
        self.assertIn("FORBIDDEN motifs", summary)

    def test_unlocked_story_retains_environment_diversity_cap(self) -> None:
        summary = storyboard._summarize_batch_for_continuity(
            "SECTION 1",
            [
                {
                    "environment": "open_landscape",
                    "visual_type": "b-roll",
                    "motif": "exterior",
                }
            ],
            self._ledger(),
        )
        self.assertIn("FORBIDDEN environments", summary)
        self.assertIn("open_landscape", summary)

    def test_storyboard_prompt_documents_historically_honest_other_value(self) -> None:
        self.assertGreaterEqual(tuple(map(int, system_prompt.PROMPT_VERSION.split("."))), (5, 2))
        self.assertIn("use other", system_prompt._STORYBOARD_SYSTEM_PROMPT)
        self.assertIn("camera distance/angle", system_prompt._STORYBOARD_SYSTEM_PROMPT)


class Tier5LetterboxValidationTest(unittest.TestCase):
    def test_horizontal_black_borders_with_bright_center_are_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "letterboxed.jpg"
            image = Image.new("RGB", (320, 180), (170, 170, 170))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 319, 11), fill=(0, 0, 0))
            draw.rectangle((0, 168, 319, 179), fill=(0, 0, 0))
            image.save(path, quality=100, subsampling=0)

            issue = media_validation._validate_image_letterbox(path)

        self.assertIsNotNone(issue)
        self.assertEqual(issue[0], "letterboxed_image")
        self.assertIn("horizontal", issue[1])

    def test_bright_full_frame_is_not_letterboxed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.jpg"
            Image.new("RGB", (320, 180), (170, 170, 170)).save(path)
            self.assertIsNone(media_validation._validate_image_letterbox(path))

    def test_uniform_dark_frame_is_left_to_luminance_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dark.jpg"
            Image.new("RGB", (320, 180), (4, 4, 4)).save(path)
            self.assertIsNone(media_validation._validate_image_letterbox(path))
            self.assertEqual(
                media_validation._validate_image_luminance(path)[0],
                "near_black_image",
            )


class Tier5FrenchRemapTest(unittest.TestCase):
    def test_normalized_match_reslices_authoritative_french_source(self) -> None:
        script = "À huit heures, l’histoire commence. Puis Léa ferme la porte."
        phrase, cursor, matched = storyboard._reslice_child_narration_phrase(
            script,
            "à huit heures l’histoire commence",
            language="fr",
        )
        self.assertTrue(matched)
        self.assertEqual(phrase, "À huit heures, l’histoire commence")
        self.assertEqual(cursor, script.index("commence") + len("commence"))

    def test_true_paraphrase_is_diagnostic_only_and_not_invented(self) -> None:
        phrase, cursor, matched = storyboard._reslice_child_narration_phrase(
            "Léa ferme la porte rouge.",
            "Léa verrouille la porte rouge",
            language="fr",
        )
        self.assertFalse(matched)
        self.assertEqual(phrase, "Léa verrouille la porte rouge")
        self.assertEqual(cursor, 0)

    def test_remap_prompt_forbids_translation_and_paraphrase(self) -> None:
        self.assertIn("COPY/PASTE one contiguous substring", storyboard._SHORT_REMAP_SYSTEM_PROMPT)
        self.assertIn("Never translate, paraphrase", storyboard._SHORT_REMAP_SYSTEM_PROMPT)
        self.assertIn("PROMPT_VERSION = \"2.1\"", storyboard._SHORT_REMAP_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
