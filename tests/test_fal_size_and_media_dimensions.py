"""Runtime proof for forensic roadmap Phase 3 item 3a.

No external APIs are called. Payload construction is pure local logic; media
validation uses local PIL images and a fake DB session.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.agents.agent4_visuals.services import image_router
from app.agents.agent4_visuals.services.media_validation import validate_visual_media_assets
from app.models import Content, VideoSection


class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        self.rows.sort(key=lambda row: (getattr(row, "language", ""), getattr(row, "section_order", 0)))
        return self

    def all(self):
        return list(self.rows)


class _FakeDb:
    def __init__(self, content, sections):
        self.content = content
        self.sections = list(sections)

    def get(self, model, content_id):
        if model is Content and str(content_id) == str(self.content.id):
            return self.content
        return None

    def query(self, model):
        if model is VideoSection:
            return _FakeQuery(self.sections)
        return _FakeQuery([])


def _section(content_id, media_url: str):
    return SimpleNamespace(
        content_id=content_id,
        language="en",
        section_order=1,
        generation_prompt=json.dumps({
            "media_url": media_url,
            "media_type": "image",
            "visual_type": "b-roll",
        }),
        media_strategy="flux_generated",
        text_card_style="default",
    )


def _write_image(path: Path, size: tuple[int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color="green").save(path)


class FalSizeAndMediaDimensionTest(unittest.TestCase):
    def test_pro_v1_1_payload_scales_to_fal_legal_size_without_losing_aspect(self) -> None:
        portrait = image_router.build_fal_payload("pro_1_1", "prompt", width=1080, height=1920)
        landscape = image_router.build_fal_payload("pro_1_1", "prompt", width=1920, height=1080)

        self.assertEqual(portrait["image_size"], {"width": 816, "height": 1440})
        self.assertEqual(landscape["image_size"], {"width": 1440, "height": 816})
        self.assertNotIn("aspect_ratio", portrait)

    def test_non_pro_size_payloads_preserve_existing_dimensions(self) -> None:
        schnell = image_router.build_fal_payload("schnell", "prompt", width=1920, height=1080)
        dev = image_router.build_fal_payload("dev", "prompt", width=1080, height=1920)

        self.assertEqual(schnell["image_size"], {"width": 1920, "height": 1080})
        self.assertEqual(dev["image_size"], {"width": 1080, "height": 1920})

    def test_ultra_aspect_ratio_follows_requested_frame_orientation(self) -> None:
        portrait = image_router.build_fal_payload("pro_1_1_ultra", "prompt", width=1080, height=1920)
        landscape = image_router.build_fal_payload("pro_1_1_ultra", "prompt", width=1920, height=1080)

        self.assertEqual(portrait["aspect_ratio"], "9:16")
        self.assertEqual(landscape["aspect_ratio"], "16:9")
        self.assertNotIn("image_size", portrait)

    def test_media_validator_blocks_short_four_by_three_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            content_id = uuid.uuid4()
            bad = media_root / "cache" / str(content_id) / "bad.jpg"
            _write_image(bad, (1056, 1440))
            content = SimpleNamespace(id=content_id, is_short_episode=True)
            db = _FakeDb(content, [_section(content_id, f"cache/{content_id}/bad.jpg")])

            with (
                patch("app.agents.agent4_visuals.services.media_validation.settings.media_path", str(media_root)),
                patch("app.services.local_run_paths.settings.media_path", str(media_root)),
            ):
                result = validate_visual_media_assets(content_id, db)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "local_image_aspect_mismatch")

    def test_media_validator_allows_fal_multiple_of_16_rounding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            content_id = uuid.uuid4()
            ok = media_root / "cache" / str(content_id) / "ok.jpg"
            _write_image(ok, (1072, 1920))
            content = SimpleNamespace(id=content_id, is_short_episode=True)
            db = _FakeDb(content, [_section(content_id, f"cache/{content_id}/ok.jpg")])

            with (
                patch("app.agents.agent4_visuals.services.media_validation.settings.media_path", str(media_root)),
                patch("app.services.local_run_paths.settings.media_path", str(media_root)),
            ):
                result = validate_visual_media_assets(content_id, db)

        self.assertTrue(result.passed)
        self.assertEqual(result.valid_local_media_count, 1)


if __name__ == "__main__":
    unittest.main()
