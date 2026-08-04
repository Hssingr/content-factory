"""Runtime proof for the Agent 6 roadmap's Phase C
(code_report/agent6_metadata_roadmap.md) — thumbnail base-image generation
only (stages 1-3: Claude prompt, Flux call, raw validation). No per-language
overlay, no VideoMetadata write — both are Phase D.

Only the paid boundaries are stubbed: Claude (``call_claude_structured``,
patched where ``system_prompt.py`` looks it up — not where it's defined,
the exact lesson Phase A's patch-target conflict already proved the hard
way) and the fal.ai call inside ``app.services.flux_client.generate_beat_image``.
Every other function (gating logic, the no-text retry/fallback loop, raw
validation, base-image placement) runs for real.

No live external API calls anywhere in this file (CLAUDE.md §19.1).
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.agents.agent6_metadata import system_prompt
from app.agents.agent6_metadata.services import thumbnail
from app.models import ChannelConfig, ChannelPlatform, Content


# ── Minimal fake DB — only what generate_thumbnail_base_image() reads ──────

class _FakeQuery:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *args, **kwargs) -> "_FakeQuery":
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Filtering is pre-baked at construction time (the test decides what a
    query would return), not parsed from SQLAlchemy filter expressions —
    this test is proving generate_thumbnail_base_image()'s own logic, not
    SQLAlchemy's, matching the fake-DB convention used throughout this
    repo's other agent tests."""

    def __init__(
        self,
        content_by_id: dict | None = None,
        configs_by_channel_id: dict | None = None,
        youtube_platform_rows: list | None = None,
    ) -> None:
        self._content = content_by_id or {}
        self._configs = configs_by_channel_id or {}
        self._youtube_platform_rows = youtube_platform_rows or []

    def get(self, model, pk):
        if model is Content:
            return self._content.get(pk)
        if model is ChannelConfig:
            return self._configs.get(pk)
        return None

    def query(self, model):
        if model is ChannelPlatform:
            return _FakeQuery(self._youtube_platform_rows)
        return _FakeQuery([])


def _content(
    is_short_episode: bool = False,
    parent_content_id=None,
    story_blueprint: dict | None = None,
) -> Content:
    return Content(
        id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        source_url="https://example.com/story",
        source_language="en",
        content_hash=str(uuid.uuid4()),
        title="Test content",
        status="RENDERED",
        is_short_episode=is_short_episode,
        parent_content_id=parent_content_id,
        story_blueprint=story_blueprint,
    )


def _write_real_jpeg(path: Path, *, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color=(80, 80, 80)).save(path, format="JPEG", quality=90)


# ── Gating tests (Check 4) — zero Flux calls whenever gated out ────────────

class GatingTest(unittest.TestCase):
    def test_child_of_parent_short_is_skipped(self) -> None:
        content = _content(is_short_episode=True, parent_content_id=uuid.uuid4())
        db = _FakeDb(content_by_id={content.id: content})

        with patch.object(
            thumbnail.flux_client, "generate_beat_image",
            side_effect=AssertionError("must not call Flux for a child-of-parent Short"),
        ):
            result = thumbnail.generate_thumbnail_base_image(content.id, db)

        self.assertIsNone(result)

    def test_solo_short_is_skipped(self) -> None:
        content = _content(is_short_episode=True, parent_content_id=None)
        db = _FakeDb(content_by_id={content.id: content})

        with patch.object(
            thumbnail.flux_client, "generate_beat_image",
            side_effect=AssertionError("must not call Flux for a Solo Short"),
        ):
            result = thumbnail.generate_thumbnail_base_image(content.id, db)

        self.assertIsNone(result)

    def test_parent_with_no_verified_youtube_platform_is_skipped(self) -> None:
        content = _content(is_short_episode=False)
        db = _FakeDb(content_by_id={content.id: content}, youtube_platform_rows=[])

        with patch.object(
            thumbnail.flux_client, "generate_beat_image",
            side_effect=AssertionError("must not call Flux with no verified youtube platform"),
        ):
            result = thumbnail.generate_thumbnail_base_image(content.id, db)

        self.assertIsNone(result)

    def test_content_not_found_is_skipped(self) -> None:
        db = _FakeDb()
        with patch.object(
            thumbnail.flux_client, "generate_beat_image",
            side_effect=AssertionError("must not call Flux for missing content"),
        ):
            result = thumbnail.generate_thumbnail_base_image(uuid.uuid4(), db)
        self.assertIsNone(result)


# ── Real generation, real PIL dimension check ──────────────────────────────

