import json
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app.agents.agent4_visuals.services.media_validation import validate_visual_media_assets
from app.models import Content, VideoSection


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        self._rows.sort(key=lambda row: (getattr(row, "language", ""), getattr(row, "section_order", 0)))
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, content, sections):
        self.content = content
        self.sections = sections

    def get(self, model, content_id):
        if model is Content and str(content_id) == str(self.content.id):
            return self.content
        return None

    def query(self, model):
        if model is VideoSection:
            return _FakeQuery(self.sections)
        return _FakeQuery([])


def _content(content_id):
    return SimpleNamespace(id=content_id, title="media validation", status="GENERATING_VISUALS")


def _section(content_id, media_url, *, order=1, visual_type="b-roll", media_strategy="flux_generated", media_type="image"):
    extras = {
        "media_url": media_url,
        "media_type": media_type,
        "visual_type": visual_type,
        "visual_category": "place",
    }
    return SimpleNamespace(
        content_id=content_id,
        language="en",
        section_order=order,
        generation_prompt=json.dumps(extras),
        media_strategy=media_strategy,
        text_card_style="default",
    )


def _write_image(path: Path, *, size: tuple[int, int] = (160, 90)):
    """Write a real local image at the given pixel size.

    Defaults to 160x90 (16:9) — matching the parent aspect ratio the media
    validator's dimension check (roadmap 3a / audit P1-3) expects for
    non-Short content. Pass size=(90, 160) (9:16) for Short-content fixtures.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color="blue").save(path)


class TestAgent4MediaValidation(unittest.TestCase):
    def _run(self, tmp, sections, content_id=None, beats_by_lang=None):
        content_id = content_id or uuid.uuid4()
        db = _FakeDb(_content(content_id), sections)
        with patch("app.agents.agent4_visuals.services.media_validation.settings.media_path", tmp), \
             patch("app.services.local_run_paths.settings.media_path", tmp):
            return validate_visual_media_assets(content_id, db, beats_by_lang=beats_by_lang)

    def test_valid_local_image_passes(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            _write_image(Path(tmp) / "cache" / str(content_id) / "ok.jpg")
            result = self._run(tmp, [_section(content_id, f"cache/{content_id}/ok.jpg")], content_id)

        self.assertTrue(result.passed)
        self.assertEqual(result.valid_local_media_count, 1)
        self.assertEqual(result.blocking_issues, [])

    def test_missing_media_path_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            result = self._run(tmp, [_section(content_id, "")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "media_path_missing")

    def test_missing_file_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            result = self._run(tmp, [_section(content_id, f"cache/{content_id}/missing.jpg")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "local_media_missing")

    def test_zero_byte_file_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            media = Path(tmp) / "cache" / str(content_id) / "empty.jpg"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"")
            result = self._run(tmp, [_section(content_id, f"cache/{content_id}/empty.jpg")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "local_media_zero_byte")

    def test_unsupported_extension_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            media = Path(tmp) / "cache" / str(content_id) / "clip.gif"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"not empty")
            result = self._run(tmp, [_section(content_id, f"cache/{content_id}/clip.gif")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "unsupported_media_extension")

    def test_unsupported_media_type_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            _write_image(Path(tmp) / "cache" / str(content_id) / "ok.jpg")
            result = self._run(
                tmp,
                [_section(content_id, f"cache/{content_id}/ok.jpg", media_type="video")],
                content_id,
            )

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "unsupported_media_type")

    def test_unreadable_image_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            media = Path(tmp) / "cache" / str(content_id) / "bad.jpg"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"not really an image")
            result = self._run(tmp, [_section(content_id, f"cache/{content_id}/bad.jpg")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "local_image_unreadable")

    def test_remote_url_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            result = self._run(tmp, [_section(content_id, "https://example.invalid/image.jpg")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.remote_media_count, 1)
        self.assertEqual(result.blocking_issues[0].code, "remote_media_url")

    def test_legacy_text_card_without_image_blocks(self):
        # Subtitles-only rendering (audit G-0/G-8): the text-card exemption is
        # retired — a legacy text-card row with no local image blocks
        # visual-ready status like any other imageless section.
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            result = self._run(
                tmp,
                [_section(content_id, "", visual_type="text_card", media_strategy="remotion_text_card")],
                content_id,
            )

        self.assertFalse(result.passed)
        self.assertEqual(result.text_card_count, 1)
        self.assertEqual(result.blocking_issues[0].code, "media_path_missing")

    def test_legacy_text_card_sentinel_blocks(self):
        # The legacy '__text_card__' sentinel is no longer produced and no
        # longer tolerated — a row still carrying it must be regenerated.
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            result = self._run(tmp, [_section(content_id, "__text_card__")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "legacy_text_card_sentinel")

    def test_unsafe_path_blocks(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            result = self._run(tmp, [_section(content_id, "../../secret.jpg")], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "unsafe_media_path")

    def test_empty_sections_block(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            result = self._run(tmp, [], content_id)

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].code, "video_sections_missing")

    # ── Persistence round-trip (roadmap 6.4 / audit V-7, AR-2) — folded in
    # from the deleted visual_orchestrator._check_media_assets(). ──────────

    def test_round_trip_matching_beat_produces_no_warnings(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            _write_image(Path(tmp) / "cache" / str(content_id) / "ok.jpg")
            media_url = f"cache/{content_id}/ok.jpg"
            beats_by_lang = {"en": [{"section_order": 1, "media_url": media_url, "media_type": "image"}]}
            result = self._run(
                tmp, [_section(content_id, media_url)], content_id, beats_by_lang=beats_by_lang,
            )

        self.assertTrue(result.passed)
        self.assertEqual(result.warnings, [])

    def test_round_trip_media_url_mismatch_is_warning_not_blocking(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            _write_image(Path(tmp) / "cache" / str(content_id) / "ok.jpg")
            saved_url = f"cache/{content_id}/ok.jpg"
            # In-memory beat claims a DIFFERENT media_url than what got persisted.
            beats_by_lang = {
                "en": [{"section_order": 1, "media_url": f"cache/{content_id}/other.jpg", "media_type": "image"}]
            }
            result = self._run(
                tmp, [_section(content_id, saved_url)], content_id, beats_by_lang=beats_by_lang,
            )

        # Round-trip findings are diagnostic only — never affect `passed`.
        self.assertTrue(result.passed)
        codes = {w.code for w in result.warnings}
        self.assertIn("persistence_media_url_mismatch", codes)

    def test_round_trip_missing_reloaded_row_is_warning(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            _write_image(Path(tmp) / "cache" / str(content_id) / "ok.jpg")
            media_url = f"cache/{content_id}/ok.jpg"
            # Beat claims section_order=2, but only section_order=1 was persisted.
            beats_by_lang = {"en": [{"section_order": 2, "media_url": media_url, "media_type": "image"}]}
            result = self._run(
                tmp, [_section(content_id, media_url, order=1)], content_id, beats_by_lang=beats_by_lang,
            )

        self.assertTrue(result.passed)
        codes = {w.code for w in result.warnings}
        self.assertIn("persistence_row_missing", codes)

    def test_no_beats_by_lang_skips_round_trip_check(self):
        """Backward compatibility: omitting beats_by_lang (every pre-existing
        caller) must produce zero round-trip warnings — identical to the
        function's behavior before this parameter existed."""
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            _write_image(Path(tmp) / "cache" / str(content_id) / "ok.jpg")
            result = self._run(tmp, [_section(content_id, f"cache/{content_id}/ok.jpg")], content_id)

        self.assertTrue(result.passed)
        self.assertEqual(result.warnings, [])


if __name__ == "__main__":
    unittest.main()
