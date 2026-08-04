"""Runtime proof for the Agent 6 roadmap's Phase D
(code_report/agent6_metadata_roadmap.md) — platform metadata generation,
platform limits, the per-language thumbnail overlay, and the orchestrator
that sequences them.

Only the paid boundaries are stubbed: Claude (``call_claude_structured``,
patched where each module looks it up) and the fal.ai call inside
``app.services.flux_client.generate_beat_image`` (patched at
``thumbnail.flux_client``, the exact lesson Phase A's patch-target conflict
already proved). Pillow compositing, platform-limit enforcement, timestamp
computation, and all orchestration logic run for real.

No live external API calls anywhere in this file (CLAUDE.md §19.1).
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy.sql.elements import False_, True_

from app.agents.agent6_metadata import system_prompt
from app.agents.agent6_metadata.services import (
    metadata_orchestrator,
    platform_limits,
    thumbnail,
)
from app.models import (
    Channel, ChannelConfig, ChannelPlatform, Content, Script, VideoMetadata,
    VideoRender, VideoSection,
)


# ── Fake DB — real (not no-op) filtering, needed because this orchestrator ──
# runs several distinct queries against the same model types within one
# call (multiple (language, platform) VideoMetadata upserts, per-language
# VideoSection loads) — a no-op filter would let row N's lookup incorrectly
# match row 1's, which the established repo-wide "no-op filter" fake-DB
# convention (see tests/test_solo_short_pipeline.py) never needed to handle
# for a single-row-per-model-per-test shape. Each test file in this repo
# already defines its own fake-DB variant tailored to what it needs.

def _extract_value(node):
    if isinstance(node, True_):
        return True
    if isinstance(node, False_):
        return False
    return node.value


class _FakeQuery:
    def __init__(self, rows: list) -> None:
        self.rows = list(rows)

    def filter(self, *conditions) -> "_FakeQuery":
        rows = self.rows
        for cond in conditions:
            key = cond.left.key
            value = _extract_value(cond.right)
            rows = [r for r in rows if getattr(r, key, None) == value]
        return _FakeQuery(rows)

    def order_by(self, column) -> "_FakeQuery":
        key = column.key
        return _FakeQuery(sorted(self.rows, key=lambda r: getattr(r, key)))

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self) -> list:
        return list(self.rows)


class _FakeDb:
    def __init__(self) -> None:
        self.tables: dict = {}

    def get(self, model, key):
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key or getattr(row, "channel_id", None) == key:
                return row
        return None

    def query(self, model) -> _FakeQuery:
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row) -> None:
        self.tables.setdefault(type(row), []).append(row)

    def commit(self) -> None:
        pass


def _write_real_jpeg(path: Path, *, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color=(60, 60, 60)).save(path, format="JPEG", quality=90)


# ── platform_limits.py unit coverage ────────────────────────────────────────

class PlatformLimitsTest(unittest.TestCase):
    def test_title_truncated_at_word_boundary(self) -> None:
        title = "word " * 40  # way over youtube's 100-char cap
        final_title, _, _ = platform_limits.enforce_platform_limits(
            "youtube", title, "short description", [],
        )
        self.assertLessEqual(len(final_title), 100)
        self.assertFalse(final_title.endswith("wor"))  # never a mid-word cut

    def test_hashtags_capped_and_ordering_preserved(self) -> None:
        hashtags = [f"#tag{i}" for i in range(10)]
        _, _, final_hashtags = platform_limits.enforce_platform_limits(
            "tiktok", "t", "d", hashtags,
        )
        self.assertEqual(final_hashtags, hashtags[:5])

    def test_unlimited_hashtag_platform_keeps_all(self) -> None:
        hashtags = [f"#tag{i}" for i in range(50)]
        _, _, final_hashtags = platform_limits.enforce_platform_limits(
            "youtube", "t", "d", hashtags,
        )
        self.assertEqual(len(final_hashtags), 50)

    def test_unknown_platform_raises(self) -> None:
        with self.assertRaises(ValueError):
            platform_limits.enforce_platform_limits("myspace", "t", "d", [])

    def test_truncation_is_logged(self) -> None:
        with self.assertLogs(
            "app.agents.agent6_metadata.services.platform_limits", level="WARNING",
        ) as logs:
            platform_limits.enforce_platform_limits("tiktok", "x" * 500, "d", [])
        self.assertTrue(any("METADATA_LIMIT_ENFORCED" in line for line in logs.output))

    def test_thumbnail_text_capped_by_words_and_chars(self) -> None:
        result = platform_limits.enforce_thumbnail_text_limit(
            "one two three four five six seven",
        )
        self.assertLessEqual(len(result.split()), 5)
        self.assertLessEqual(len(result), 30)

    def test_thumbnail_text_within_limit_unchanged(self) -> None:
        self.assertEqual(platform_limits.enforce_thumbnail_text_limit("short phrase"), "short phrase")


# ── build_youtube_timestamps() pure-function unit coverage ─────────────────

class BuildYoutubeTimestampsTest(unittest.TestCase):
    def test_matches_real_audio_start_ms_exactly(self) -> None:
        beats = [
            {"audio_start_ms": 0, "script_text": "Intro"},
            {"audio_start_ms": 65_000, "script_text": "The reveal"},
            {"audio_start_ms": 3_725_000, "script_text": "Outro"},
        ]
        result = metadata_orchestrator.build_youtube_timestamps(beats)
        self.assertIn("0:00 Intro", result)
        self.assertIn("1:05 The reveal", result)
        self.assertIn("1:02:05 Outro", result)

    def test_empty_beats_returns_empty_string(self) -> None:
        self.assertEqual(metadata_orchestrator.build_youtube_timestamps([]), "")

    def test_long_label_truncated(self) -> None:
        beats = [{"audio_start_ms": 0, "script_text": "word " * 30}]
        result = metadata_orchestrator.build_youtube_timestamps(beats)
        self.assertLess(len(result), 70)


# ── generate_platform_metadata() — Claude boundary stubbed ─────────────────

class PlatformMetadataGenerationTest(unittest.TestCase):
    def test_per_platform_style_guidance_differs(self) -> None:
        seen_messages = {}

        def _fake_call(**kwargs):
            seen_messages[kwargs["user_message"]] = True
            return {"title": "t", "description": "d", "hashtags": [], "thumbnail_text": ""}

        with patch.object(system_prompt, "call_claude_structured", side_effect=_fake_call) as stub:
            for platform in ("youtube", "tiktok", "instagram", "facebook"):
                system_prompt.generate_platform_metadata(
                    platform, "en", "narration text", {"hook": "h"}, niche="n", tone="t",
                )

        self.assertEqual(stub.call_count, 4)
        user_messages = [c.kwargs["user_message"] for c in stub.call_args_list]
        # Every call's prompt is genuinely distinct (platform style guidance
        # differs) — not one generic call reused four times.
        self.assertEqual(len(set(user_messages)), 4)
        for platform, message in zip(("youtube", "tiktok", "instagram", "facebook"), user_messages):
            self.assertIn(
                system_prompt._PLATFORM_STYLE_GUIDANCE[platform].split(":")[0], message,
            )

    def test_thumbnail_text_only_requested_for_youtube(self) -> None:
        captured = {}

        def _fake_call(**kwargs):
            captured["message"] = kwargs["user_message"]
            return {"title": "t", "description": "d", "hashtags": [], "thumbnail_text": ""}

        with patch.object(system_prompt, "call_claude_structured", side_effect=_fake_call):
            system_prompt.generate_platform_metadata("tiktok", "en", "x", {})
        self.assertIn("Request thumbnail_text: no", captured["message"])

        with patch.object(system_prompt, "call_claude_structured", side_effect=_fake_call):
            system_prompt.generate_platform_metadata("youtube", "en", "x", {})
        self.assertIn("Request thumbnail_text: yes", captured["message"])

    def test_transport_failure_returns_none_without_raising(self) -> None:
        with patch.object(
            system_prompt, "call_claude_structured", side_effect=RuntimeError("boom"),
        ):
            result = system_prompt.generate_platform_metadata("youtube", "en", "x", {})
        self.assertIsNone(result)

    def test_full_narration_passed_untruncated(self) -> None:
        long_script = "word " * 3000
        captured = {}

        def _fake_call(**kwargs):
            captured["message"] = kwargs["user_message"]
            return {"title": "t", "description": "d", "hashtags": [], "thumbnail_text": ""}

        with patch.object(system_prompt, "call_claude_structured", side_effect=_fake_call):
            system_prompt.generate_platform_metadata("youtube", "en", long_script, {})
        self.assertIn(long_script.strip(), captured["message"])


# ── composite_thumbnail_overlay() — Pillow only, no paid calls ─────────────

class CompositeOverlayTest(unittest.TestCase):
    def test_successful_overlay_produces_validated_file(self) -> None:
        content_id = uuid.uuid4()
        with tempfile.TemporaryDirectory() as tmp:
            base_relative = f"thumbnails/{content_id}/base.jpg"
            _write_real_jpeg(Path(tmp) / base_relative, width=1280, height=720)

            with patch.object(thumbnail.settings, "media_path", tmp):
                result = thumbnail.composite_thumbnail_overlay(
                    base_relative, "Total Chaos", "en", content_id,
                )

            self.assertEqual(result, f"thumbnails/{content_id}/en.jpg")
            final = Path(tmp) / result
            self.assertTrue(final.is_file())
            with Image.open(final) as img:
                self.assertEqual(img.size, (1280, 720))

    def test_missing_base_image_is_non_fatal(self) -> None:
        content_id = uuid.uuid4()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(thumbnail.settings, "media_path", tmp):
                result = thumbnail.composite_thumbnail_overlay(
                    f"thumbnails/{content_id}/base.jpg", "Text", "en", content_id,
                )
        self.assertIsNone(result)

    def test_uncovered_glyph_skips_overlay_non_fatally(self) -> None:
        content_id = uuid.uuid4()
        with tempfile.TemporaryDirectory() as tmp:
            base_relative = f"thumbnails/{content_id}/base.jpg"
            _write_real_jpeg(Path(tmp) / base_relative, width=1280, height=720)

            # A CJK character Archivo Black (Latin-only) cannot render.
            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                self.assertLogs(
                    "app.agents.agent6_metadata.services.thumbnail", level="WARNING",
                ) as logs,
            ):
                result = thumbnail.composite_thumbnail_overlay(
                    base_relative, "秘密", "ja", content_id,
                )

            self.assertIsNone(result)
            self.assertTrue(any("THUMBNAIL_FONT_GLYPH_MISSING" in line for line in logs.output))
            # No output file was left behind for the skipped language.
            self.assertFalse((Path(tmp) / f"thumbnails/{content_id}/ja.jpg").exists())

    def test_font_covers_ordinary_accented_latin_text(self) -> None:
        from PIL import ImageFont

        font = ImageFont.truetype(str(thumbnail._FONT_PATH), 48)
        covers, bad_char = thumbnail._font_covers_text(font, "Café à l'école")
        self.assertTrue(covers)
        self.assertIsNone(bad_char)


# ── run_metadata_generation_for_content() — the real orchestrator ──────────

def _channel_setup(db: _FakeDb, *, verified_platforms: list[str]) -> uuid.UUID:
    channel_id = uuid.uuid4()
    db.add(Channel(id=channel_id, niche="true crime", tone="suspenseful"))
    db.add(ChannelConfig(channel_id=channel_id, visual_style="noir", image_style="photorealistic"))
    for platform in verified_platforms:
        db.add(ChannelPlatform(
            channel_id=channel_id, language="en", platform=platform, verified=True,
        ))
    return channel_id


def _stub_metadata(platform: str, language: str, thumbnail_text: str = "") -> dict:
    return {
        "title": f"{platform}-{language}-title",
        "description": f"{platform}-{language}-description",
        "hashtags": ["#a", "#b"],
        "thumbnail_text": thumbnail_text,
    }


class OrchestratorRowCountAndShapeTest(unittest.TestCase):
    def test_one_row_per_language_times_verified_platform(self) -> None:
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube", "tiktok"])
        content_id = uuid.uuid4()
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "h"},
        )
        db.add(content)
        for language in ("en", "fr"):
            db.add(Script(content_id=content_id, language=language, voice_script="narration", validated=True))
            db.add(VideoRender(content_id=content_id, language=language, format="main", duration_seconds=10.0))

        with (
            patch.object(
                system_prompt, "call_claude_structured",
                side_effect=lambda **kw: {
                    "title": "t", "description": "d", "hashtags": [], "thumbnail_text": "",
                },
            ),
            patch.object(metadata_orchestrator, "_ensure_base_image", return_value=None),
        ):
            success = metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

        self.assertTrue(success)
        rows = db.tables.get(VideoMetadata, [])
        self.assertEqual(len(rows), 4)  # 2 languages x 2 platforms
        combos = {(r.language, r.platform) for r in rows}
        self.assertEqual(combos, {("en", "youtube"), ("en", "tiktok"), ("fr", "youtube"), ("fr", "tiktok")})

    def test_child_of_parent_short_still_produces_text_metadata_no_thumbnail(self) -> None:
        """Check 4, Finding 4.4: only thumbnail generation is shape-gated —
        text metadata is generated regardless of render format."""
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube"])
        parent_id = uuid.uuid4()
        db.add(Content(
            id=parent_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "parent hook text"},
        ))
        child_id = uuid.uuid4()
        db.add(Content(
            id=child_id, channel_id=channel_id, is_short_episode=True,
            parent_content_id=parent_id, status="RENDERED", story_blueprint=None,
        ))
        db.add(Script(content_id=child_id, language="en", voice_script="short narration", validated=True))
        db.add(VideoRender(content_id=child_id, language="en", format="short", duration_seconds=1.0))

        captured_blueprints = []

        def _fake_call(**kwargs):
            captured_blueprints.append(kwargs["user_message"])
            return {"title": "t", "description": "d", "hashtags": [], "thumbnail_text": "x"}

        with (
            patch.object(system_prompt, "call_claude_structured", side_effect=_fake_call),
            patch.object(
                thumbnail.flux_client, "generate_beat_image",
                side_effect=AssertionError("must not call Flux for a child-of-parent Short"),
            ),
        ):
            success = metadata_orchestrator.run_metadata_generation_for_content(child_id, db)

        self.assertTrue(success)
        rows = db.tables.get(VideoMetadata, [])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].thumbnail_file_path)
        # Blueprint parent-fallback proof: the child's Claude call was
        # grounded in the PARENT's hook, not an empty blueprint.
        self.assertTrue(any("parent hook text" in msg for msg in captured_blueprints))

    def test_solo_short_never_attempts_parent_lookup(self) -> None:
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube"])
        content_id = uuid.uuid4()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=True,
            parent_content_id=None, status="RENDERED", story_blueprint=None,
        ))
        db.add(Script(content_id=content_id, language="en", voice_script="solo narration", validated=True))
        db.add(VideoRender(content_id=content_id, language="en", format="short", duration_seconds=1.0))

        with (
            patch.object(
                system_prompt, "call_claude_structured",
                return_value={"title": "t", "description": "d", "hashtags": [], "thumbnail_text": ""},
            ),
            patch.object(
                thumbnail.flux_client, "generate_beat_image",
                side_effect=AssertionError("must not call Flux for a Solo Short"),
            ),
        ):
            # Must not raise despite content.story_blueprint being None and
            # parent_content_id being None — no parent lookup is attempted.
            success = metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

        self.assertTrue(success)
        rows = db.tables.get(VideoMetadata, [])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].thumbnail_file_path)


class OrchestratorTimestampsAndLimitsTest(unittest.TestCase):
    def test_youtube_description_includes_real_timestamps(self) -> None:
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube"])
        content_id = uuid.uuid4()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "h"},
        ))
        db.add(Script(content_id=content_id, language="en", voice_script="narration", validated=True))
        db.add(VideoRender(content_id=content_id, language="en", format="main", duration_seconds=200.0))
        db.add(VideoSection(
            content_id=content_id, language="en", section_order=0,
            script_text="Opening scene", audio_start_ms=0, audio_end_ms=5000,
        ))
        db.add(VideoSection(
            content_id=content_id, language="en", section_order=1,
            script_text="The big reveal", audio_start_ms=90_000, audio_end_ms=95_000,
        ))

        with (
            patch.object(
                system_prompt, "call_claude_structured",
                return_value={
                    "title": "t", "description": "Base description.",
                    "hashtags": [], "thumbnail_text": "",
                },
            ),
            patch.object(metadata_orchestrator, "_ensure_base_image", return_value=None),
        ):
            metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

        row = db.tables[VideoMetadata][0]
        self.assertIn("0:00 Opening scene", row.description)
        self.assertIn("1:30 The big reveal", row.description)
        self.assertTrue(row.description.startswith("Base description."))

    def test_oversized_stub_response_is_enforced_and_logged(self) -> None:
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["tiktok"])
        content_id = uuid.uuid4()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "h"},
        ))
        db.add(Script(content_id=content_id, language="en", voice_script="narration", validated=True))
        db.add(VideoRender(content_id=content_id, language="en", format="main", duration_seconds=10.0))

        oversized = {
            "title": "x" * 500,
            "description": "y" * 5000,
            "hashtags": [f"#h{i}" for i in range(20)],
            "thumbnail_text": "",
        }
        with (
            patch.object(system_prompt, "call_claude_structured", return_value=oversized),
            patch.object(metadata_orchestrator, "_ensure_base_image", return_value=None),
            self.assertLogs(
                "app.agents.agent6_metadata.services.platform_limits", level="WARNING",
            ) as logs,
        ):
            metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

        row = db.tables[VideoMetadata][0]
        self.assertLessEqual(len(row.title), 150)  # tiktok title_max
        self.assertLessEqual(len(row.description), 2200)
        self.assertLessEqual(len(row.hashtags), 5)
        self.assertTrue(any("METADATA_LIMIT_ENFORCED" in line for line in logs.output))

    def test_oversized_thumbnail_text_enforced_and_logged(self) -> None:
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube"])
        content_id = uuid.uuid4()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "h"},
        ))
        db.add(Script(content_id=content_id, language="en", voice_script="narration", validated=True))
        db.add(VideoRender(content_id=content_id, language="en", format="main", duration_seconds=10.0))

        with tempfile.TemporaryDirectory() as tmp:
            base_relative = f"thumbnails/{content_id}/base.jpg"
            _write_real_jpeg(Path(tmp) / base_relative, width=1280, height=720)

            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                patch.object(metadata_orchestrator.settings, "media_path", tmp),
                patch.object(
                    system_prompt, "call_claude_structured",
                    return_value={
                        "title": "t", "description": "d", "hashtags": [],
                        "thumbnail_text": "way way too many words for one thumbnail overlay",
                    },
                ),
                self.assertLogs(
                    "app.agents.agent6_metadata.services.platform_limits", level="WARNING",
                ) as logs,
            ):
                metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

            row = db.tables[VideoMetadata][0]
            self.assertIsNotNone(row.thumbnail_text)
            self.assertLessEqual(len(row.thumbnail_text.split()), 5)
            self.assertTrue(
                any("field=thumbnail_text" in line for line in logs.output),
            )


    def test_unknown_platform_degrades_only_that_pair(self) -> None:
        """Phase E hardening: a stale/legacy ChannelPlatform row for a
        platform not in platform_limits.PLATFORM_LIMITS (e.g. a
        decommissioned platform) must not abort the whole content item —
        only that (language, platform) pair is skipped, every other pair
        still succeeds, and the content item still reports success."""
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube", "myspace"])
        content_id = uuid.uuid4()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "h"},
        ))
        db.add(Script(content_id=content_id, language="en", voice_script="narration", validated=True))
        db.add(VideoRender(content_id=content_id, language="en", format="main", duration_seconds=10.0))

        with (
            patch.object(
                system_prompt, "call_claude_structured",
                return_value={"title": "t", "description": "d", "hashtags": [], "thumbnail_text": ""},
            ),
            patch.object(metadata_orchestrator, "_ensure_base_image", return_value=None),
            self.assertLogs("app.agents.agent6_metadata.services.metadata_orchestrator", level="ERROR") as logs,
        ):
            success = metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

        self.assertTrue(success)  # the youtube pair still succeeded
        rows = db.tables.get(VideoMetadata, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].platform, "youtube")
        self.assertTrue(
            any("METADATA_PLATFORM_LIMITS_UNKNOWN_PLATFORM" in line for line in logs.output),
        )


class OrchestratorSequencingAndReuseTest(unittest.TestCase):
    def test_metadata_generation_recorded_before_overlay_and_failed_metadata_skips_overlay(self) -> None:
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube"])
        content_id = uuid.uuid4()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "h"},
        ))
        db.add(Script(content_id=content_id, language="en", voice_script="ok narration", validated=True))
        db.add(Script(content_id=content_id, language="fr", voice_script="ok narration fr", validated=True))
        db.add(VideoRender(content_id=content_id, language="en", format="main", duration_seconds=10.0))
        db.add(VideoRender(content_id=content_id, language="fr", format="main", duration_seconds=10.0))

        call_order: list[tuple[str, str]] = []

        def _fake_metadata(platform, language, *a, **kw):
            call_order.append(("metadata", language))
            if language == "fr":
                return None  # simulate a hard failure for fr
            return _stub_metadata(platform, language, thumbnail_text="Big Reveal")

        def _fake_overlay(base_path, text, language, cid):
            call_order.append(("overlay", language))
            return f"thumbnails/{cid}/{language}.jpg"

        with tempfile.TemporaryDirectory() as tmp:
            base_relative = f"thumbnails/{content_id}/base.jpg"
            _write_real_jpeg(Path(tmp) / base_relative, width=1280, height=720)

            with (
                patch.object(metadata_orchestrator.settings, "media_path", tmp),
                patch.object(system_prompt, "generate_platform_metadata", side_effect=_fake_metadata),
                patch.object(thumbnail, "composite_thumbnail_overlay", side_effect=_fake_overlay),
            ):
                metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

        # "en"'s metadata call must be recorded before "en"'s overlay call.
        en_metadata_idx = call_order.index(("metadata", "en"))
        en_overlay_idx = call_order.index(("overlay", "en"))
        self.assertLess(en_metadata_idx, en_overlay_idx)

        # "fr"'s metadata call failed -> no overlay call for "fr" at all.
        self.assertNotIn(("overlay", "fr"), call_order)

        rows = {r.language: r for r in db.tables[VideoMetadata]}
        self.assertIsNotNone(rows["en"].thumbnail_file_path)
        self.assertNotIn("fr", rows)  # failed metadata call -> no row at all

    def test_base_image_generated_once_and_reused_across_languages(self) -> None:
        db = _FakeDb()
        channel_id = _channel_setup(db, verified_platforms=["youtube"])
        content_id = uuid.uuid4()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            status="RENDERED", story_blueprint={"hook": "h"},
        ))
        for language in ("en", "fr", "es"):
            db.add(Script(content_id=content_id, language=language, voice_script="x", validated=True))
            db.add(VideoRender(content_id=content_id, language=language, format="main", duration_seconds=10.0))

        with tempfile.TemporaryDirectory() as tmp:
            fixture_relative = "cache/fixture/deadbeef.jpg"
            _write_real_jpeg(Path(tmp) / fixture_relative, width=1280, height=720)

            with (
                patch.object(thumbnail.settings, "media_path", tmp),
                patch.object(metadata_orchestrator.settings, "media_path", tmp),
                patch.object(
                    system_prompt, "call_claude_structured",
                    return_value={
                        "title": "t", "description": "d", "hashtags": [],
                        "thumbnail_text": "Reused Base",
                    },
                ),
                patch.object(
                    thumbnail.flux_client, "generate_beat_image", return_value=fixture_relative,
                ) as flux_stub,
            ):
                metadata_orchestrator.run_metadata_generation_for_content(content_id, db)

            # Exactly one Flux call for the whole content item, regardless of
            # 3 languages — the second and third languages' _ensure_base_image
            # calls find the canonical thumbnails/{content_id}/base.jpg file
            # already on disk (Phase C's contract) and reuse it.
            self.assertEqual(flux_stub.call_count, 1)

            rows = db.tables[VideoMetadata]
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual(row.thumbnail_file_path, f"thumbnails/{content_id}/{row.language}.jpg")


if __name__ == "__main__":
    unittest.main()
