"""Runtime proofs for the image-routing audit fixes.

1. Reuse honored: `generate_beat_image_with_routing()` returns a beat's
   existing local `cache/` media_url untouched — no fal.ai call, no
   tier-count increment (previously a cache-holding beat was mis-counted as
   a Schnell generation and structurally re-entered the generation path).
2. Pro budget: `pro_used_so_far` sums every pro-family tier key, and the
   per-content cap really stops the second qualifying beat from selecting
   Pro even when it qualifies.

Only the paid fal.ai boundary (fal_client + httpx.get) is stubbed — the real
select_route()/generate_beat_image_with_routing()/generate_beat_image()
chain runs, following tests/test_short_portrait_generation.py's precedent.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.agent4_visuals.services import flux_generator


class _RecordingSyncClient:
    calls: list[dict] = []

    def __init__(self, key: str | None = None) -> None:
        self.key = key

    def run(self, endpoint: str, arguments: dict, timeout: float) -> dict:
        _RecordingSyncClient.calls.append({"endpoint": endpoint, "arguments": arguments})
        return {"images": [{"url": "https://fake-cdn.example/image.jpg"}]}


class _FakeHttpxResponse:
    content = b"fake-jpeg-bytes"

    def raise_for_status(self) -> None:
        pass


_STUB_FAL_CLIENT = types.SimpleNamespace(
    SyncClient=_RecordingSyncClient,
    FalClientError=Exception,
)


def _beat(order: int, **overrides) -> dict:
    beat = {
        "beat_order": order,
        "flux_prompt": f"a concrete subject {order}, wide shot, photorealistic",
        "environment": "indoor_office",
        "media_strategy": "flux_generated",
        "media_url": "",
        "beat_intensity": "medium",
        "visual_category": "object",
        "script_text": "plain narration",
    }
    beat.update(overrides)
    return beat


class _FluxHarness(unittest.TestCase):
    """Shared stub setup: fal.ai boundary + temp media dir."""

    def setUp(self):
        self._patches = [
            patch.object(flux_generator, "fal_client", _STUB_FAL_CLIENT),
            patch.object(flux_generator.httpx, "get", lambda *a, **k: _FakeHttpxResponse()),
        ]
        for p in self._patches:
            p.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_media_path = flux_generator.settings.media_path
        self._orig_fal_key = flux_generator.settings.fal_key
        flux_generator.settings.media_path = self._tmp.name
        flux_generator.settings.fal_key = "stub-key"
        _RecordingSyncClient.calls.clear()

    def tearDown(self):
        flux_generator.settings.media_path = self._orig_media_path
        flux_generator.settings.fal_key = self._orig_fal_key
        self._tmp.cleanup()
        for p in self._patches:
            p.stop()


class TestReuseRouteHonored(_FluxHarness):
    def test_resolved_beat_returns_existing_url_without_generation_or_count(self):
        # The reused file must really exist on disk for the short-circuit.
        existing = Path(self._tmp.name) / "cache" / "other-content" / "existing.jpg"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"jpeg")
        beat = _beat(0, media_url="cache/other-content/existing.jpg")
        tier_counts: dict[str, int] = {}

        result = flux_generator.generate_beat_image_with_routing(beat, "content-1", tier_counts)

        self.assertEqual(result, "cache/other-content/existing.jpg")
        self.assertEqual(_RecordingSyncClient.calls, [])   # no fal.ai call
        self.assertEqual(tier_counts, {})                  # no tier count

    def test_resolved_beat_with_missing_file_regenerates_self_healing(self):
        """Cache-wipe recovery: a media_url pointing at a deleted file must
        fall through to real regeneration, never be returned as-is (the
        media validator would otherwise block the content on every re-run)."""
        # order != 0 / medium / object → never heuristic-qualified, so the
        # regeneration lands on Schnell regardless of ambient routing flags.
        beat = _beat(5, media_url="cache/other-content/deleted.jpg")  # not on disk
        tier_counts: dict[str, int] = {}

        result = flux_generator.generate_beat_image_with_routing(beat, "content-1", tier_counts)

        self.assertTrue(result.startswith("cache/content-1/"))
        self.assertEqual(len(_RecordingSyncClient.calls), 1)  # real regeneration
        self.assertEqual(tier_counts, {"schnell": 1})

    def test_pending_beat_still_generates_and_counts_schnell(self):
        beat = _beat(1)
        tier_counts: dict[str, int] = {}

        result = flux_generator.generate_beat_image_with_routing(beat, "content-1", tier_counts)

        self.assertTrue(result.startswith("cache/content-1/"))
        self.assertEqual(len(_RecordingSyncClient.calls), 1)
        self.assertEqual(tier_counts, {"schnell": 1})
        self.assertEqual(_RecordingSyncClient.calls[0]["endpoint"], "fal-ai/flux-1/schnell")


class TestProBudgetAcrossQualifyingBeats(_FluxHarness):
    def _routing_settings(self):
        return (
            patch.object(flux_generator.settings, "image_routing_enabled", True),
            patch.object(flux_generator.settings, "image_routing_allow_pro", True),
            patch.object(flux_generator.settings, "image_routing_allow_dev", True),
            patch.object(flux_generator.settings, "image_routing_max_pro_per_content", 1),
        )

    def test_second_qualifying_beat_is_demoted_once_pro_budget_spent(self):
        patches = self._routing_settings()
        for p in patches:
            p.start()
        try:
            tier_counts: dict[str, int] = {}
            first = _beat(1, beat_intensity="high")   # qualifies (high intensity)
            second = _beat(2, beat_intensity="high")  # qualifies too

            flux_generator.generate_beat_image_with_routing(first, "content-1", tier_counts)
            flux_generator.generate_beat_image_with_routing(second, "content-1", tier_counts)

            endpoints = [c["endpoint"] for c in _RecordingSyncClient.calls]
            self.assertEqual(endpoints[0], "fal-ai/flux-pro/v1.1")   # budget=1 → Pro
            self.assertEqual(endpoints[1], "fal-ai/flux/dev")        # cap hit → Dev
            self.assertEqual(tier_counts, {"pro_1_1": 1, "dev": 1})
        finally:
            for p in patches:
                p.stop()

    def test_non_qualifying_beat_stays_schnell_even_with_routing_enabled(self):
        patches = self._routing_settings()
        for p in patches:
            p.start()
        try:
            tier_counts: dict[str, int] = {}
            plain = _beat(3)  # medium intensity, object, no reveal keyword, order != 0
            flux_generator.generate_beat_image_with_routing(plain, "content-1", tier_counts)
            self.assertEqual(
                _RecordingSyncClient.calls[0]["endpoint"], "fal-ai/flux-1/schnell"
            )
            self.assertEqual(tier_counts, {"schnell": 1})
        finally:
            for p in patches:
                p.stop()

    def test_routing_disabled_everything_schnell(self):
        # Explicitly force the disabled state — the operator's local .env may
        # legitimately enable routing (it does on the dev machine), so this
        # test must never read ambient settings.
        with patch.object(flux_generator.settings, "image_routing_enabled", False):
            tier_counts: dict[str, int] = {}
            cover = _beat(0, beat_intensity="high", visual_category="person")  # max-qualifying
            flux_generator.generate_beat_image_with_routing(cover, "content-1", tier_counts)
            self.assertEqual(_RecordingSyncClient.calls[0]["endpoint"], "fal-ai/flux-1/schnell")
            self.assertEqual(tier_counts, {"schnell": 1})


if __name__ == "__main__":
    unittest.main()
