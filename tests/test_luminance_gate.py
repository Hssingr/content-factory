"""Runtime proof for the image luminance gate (roadmap Phase A1, operator
video-output audit — a real production run shipped a beat whose Flux
generation was a 100% black JPEG; nothing before this fix inspected pixel
content).

Only the external paid fal wrapper boundary (``_call_fal``) is stubbed. The
generation loop, routing, local image writes, real luminance measurement via
PIL, the one-shot well-lit reroll, and neighbor-fill hand-off all execute
for real — mirrors the existing pattern in ``test_pixel_duplicate_reroll.py``.
"""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.services import media_validation
from app.agents.agent4_visuals.subagents import storyboard


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


def _write_solid(path: Path, *, luminance: int, seed: int = 0, size: tuple[int, int] = (64, 36)) -> None:
    """Write a real JPEG whose mean grayscale value is ~``luminance``.

    Deliberately NOT a perfectly uniform fill: a flat single-color image
    always hashes identically under _average_pixel_hash() regardless of its
    absolute brightness (average-hash only encodes relative pattern —
    "pixel >= own mean" is trivially true everywhere on a flat image), so
    two solid images at very different luminance levels would incorrectly
    collide as pixel-duplicates. A small per-pixel seeded noise band (kept
    tight enough to leave the mean within the caller's target band) gives
    each fixture a distinct pattern while keeping luminance controllable —
    same tradeoff a real photographed/generated frame's texture provides.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            value = max(0, min(255, luminance + rng.randint(-8, 8)))
            pixels[x, y] = (value, value, value)
    image.save(path, format="JPEG")


class LuminanceGateGenerationTest(unittest.TestCase):
    def test_dark_image_rerolls_once_and_succeeds_when_reroll_is_bright(self) -> None:
        calls: list[str] = []

        def fake_call_fal(
            prompt, cache_dir, media_path, cache_key_extra="",
            model_key="schnell", width=1920, height=1080,
        ):
            call_index = len(calls)
            calls.append(prompt)
            local_path = Path(cache_dir) / f"fake-{call_index}.jpg"
            # First generation is pure black; the one reroll is bright.
            _write_solid(local_path, luminance=0 if call_index == 0 else 200, seed=call_index)
            return str(local_path.relative_to(media_path))

        with tempfile.TemporaryDirectory() as tmp:
            beats = [_beat(0, "Empty council chamber, documentary photograph")]
            with (
                patch("app.agents.agent4_visuals.services.flux_generator.settings.media_path", tmp),
                patch("app.agents.agent4_visuals.services.flux_generator.settings.fal_key", "test-key"),
                patch("app.agents.agent4_visuals.services.flux_generator._call_fal", side_effect=fake_call_fal),
            ):
                result = flux_generator.generate_all_beat_images(beats, "content-1")

        # Exactly one reroll: the initial generation + one well-lit retry.
        self.assertEqual(len(calls), 2)
        self.assertIn(flux_generator._WELL_LIT_REROLL_CLAUSE, calls[1])
        # The final beat uses the (bright) rerolled image, not the dark original.
        self.assertEqual(result[0]["media_url"], "cache/content-1/fake-1.jpg")
        self.assertIn(flux_generator._WELL_LIT_REROLL_CLAUSE, result[0]["flux_prompt"])

    def test_still_dark_after_reroll_hands_off_to_neighbor_fill(self) -> None:
        calls: list[str] = []

        def fake_call_fal(
            prompt, cache_dir, media_path, cache_key_extra="",
            model_key="schnell", width=1920, height=1080,
        ):
            call_index = len(calls)
            calls.append(prompt)
            local_path = Path(cache_dir) / f"fake-{call_index}.jpg"
            # Beat 0 is bright on the first (only) try. Beat 1's first AND
            # reroll attempt both come back pure black.
            if call_index == 0:
                _write_solid(local_path, luminance=200, seed=call_index)
            else:
                _write_solid(local_path, luminance=0, seed=call_index)
            return str(local_path.relative_to(media_path))

        with tempfile.TemporaryDirectory() as tmp:
            beats = [
                _beat(0, "Bright harbor at midday, documentary photograph"),
                _beat(1, "Council chamber at night, documentary photograph"),
            ]
            with (
                patch("app.agents.agent4_visuals.services.flux_generator.settings.media_path", tmp),
                patch("app.agents.agent4_visuals.services.flux_generator.settings.fal_key", "test-key"),
                patch("app.agents.agent4_visuals.services.flux_generator._call_fal", side_effect=fake_call_fal),
            ):
                result = flux_generator.generate_all_beat_images(beats, "content-1")

        # Beat 0: one call, bright, kept as-is.
        # Beat 1: three calls (original + heal attempt 1 corrective clause +
        # heal attempt 2 deterministic prompt rewrite), all dark → handed to
        # neighbor-fill, which reuses beat 0's bright image.
        self.assertEqual(len(calls), 4)
        self.assertEqual(result[0]["media_url"], "cache/content-1/fake-0.jpg")
        self.assertEqual(result[1]["media_url"], result[0]["media_url"])

    def test_bright_image_never_triggers_a_reroll(self) -> None:
        calls: list[str] = []

        def fake_call_fal(
            prompt, cache_dir, media_path, cache_key_extra="",
            model_key="schnell", width=1920, height=1080,
        ):
            calls.append(prompt)
            local_path = Path(cache_dir) / f"fake-{len(calls) - 1}.jpg"
            _write_solid(local_path, luminance=180, seed=len(calls) - 1)
            return str(local_path.relative_to(media_path))

        with tempfile.TemporaryDirectory() as tmp:
            beats = [_beat(0, "Sunlit harbor, documentary photograph")]
            with (
                patch("app.agents.agent4_visuals.services.flux_generator.settings.media_path", tmp),
                patch("app.agents.agent4_visuals.services.flux_generator.settings.fal_key", "test-key"),
                patch("app.agents.agent4_visuals.services.flux_generator._call_fal", side_effect=fake_call_fal),
            ):
                result = flux_generator.generate_all_beat_images(beats, "content-1")

        self.assertEqual(len(calls), 1)
        self.assertEqual(result[0]["media_url"], "cache/content-1/fake-0.jpg")
        self.assertNotIn(flux_generator._WELL_LIT_REROLL_CLAUSE, result[0]["flux_prompt"])


class ChildShortLuminanceGateTest(unittest.TestCase):
    """Same gate, child Short path (generate_pending_beat_images) — proves
    the wiring was added to both call sites, not just the parent's."""

    def test_dark_child_beat_rerolls_and_succeeds(self) -> None:
        calls: list[str] = []

        def fake_call_fal(
            prompt, cache_dir, media_path, cache_key_extra="",
            model_key="schnell", width=1920, height=1080,
        ):
            call_index = len(calls)
            calls.append(prompt)
            local_path = Path(cache_dir) / f"fake-{call_index}.jpg"
            # Portrait fixture (9:16) — the child path's size gate would
            # correctly flag a landscape/square image.
            _write_solid(
                local_path, luminance=0 if call_index == 0 else 200,
                seed=call_index, size=(36, 64),
            )
            return str(local_path.relative_to(media_path))

        with tempfile.TemporaryDirectory() as tmp:
            beats = [_beat(0, "Empty council chamber, documentary photograph")]
            with (
                patch("app.agents.agent4_visuals.services.flux_generator.settings.media_path", tmp),
                patch("app.agents.agent4_visuals.services.flux_generator.settings.fal_key", "test-key"),
                patch("app.agents.agent4_visuals.services.flux_generator._call_fal", side_effect=fake_call_fal),
            ):
                result = storyboard.generate_pending_beat_images(beats, "child-content-1")

        self.assertEqual(len(calls), 2)
        self.assertIn(flux_generator._WELL_LIT_REROLL_CLAUSE, calls[1])
        self.assertEqual(result[0]["media_url"], "cache/child-content-1/fake-1.jpg")


