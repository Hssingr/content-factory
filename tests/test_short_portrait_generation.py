"""Runtime proof for Short portrait image generation (roadmap 3.1 / audit V-2, G-4.2).

Child Shorts render vertically (Short.tsx, 1080x1920), but before this phase
every child beat's Flux image was requested at the parent's landscape
default (1920x1080) — both genuinely-new beats and beats whose media_url was
a direct pointer at the *parent's* landscape cache file. This proves, with
only the paid fal.ai boundary stubbed (fal_client + httpx.get, following this
repo's established pattern in test_flux_failure_logging.py), that:

  1. image_router.build_cache_key_material() is backward-compatible at the
     Schnell landscape default, and size-disambiguates any other size.
  2. flux_client._call_fal()/generate_beat_image() (moved from flux_generator —
     Agent 6 roadmap Phase A, code_report/agent6_metadata_roadmap.md) and
     flux_generator.generate_beat_image_with_routing() actually request
     portrait pixels end-to-end when given width/height, and produce a
     cache key distinct from the landscape default for the identical prompt.
  3. The real storyboard.generate_pending_beat_images() regenerates EVERY
     beat (both previously-"pending" and previously-"reused") fresh under
     the CHILD's own cache directory at portrait size — never leaving a
     beat pointed at the parent's landscape cache file.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.agent4_visuals.services import flux_generator, image_router
from app.agents.agent4_visuals.subagents import storyboard
from app.services import flux_client


# ── Stub fal.ai boundary (paid API) — never internal logic ────────────────

class _RecordingSyncClient:
    """Stub fal_client.SyncClient that records every request payload and
    returns a fake CDN URL — no network call."""

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


def _fake_httpx_get(*args, **kwargs) -> _FakeHttpxResponse:
    return _FakeHttpxResponse()


_STUB_FAL_CLIENT = types.SimpleNamespace(
    SyncClient=_RecordingSyncClient,
    FalClientError=Exception,
)


def _patched_flux_client():
    """Context manager stack patching only the paid-API boundary
    (``fal_client``/``httpx`` live in ``app.services.flux_client`` since
    Agent 6 roadmap Phase A — ``code_report/agent6_metadata_roadmap.md``)."""
    return (
        patch.object(flux_client, "fal_client", _STUB_FAL_CLIENT),
        patch.object(flux_client.httpx, "get", _fake_httpx_get),
    )


class TestCacheKeyMaterialSizeAwareness(unittest.TestCase):
    def test_schnell_default_landscape_unchanged(self):
        # Exact pre-3.1 material for the default size — zero cache invalidation.
        material = image_router.build_cache_key_material(
            "schnell", "a case file on a desk", width=1920, height=1080,
        )
        self.assertEqual(material, "a case file on a desk")

    def test_schnell_default_landscape_with_extra_unchanged(self):
        material = image_router.build_cache_key_material(
            "schnell", "a case file on a desk", width=1920, height=1080,
            cache_key_extra="hard_retry:3",
        )
        self.assertEqual(material, "hard_retry:3\na case file on a desk")

    def test_schnell_portrait_gets_size_prefix(self):
        material = image_router.build_cache_key_material(
            "schnell", "a case file on a desk", width=1080, height=1920,
        )
        self.assertNotEqual(
            material,
            image_router.build_cache_key_material(
                "schnell", "a case file on a desk", width=1920, height=1080,
            ),
        )
        self.assertIn("size=1080x1920", material)

    def test_portrait_and_landscape_hashes_never_collide(self):
        import hashlib

        landscape = image_router.build_cache_key_material(
            "schnell", "an old missing person poster", width=1920, height=1080,
        )
        portrait = image_router.build_cache_key_material(
            "schnell", "an old missing person poster", width=1080, height=1920,
        )
        h1 = hashlib.sha256(landscape.encode()).hexdigest()[:24]
        h2 = hashlib.sha256(portrait.encode()).hexdigest()[:24]
        self.assertNotEqual(h1, h2)


class TestCallFalRequestsPortraitPixels(unittest.TestCase):
    def test_call_fal_default_size_matches_pre_existing_behavior(self):
        p1, p2 = _patched_flux_client()
        p1.start()
        p2.start()
        orig_fal_key = flux_generator.settings.fal_key
        flux_generator.settings.fal_key = "stub-key"
        _RecordingSyncClient.calls.clear()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                media_path = Path(tmpdir)
                cache_dir = media_path / "cache" / "content-1"
                cache_dir.mkdir(parents=True)
                path = flux_client._call_fal(
                    "a forest path at dawn", cache_dir, media_path, model_key="schnell",
                )
                self.assertIsNotNone(path)
                payload = _RecordingSyncClient.calls[0]["arguments"]
                self.assertEqual(payload["image_size"], {"width": 1920, "height": 1080})
        finally:
            flux_generator.settings.fal_key = orig_fal_key
            p2.stop()
            p1.stop()

    def test_call_fal_portrait_size_is_requested_and_cached_separately(self):
        p1, p2 = _patched_flux_client()
        p1.start()
        p2.start()
        orig_fal_key = flux_generator.settings.fal_key
        flux_generator.settings.fal_key = "stub-key"
        _RecordingSyncClient.calls.clear()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                media_path = Path(tmpdir)
                cache_dir = media_path / "cache" / "content-1"
                cache_dir.mkdir(parents=True)

                landscape_path = flux_client._call_fal(
                    "a forest path at dawn", cache_dir, media_path,
                    model_key="schnell", width=1920, height=1080,
                )
                portrait_path = flux_client._call_fal(
                    "a forest path at dawn", cache_dir, media_path,
                    model_key="schnell", width=1080, height=1920,
                )
                self.assertIsNotNone(landscape_path)
                self.assertIsNotNone(portrait_path)
                self.assertNotEqual(landscape_path, portrait_path)

                payload = _RecordingSyncClient.calls[-1]["arguments"]
                self.assertEqual(payload["image_size"], {"width": 1080, "height": 1920})
                # Two distinct cache files really exist on disk.
                self.assertEqual(len(list(cache_dir.glob("*.jpg"))), 2)
        finally:
            flux_generator.settings.fal_key = orig_fal_key
            p2.stop()
            p1.stop()

    def test_generate_beat_image_with_routing_threads_portrait_size(self):
        p1, p2 = _patched_flux_client()
        p1.start()
        p2.start()
        # Force the conservative-by-default routing contract regardless of
        # this operator's local .env (which has image routing enabled for
        # manual Dev/Pro canary testing) — this test asserts default
        # (Schnell, no size cap) behavior specifically.
        p3 = patch.object(flux_generator.settings, "image_routing_enabled", False)
        p3.start()
        orig_fal_key = flux_generator.settings.fal_key
        orig_media_path = flux_generator.settings.media_path
        flux_generator.settings.fal_key = "stub-key"
        _RecordingSyncClient.calls.clear()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                flux_generator.settings.media_path = tmpdir
                beat = {
                    "beat_order": 0,
                    "flux_prompt": "a lab report on a bench",
                    "environment": "laboratory",
                    "media_url": "",
                }
                tier_counts: dict[str, int] = {}
                path = flux_generator.generate_beat_image_with_routing(
                    beat, "child-content-1", tier_counts, width=1080, height=1920,
                )
                self.assertIsNotNone(path)
                self.assertTrue(path.startswith("cache/child-content-1/"))
                payload = _RecordingSyncClient.calls[-1]["arguments"]
                self.assertEqual(payload["image_size"], {"width": 1080, "height": 1920})
        finally:
            flux_generator.settings.fal_key = orig_fal_key
            flux_generator.settings.media_path = orig_media_path
            p3.stop()
            p2.stop()
            p1.stop()


def _beat(order: int, media_url: str = "", flux_prompt: str = "") -> dict:
    return {
        "beat_order": order,
        "section_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": f"narration {order}",
        "visual_intent": f"intent {order}",
        "visual_type": "b-roll",
        "visual_category": "object",
        "environment": "indoor_office",
        "flux_prompt": flux_prompt or f"concrete subject {order}, wide shot, photorealistic",
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "object",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": media_url,
        "media_type": "image",
    }


class TestGeneratePendingBeatImagesPortraitChain(unittest.TestCase):
    """Drives the real storyboard.generate_pending_beat_images() with only
    the fal.ai boundary stubbed."""

    def test_reused_and_pending_beats_both_regenerate_at_portrait_in_child_cache(self):
        p1, p2 = _patched_flux_client()
        p1.start()
        p2.start()
        # Force the conservative-by-default routing contract regardless of
        # this operator's local .env (which has image routing enabled for
        # manual Dev/Pro canary testing) — beat_order=0 always qualifies as a
        # "cover frame" under the Dev/Pro heuristic, which would otherwise
        # route it to pro_1_1 (max_side=1440) and break this test's portrait
        # size assertion for reasons unrelated to what it's actually proving.
        p3 = patch.object(flux_generator.settings, "image_routing_enabled", False)
        p3.start()
        orig_fal_key = flux_generator.settings.fal_key
        orig_media_path = flux_generator.settings.media_path
        flux_generator.settings.fal_key = "stub-key"
        _RecordingSyncClient.calls.clear()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                flux_generator.settings.media_path = tmpdir
                child_content_id = "child-abc"

                pending_beat = _beat(0, media_url="", flux_prompt="a new scene never seen before")
                reused_beat = _beat(
                    1,
                    media_url="cache/parent-xyz/deadbeef1234.jpg",
                    flux_prompt="a case file document on a desk",
                )
                beats = [pending_beat, reused_beat]

                result = storyboard.generate_pending_beat_images(beats, child_content_id)

                self.assertEqual(len(_RecordingSyncClient.calls), 2)
                for beat in result:
                    self.assertTrue(beat["media_url"].startswith(f"cache/{child_content_id}/"))
                    self.assertNotIn("parent-xyz", beat["media_url"])

                for call in _RecordingSyncClient.calls:
                    self.assertEqual(
                        call["arguments"]["image_size"], {"width": 1080, "height": 1920}
                    )
        finally:
            flux_generator.settings.fal_key = orig_fal_key
            flux_generator.settings.media_path = orig_media_path
            p3.stop()
            p2.stop()
            p1.stop()

    def test_all_fal_calls_failing_leaves_beats_empty_no_parent_fallback(self):
        """Fail-loud contract: if every fresh portrait call hard-fails, a
        previously-reused beat's parent media_url must not silently survive
        (that would reintroduce a landscape image into a vertical Short)."""
        failing_client = types.SimpleNamespace(
            SyncClient=lambda key=None: types.SimpleNamespace(
                run=lambda *a, **k: (_ for _ in ()).throw(Exception("stubbed hard failure"))
            ),
            FalClientError=Exception,
        )
        p1 = patch.object(flux_client, "fal_client", failing_client)
        p1.start()
        orig_fal_key = flux_generator.settings.fal_key
        orig_media_path = flux_generator.settings.media_path
        flux_generator.settings.fal_key = "stub-key"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                flux_generator.settings.media_path = tmpdir
                reused_beat = _beat(0, media_url="cache/parent-xyz/deadbeef1234.jpg")
                beats = [reused_beat]

                result = storyboard.generate_pending_beat_images(beats, "child-abc")

                self.assertEqual(result[0]["media_url"], "")
        finally:
            flux_generator.settings.fal_key = orig_fal_key
            flux_generator.settings.media_path = orig_media_path
            p1.stop()

    def test_empty_beats_list_is_noop(self):
        self.assertEqual(storyboard.generate_pending_beat_images([], "child-abc"), [])


if __name__ == "__main__":
    unittest.main()
