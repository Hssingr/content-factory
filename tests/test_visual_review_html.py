import json
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.models import AudioFile, Content, Script, VideoSection
from app.agents.agent4_visuals.services.visual_review import generate_visual_review_html


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
    def __init__(self, content, sections, scripts=None, audio=None):
        self.content = content
        self.sections = sections
        self.scripts = scripts or []
        self.audio = audio or []

    def get(self, model, content_id):
        if model is Content and str(content_id) == str(self.content.id):
            return self.content
        return None

    def query(self, model):
        if model is VideoSection:
            return _FakeQuery(self.sections)
        if model is Script:
            return _FakeQuery(self.scripts)
        if model is AudioFile:
            return _FakeQuery(self.audio)
        return _FakeQuery([])


def _content(content_id):
    return SimpleNamespace(
        id=content_id,
        title="Escaped <Title>",
        status="PARENT_VISUALS_DONE",
        is_short_episode=False,
    )


def _section(content_id, order=1, media_url="", text="Narration <b>bold</b>", extras=None):
    payload = {
        "visual_intent": "Intent & mood",
        "visual_type": "b-roll",
        "visual_category": "place",
        "environment": "indoor_office",
        "motif": "documents",
        "media_url": media_url,
        "media_type": "image",
    }
    if extras:
        payload.update(extras)
    return SimpleNamespace(
        content_id=content_id,
        language="en",
        section_order=order,
        script_text=text,
        audio_start_ms=1000,
        audio_end_ms=4500,
        flux_prompt="Prompt <script>alert(1)</script>",
        effect="slow_zoom",
        color_grade="desaturated",
        generation_prompt=json.dumps(payload),
        beat_intensity="high",
        suggested_duration_sec=3.5,
        media_strategy="flux_generated",
        text_card_style="default",
    )


class TestVisualReviewHtml(unittest.TestCase):
    def test_generates_html_with_escaped_text_and_missing_warning(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            db = _FakeDb(_content(content_id), [_section(content_id, media_url="cache/missing.jpg")])
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_review.settings.media_path", tmp):
                path = generate_visual_review_html(content_id, db)
                html = path.read_text(encoding="utf-8")

        self.assertTrue(path.name, "visual_review.html")
        self.assertIn("&lt;Title&gt;", html)
        self.assertIn("Narration &lt;b&gt;bold&lt;/b&gt;", html)
        self.assertIn("Prompt &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("Prompt <script>", html)
        self.assertIn("Local media file missing", html)

    def test_local_image_gets_relative_preview(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            media = Path(tmp) / "cache" / str(content_id) / "image.jpg"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"fake image bytes")
            db = _FakeDb(_content(content_id), [_section(content_id, media_url=f"cache/{content_id}/image.jpg")])
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_review.settings.media_path", tmp):
                path = generate_visual_review_html(content_id, db)
                html = path.read_text(encoding="utf-8")

        self.assertIn('<img class="preview"', html)
        self.assertIn("../../../cache/", html)
        self.assertNotIn(str(media), html.split('<img class="preview"', 1)[1].split(">", 1)[0])

    def test_remote_url_is_not_previewed_or_fetched(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            remote = "https://example.invalid/image.jpg"
            db = _FakeDb(_content(content_id), [_section(content_id, media_url=remote)])
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_review.settings.media_path", tmp):
                html = generate_visual_review_html(content_id, db).read_text(encoding="utf-8")

        self.assertIn(remote, html)
        self.assertIn("Remote URL stored; not fetched", html)
        self.assertNotIn(f'src="{remote}"', html)

    def test_empty_sections_and_idempotent_regeneration(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            db = _FakeDb(_content(content_id), [])
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_review.settings.media_path", tmp):
                first = generate_visual_review_html(content_id, db)
                first_text = first.read_text(encoding="utf-8")
                second = generate_visual_review_html(content_id, db)
                second_text = second.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertIn("No VideoSection rows found", second_text)
        self.assertIn("Total visual sections", first_text)


if __name__ == "__main__":
    unittest.main()
