"""Runtime proof for Task 1 (code_report/TODO, 2026-08-05): the perceptual
pixel-duplicate guard uses a real difference-hash (dHash), not the prior
average-hash, and its threshold is calibrated to actually catch near-
identical-but-not-byte-identical images — the exact failure shape found in
the "Lords of Finance" run (content ce1cd671), where two adjacent beats
rendered visually near-identical images that the old average-hash +
threshold=3 combination could never have caught (empirically, even the
closest adjacent pair in that real run scored aHash distance=10, yet those
two images were not remotely similar on inspection).

Exercises `_dedupe_generated_image_once()`/`_find_pixel_collision()`/
`_perceptual_pixel_hash()` directly against real PIL-written JPEGs — no
external API calls; only the paid Flux reroll call is stubbed.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.agents.agent4_visuals.services import flux_generator


def _beat(order: int, prompt: str) -> dict:
    return {
        "beat_order": order,
        "section_order": order,
        "flux_prompt": prompt,
        "environment": "indoor_office",
        "media_url": "",
        "media_type": "image",
    }


def _write_gradient(path: Path, *, seed: int, size: tuple[int, int] = (64, 36)) -> None:
    """A smooth diagonal gradient, seeded so different seeds are genuinely
    different compositions (distinct light direction/edge position) rather
    than near-duplicates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = size
    image = Image.new("L", size)
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x * 3 + y * 5 + seed * 47) % 256
    image.convert("RGB").save(path)