class RealGenerationTest(unittest.TestCase):
    def test_parent_with_verified_youtube_produces_validated_base_image(self) -> None:
        content = _content(
            is_short_episode=False,
            story_blueprint={
                "hook": "A locked room, a missing key.",
                "character_descriptors": [{"name": "Sam", "description": "detective"}],
                "era_setting": "1920s Chicago",
            },
        )
        config = ChannelConfig(channel_id=content.channel_id, visual_style="noir", image_style="photorealistic")
        db = _FakeDb(
            content_by_id={content.id: content},
            configs_by_channel_id={content.channel_id: config},
            youtube_platform_rows=[
                ChannelPlatform(channel_id=content.channel_id, language="en", platform="youtube", verified=True),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                patch.object(
                    system_prompt, "call_claude_structured",
                    return_value={"flux_prompt": "a locked door, brass key on the floor, dramatic side light"},
                ) as claude_stub,
                patch.object(thumbnail.flux_client, "generate_beat_image") as flux_stub,
            ):
                fixture_relative = "cache/fixture-content/deadbeef.jpg"
                fixture_path = Path(tmp) / fixture_relative
                _write_real_jpeg(fixture_path, width=1280, height=720)
                flux_stub.return_value = fixture_relative

                result = thumbnail.generate_thumbnail_base_image(content.id, db)

            self.assertEqual(claude_stub.call_count, 1)
            self.assertEqual(flux_stub.call_count, 1)
            self.assertIsNotNone(result)
            self.assertEqual(result, f"thumbnails/{content.id}/base.jpg")

            # Real PIL check against the file this function actually placed.
            final_path = Path(tmp) / result
            self.assertTrue(final_path.is_file())
            with Image.open(final_path) as img:
                self.assertEqual(img.size, (1280, 720))

            # The Flux call itself requested the right geometry/tier.
            _, kwargs = flux_stub.call_args
            self.assertEqual(kwargs["width"], 1280)
            self.assertEqual(kwargs["height"], 720)


# ── Stage 3 validation + one retry + non-fatal degrade ─────────────────────

class ValidationRetryDegradeTest(unittest.TestCase):
    def _db(self):
        content = _content(is_short_episode=False)
        db = _FakeDb(
            content_by_id={content.id: content},
            youtube_platform_rows=[
                ChannelPlatform(channel_id=content.channel_id, language="en", platform="youtube", verified=True),
            ],
        )
        return content, db

    def test_first_attempt_wrong_size_second_attempt_succeeds(self) -> None:
        content, db = self._db()
        with tempfile.TemporaryDirectory() as tmp:
            bad_relative = "cache/x/bad.jpg"
            good_relative = "cache/x/good.jpg"
            _write_real_jpeg(Path(tmp) / bad_relative, width=640, height=360)  # wrong size
            _write_real_jpeg(Path(tmp) / good_relative, width=1280, height=720)

            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                patch.object(
                    system_prompt, "call_claude_structured",
                    return_value={"flux_prompt": "a dim hallway, single overhead bulb"},
                ),
                patch.object(
                    thumbnail.flux_client, "generate_beat_image",
                    side_effect=[bad_relative, good_relative],
                ) as flux_stub,
            ):
                result = thumbnail.generate_thumbnail_base_image(content.id, db)

            self.assertEqual(flux_stub.call_count, 2)
            # Second call used a distinct cache key so it can't hash back to
            # the same (bad) cached artifact.
            self.assertEqual(flux_stub.call_args_list[1].kwargs.get("cache_key_extra"), "thumbnail_retry:1")
            self.assertIsNotNone(result)
            self.assertEqual(result, f"thumbnails/{content.id}/base.jpg")

    def test_both_attempts_fail_returns_none_without_raising(self) -> None:
        content, db = self._db()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                patch.object(
                    system_prompt, "call_claude_structured",
                    return_value={"flux_prompt": "an empty stairwell"},
                ),
                patch.object(
                    thumbnail.flux_client, "generate_beat_image",
                    side_effect=[None, None],
                ) as flux_stub,
            ):
                result = thumbnail.generate_thumbnail_base_image(content.id, db)

            self.assertEqual(flux_stub.call_count, 2)
            self.assertIsNone(result)

    def test_both_attempts_wrong_dimensions_returns_none(self) -> None:
        content, db = self._db()
        with tempfile.TemporaryDirectory() as tmp:
            bad1 = "cache/x/bad1.jpg"
            bad2 = "cache/x/bad2.jpg"
            _write_real_jpeg(Path(tmp) / bad1, width=1920, height=1080)
            _write_real_jpeg(Path(tmp) / bad2, width=1080, height=1920)

            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                patch.object(
                    system_prompt, "call_claude_structured",
                    return_value={"flux_prompt": "a rain-slicked street"},
                ),
                patch.object(
                    thumbnail.flux_client, "generate_beat_image",
                    side_effect=[bad1, bad2],
                ) as flux_stub,
            ):
                result = thumbnail.generate_thumbnail_base_image(content.id, db)

            self.assertEqual(flux_stub.call_count, 2)
            self.assertIsNone(result)