class MeanLuminanceUnitTest(unittest.TestCase):
    def test_mean_luminance_measures_real_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp)
            dark_path = media_path / "dark.jpg"
            bright_path = media_path / "bright.jpg"
            _write_solid(dark_path, luminance=0)
            _write_solid(bright_path, luminance=220)

            dark_luminance = flux_generator._mean_luminance("dark.jpg", media_path)
            bright_luminance = flux_generator._mean_luminance("bright.jpg", media_path)

        self.assertIsNotNone(dark_luminance)
        self.assertIsNotNone(bright_luminance)
        self.assertLess(dark_luminance, flux_generator._LUMINANCE_MEAN_FLOOR)
        self.assertGreaterEqual(bright_luminance, flux_generator._LUMINANCE_MEAN_FLOOR)

    def test_mean_luminance_unreadable_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp)
            self.assertIsNone(flux_generator._mean_luminance("does-not-exist.jpg", media_path))


class MediaValidationLuminanceCheckTest(unittest.TestCase):
    def test_rejects_near_black_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "black.jpg"
            _write_solid(path, luminance=2)
            issue = media_validation._validate_image_luminance(path)
        self.assertIsNotNone(issue)
        self.assertEqual(issue[0], "near_black_image")

    def test_accepts_well_lit_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bright.jpg"
            _write_solid(path, luminance=180)
            issue = media_validation._validate_image_luminance(path)
        self.assertIsNone(issue)

    def test_borderline_at_floor_is_accepted_not_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "borderline.jpg"
            _write_solid(path, luminance=int(media_validation._NEAR_BLACK_LUMINANCE_FLOOR) + 1)
            issue = media_validation._validate_image_luminance(path)
        self.assertIsNone(issue)


if __name__ == "__main__":
    unittest.main()