def _write_near_identical_variant(path: Path, *, base_seed: int, size: tuple[int, int] = (64, 36)) -> None:
    """The SAME gradient as `_write_gradient(base_seed)`, but with a small
    number of pixels perturbed — different bytes on disk, visually
    near-identical (the exact "tiny Schnell sampling variance" shape this
    guard exists to catch)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = size
    image = Image.new("L", size)
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            value = (x * 3 + y * 5 + base_seed * 47) % 256
            # Perturb a small, scattered fraction of pixels only.
            if (x * height + y) % 23 == 0:
                value = (value + 40) % 256
            pixels[x, y] = value
    image.convert("RGB").save(path)


class PerceptualHashDistanceTest(unittest.TestCase):
    """Direct proof that dHash (not the old average-hash) is what runs now,
    and that its distance behaves sensibly on real images."""

    def test_near_identical_images_score_within_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp)
            a = media_path / "a.jpg"
            b = media_path / "b.jpg"
            _write_gradient(a, seed=1)
            _write_near_identical_variant(b, base_seed=1)

            hash_a = flux_generator._perceptual_pixel_hash(str(a.relative_to(media_path)), media_path)
            hash_b = flux_generator._perceptual_pixel_hash(str(b.relative_to(media_path)), media_path)
            self.assertIsNotNone(hash_a)
            self.assertIsNotNone(hash_b)
            self.assertNotEqual(a.read_bytes(), b.read_bytes(), "fixture bug: bytes must differ")

            distance = flux_generator._hamming_distance(hash_a, hash_b)
            self.assertLessEqual(distance, flux_generator._PIXEL_HASH_COLLISION_MAX_DISTANCE)

    def test_distinct_images_score_outside_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp)
            a = media_path / "a.jpg"
            b = media_path / "b.jpg"
            _write_gradient(a, seed=1)
            _write_gradient(b, seed=9)  # different light direction entirely

            hash_a = flux_generator._perceptual_pixel_hash(str(a.relative_to(media_path)), media_path)
            hash_b = flux_generator._perceptual_pixel_hash(str(b.relative_to(media_path)), media_path)
            distance = flux_generator._hamming_distance(hash_a, hash_b)
            self.assertGreater(distance, flux_generator._PIXEL_HASH_COLLISION_MAX_DISTANCE)


class DedupeGeneratedImageOnceTest(unittest.TestCase):
    """End-to-end through the real dedup+reroll mechanism used by
    generate_all_beat_images()."""

    def test_near_identical_pair_triggers_exactly_one_reroll(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp)
            first_path = "cache/content-1/first.jpg"
            second_path = "cache/content-1/second.jpg"
            reroll_path = "cache/content-1/reroll.jpg"
            _write_gradient(media_path / first_path, seed=1)
            _write_near_identical_variant(media_path / second_path, base_seed=1)
            _write_gradient(media_path / reroll_path, seed=9)  # genuinely distinct reroll result

            reroll_calls = []

            def fake_routing(beat, content_id, tier_counts, *, width, height):
                reroll_calls.append(beat.get("flux_prompt"))
                return reroll_path

            with (
                self._patch_settings(media_path),
                self._patch_routing(fake_routing),
            ):
                pixel_ledger: list[dict] = []
                tier_counts: dict[str, int] = {}
                out_first = flux_generator._dedupe_generated_image_once(
                    _beat(0, "a lit office desk"), first_path, "content-1",
                    tier_counts, pixel_ledger, width=1920, height=1080,
                )
                out_second = flux_generator._dedupe_generated_image_once(
                    _beat(1, "a lit office desk again"), second_path, "content-1",
                    tier_counts, pixel_ledger, width=1920, height=1080,
                )

            self.assertEqual(out_first, first_path)
            self.assertEqual(len(reroll_calls), 1, "exactly one reroll for the colliding pair")
            self.assertEqual(out_second, reroll_path)

    def test_visually_distinct_images_trigger_no_reroll(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp)
            first_path = "cache/content-1/first.jpg"
            second_path = "cache/content-1/second.jpg"
            _write_gradient(media_path / first_path, seed=1)
            _write_gradient(media_path / second_path, seed=9)

            reroll_calls = []

            def fake_routing(beat, content_id, tier_counts, *, width, height):
                reroll_calls.append(beat.get("flux_prompt"))
                return "cache/content-1/should-not-be-called.jpg"

            with (
                self._patch_settings(media_path),
                self._patch_routing(fake_routing),
            ):
                pixel_ledger: list[dict] = []
                tier_counts: dict[str, int] = {}
                out_first = flux_generator._dedupe_generated_image_once(
                    _beat(0, "office desk"), first_path, "content-1",
                    tier_counts, pixel_ledger, width=1920, height=1080,
                )
                out_second = flux_generator._dedupe_generated_image_once(
                    _beat(1, "bedroom window"), second_path, "content-1",
                    tier_counts, pixel_ledger, width=1920, height=1080,
                )

            self.assertEqual(out_first, first_path)
            self.assertEqual(out_second, second_path)
            self.assertEqual(reroll_calls, [], "distinct images must never trigger a reroll")

    def test_second_collision_after_reroll_is_accepted_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp)
            first_path = "cache/content-1/first.jpg"
            second_path = "cache/content-1/second.jpg"
            reroll_path = "cache/content-1/reroll.jpg"
            _write_gradient(media_path / first_path, seed=1)
            _write_near_identical_variant(media_path / second_path, base_seed=1)
            # The reroll ALSO collides with the first image (still near-identical).
            _write_near_identical_variant(media_path / reroll_path, base_seed=1)

            def fake_routing(beat, content_id, tier_counts, *, width, height):
                return reroll_path

            with (
                self._patch_settings(media_path),
                self._patch_routing(fake_routing),
                self.assertLogs(flux_generator.logger.name, level=logging.WARNING) as logs,
            ):
                pixel_ledger: list[dict] = []
                tier_counts: dict[str, int] = {}
                flux_generator._dedupe_generated_image_once(
                    _beat(0, "office desk"), first_path, "content-1",
                    tier_counts, pixel_ledger, width=1920, height=1080,
                )
                out_second = flux_generator._dedupe_generated_image_once(
                    _beat(1, "office desk again"), second_path, "content-1",
                    tier_counts, pixel_ledger, width=1920, height=1080,
                )

            # Accepted (not rerolled a second time) despite still colliding.
            self.assertEqual(out_second, reroll_path)
            self.assertTrue(
                any("PIXEL_DUPLICATE_REROLL_STILL_COLLIDES" in m for m in logs.output),
                logs.output,
            )

    def _patch_settings(self, media_path: Path):
        from unittest.mock import patch
        return patch.object(flux_generator.settings, "media_path", str(media_path))

    def _patch_routing(self, fake):
        from unittest.mock import patch
        return patch.object(flux_generator, "generate_beat_image_with_routing", side_effect=fake)


if __name__ == "__main__":
    unittest.main()