# ── _validate_raw_thumbnail unit coverage ───────────────────────────────────

class ValidateRawThumbnailTest(unittest.TestCase):
    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(thumbnail.settings, "media_path", tmp):
                ok, reason = thumbnail._validate_raw_thumbnail("does/not/exist.jpg")
            self.assertFalse(ok)
            self.assertEqual(reason, "file_missing")

    def test_too_small_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not a real image")
            with patch.object(thumbnail.settings, "media_path", tmp):
                ok, reason = thumbnail._validate_raw_thumbnail("tiny.jpg")
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("file_too_small"))

    def test_wrong_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_real_jpeg(Path(tmp) / "wrong.jpg", width=800, height=600)
            with patch.object(thumbnail.settings, "media_path", tmp):
                ok, reason = thumbnail._validate_raw_thumbnail("wrong.jpg")
            self.assertFalse(ok)
            self.assertEqual(reason, "wrong_dimensions_800x600")

    def test_valid_image_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_real_jpeg(Path(tmp) / "ok.jpg", width=1280, height=720)
            with patch.object(thumbnail.settings, "media_path", tmp):
                ok, reason = thumbnail._validate_raw_thumbnail("ok.jpg")
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")


# ── Stage 1: no-text retry/fallback loop (system_prompt.py) ────────────────

class ThumbnailPromptRetryFallbackTest(unittest.TestCase):
    def test_clean_first_attempt_used_verbatim_with_clause_appended(self) -> None:
        with patch.object(
            system_prompt, "call_claude_structured",
            return_value={"flux_prompt": "a foggy pier at dawn"},
        ) as stub:
            prompt = system_prompt.generate_thumbnail_prompt({"hook": "x"})

        self.assertEqual(stub.call_count, 1)
        self.assertTrue(prompt.startswith("a foggy pier at dawn,"))
        self.assertIn(system_prompt._NO_TEXT_CLAUSE, prompt)

    def test_first_attempt_violates_second_is_clean(self) -> None:
        with patch.object(
            system_prompt, "call_claude_structured",
            side_effect=[
                {"flux_prompt": 'a sign that says "DANGER"'},
                {"flux_prompt": "a cracked warning light on a wall"},
            ],
        ) as stub:
            prompt = system_prompt.generate_thumbnail_prompt({"hook": "x"})

        self.assertEqual(stub.call_count, 2)
        self.assertTrue(prompt.startswith("a cracked warning light on a wall,"))
        self.assertIn(system_prompt._NO_TEXT_CLAUSE, prompt)

    def test_both_attempts_violate_falls_back_to_safe_template(self) -> None:
        with patch.object(
            system_prompt, "call_claude_structured",
            side_effect=[
                {"flux_prompt": 'a label reading "EVIDENCE"'},
                {"flux_prompt": 'a banner that says "CLOSED"'},
            ],
        ) as stub:
            prompt = system_prompt.generate_thumbnail_prompt({"hook": "x"})

        self.assertEqual(stub.call_count, 2)
        self.assertTrue(prompt.startswith(system_prompt._THUMBNAIL_SAFE_FALLBACK_PROMPT))
        self.assertIn(system_prompt._NO_TEXT_CLAUSE, prompt)

    def test_claude_exception_both_times_falls_back_without_raising(self) -> None:
        with patch.object(
            system_prompt, "call_claude_structured",
            side_effect=RuntimeError("stubbed transport failure"),
        ) as stub:
            prompt = system_prompt.generate_thumbnail_prompt({"hook": "x"})

        self.assertEqual(stub.call_count, 2)
        self.assertTrue(prompt.startswith(system_prompt._THUMBNAIL_SAFE_FALLBACK_PROMPT))

    def test_empty_blueprint_does_not_raise(self) -> None:
        with patch.object(
            system_prompt, "call_claude_structured",
            return_value={"flux_prompt": "a generic dramatic scene"},
        ):
            prompt = system_prompt.generate_thumbnail_prompt(None)
        self.assertIn(system_prompt._NO_TEXT_CLAUSE, prompt)


if __name__ == "__main__":
    unittest.main()
