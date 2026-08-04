"""Runtime proof for roadmap Phase 3 item 3b pixel dedupe.

Only the external paid fal wrapper boundary is stubbed. The generation loop,
routing, local image writes, perceptual hash comparison, and one deterministic
composition-slot reroll all execute.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.subagents.storyboard_validator import composition_slot_variation
from app.services import flux_client


def _beat(order: int, prompt: str) -> dict:
    return {
        "beat_order": order,
        "section_order": order,
        "flux_prompt": prompt,
        "environment": "indoor_office",
        "media_url": "",
        "media_type": "image",
        "media_strategy": "flux_generated",
    }


def _write_pattern(path: Path, *, variant: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", (64, 36))
    pixels = image.load()
    for y in range(36):
        for x in range(64):
            if variant == 0:
                value = 255 if x < 32 else 0
            else:
                value = 255 if y < 32 else 0
            pixels[x, y] = value
    image.convert("RGB").save(path)


class PixelDuplicateRerollTest(unittest.TestCase):
    def test_generate_all_rerolls_one_pixel_collision_with_composition_variation(self) -> None:
        calls: list[str] = []

        def fake_call_fal(
            prompt, cache_dir, media_path, cache_key_extra="",
            model_key="schnell", width=1920, height=1080,
        ):
            call_index = len(calls)
            calls.append(prompt)
            local_path = Path(cache_dir) / f"fake-{call_index}.jpg"
            # First two provider results collide exactly; the one deterministic
            # reroll returns a different local image.
            _write_pattern(local_path, variant=0 if call_index < 2 else 1)
            return str(local_path.relative_to(media_path))

        with tempfile.TemporaryDirectory() as tmp:
            beats = [
                _beat(0, "Office desk with case folder, documentary photograph"),
                _beat(1, "Bedroom window with curtain, documentary photograph"),
            ]
            with patch("app.agents.agent4_visuals.services.flux_generator.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.flux_generator.settings.fal_key", "test-key"), \
                 patch("app.services.flux_client._call_fal", side_effect=fake_call_fal):
                result = flux_generator.generate_all_beat_images(beats, "content-1")

        self.assertEqual(len(calls), 3)
        self.assertIn(composition_slot_variation(1), calls[2])
        self.assertEqual(result[0]["media_url"], "cache/content-1/fake-0.jpg")
        self.assertEqual(result[1]["media_url"], "cache/content-1/fake-2.jpg")
        self.assertIn(composition_slot_variation(1), result[1]["flux_prompt"])


if __name__ == "__main__":
    unittest.main()
