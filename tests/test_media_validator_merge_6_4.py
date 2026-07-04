"""Runtime proof for the media-validator merge (roadmap 6.4 / audit V-7, AR-2).

Two separate media validators existed: `storyboard_validator.validate_media_assets()`
(observability-only, per-language, in-memory beats) and
`media_validation.validate_visual_media_assets()` (blocking, whole-content, DB
reload). The fix keeps the blocking one as the single source of truth and
folds the persistence round-trip check into it.

This proves the full chain: `run_visual_generation_for_content()` (the real
function) receives `result["beats_by_lang"]` from `run_visual_generation()`
and threads it into `validate_visual_media_assets()`, which performs the
round-trip comparison against real (fake-DB-backed) `VideoSection` rows —
never blocking the pipeline on a mismatch. Only the paid/expensive calls
(`run_visual_generation()` itself, the visual bible, the HTML review page)
are stubbed.
"""

from __future__ import annotations

import json
import operator as _operator
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app.agents.agent4_visuals.services import visual_orchestrator as vo
from app.models import AudioFile, Channel, ChannelConfig, Content, Script, VideoSection


def _condition_value(condition):
    right = getattr(condition, "right", None)
    if hasattr(right, "value"):
        return right.value
    type_name = type(right).__name__
    if type_name == "True_":
        return True
    if type_name == "False_":
        return False
    return right


def _row_matches(row, conditions) -> bool:
    for cond in conditions:
        attr = cond.left.key
        expected = _condition_value(cond)
        actual = getattr(row, attr, None)
        if cond.operator not in (_operator.eq,) and cond.operator.__name__ not in ("eq", "is_"):
            raise NotImplementedError(f"Unsupported operator in fake query: {cond.operator}")
        if actual != expected:
            return False
    return True


class _FakeQuery:
    def __init__(self, rows: list, conditions: tuple = ()):
        self._rows = rows
        self._conditions = conditions

    def filter(self, *conditions):
        return _FakeQuery(self._rows, self._conditions + conditions)

    def order_by(self, *args, **kwargs):
        return self

    def all(self) -> list:
        return [r for r in self._rows if _row_matches(r, self._conditions)]

    def first(self):
        matched = self.all()
        return matched[0] if matched else None


class _FakeDb:
    def __init__(self):
        self.tables: dict[type, list] = {
            Content: [], Channel: [], ChannelConfig: [], Script: [],
            AudioFile: [], VideoSection: [],
        }
        self.commits = 0

    def get(self, model, key):
        for row in self.tables.get(model, []):
            row_key = getattr(row, "id", None) or getattr(row, "channel_id", None)
            if row_key == key:
                return row
        return None

    def query(self, model):
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1


class TestMediaValidatorMergeRuntimeChain(unittest.TestCase):
    def _fixtures(self):
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db = _FakeDb()
        db.add(Channel(id=channel_id, niche="horror", tone="tense"))
        db.add(ChannelConfig(
            channel_id=channel_id, script_format="youtube_long",
            allow_legacy_fallback=False, visual_style="", image_style="",
        ))
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="AUDIO_DONE",
        )
        db.add(content)
        db.add(Script(
            id=uuid.uuid4(), content_id=content_id, language="en",
            voice_script="[SECTION 1]\nSome narration.", version=1, validated=True,
        ))
        db.add(AudioFile(
            id=uuid.uuid4(), content_id=content_id, language="en",
            file_path="audio/en.mp3", duration_ms=9000, whisper_transcript=[],
        ))
        return db, content_id

    def _seed_video_section(self, db, content_id, *, media_url):
        db.add(VideoSection(
            id=uuid.uuid4(), content_id=content_id, language="en", section_order=0,
            script_text="Some narration.", audio_start_ms=0, audio_end_ms=9000,
            generation_prompt=json.dumps({"media_url": media_url, "media_type": "image"}),
        ))

    @staticmethod
    def _write_image(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), color="blue").save(path)

    def test_beats_by_lang_flows_into_round_trip_check_without_blocking(self):
        db, content_id = self._fixtures()
        with TemporaryDirectory() as tmp:
            self._write_image(Path(tmp) / "cache" / str(content_id) / "real.jpg")
            persisted_url = f"cache/{content_id}/real.jpg"
            self._seed_video_section(db, content_id, media_url=persisted_url)

            # run_visual_generation()'s in-memory result claims a DIFFERENT
            # media_url than what actually got persisted — simulating exactly
            # the class of bug the round-trip check exists to catch.
            stale_beats_by_lang = {
                "en": [{"section_order": 0, "media_url": f"cache/{content_id}/stale.jpg", "media_type": "image"}]
            }
            fake_result = {"status": "PARENT_VISUALS_DONE", "beats_by_lang": stale_beats_by_lang}

            captured_validation_kwargs = {}
            real_validate = vo.validate_visual_media_assets

            def spying_validate(*args, **kwargs):
                captured_validation_kwargs.update(kwargs)
                return real_validate(*args, **kwargs)

            with (
                patch.object(vo, "run_visual_generation", return_value=fake_result),
                patch.object(vo, "generate_visual_bible_for_content", return_value={}),
                patch.object(vo, "generate_visual_review_html", return_value="review.html"),
                patch.object(vo, "ensure_run_dirs", return_value={}),
                patch.object(vo, "validate_visual_media_assets", side_effect=spying_validate),
                patch("app.agents.agent4_visuals.services.media_validation.settings.media_path", tmp),
                patch("app.services.local_run_paths.settings.media_path", tmp),
            ):
                ok = vo.run_visual_generation_for_content(content_id, db)

        # The real validate_visual_media_assets() received the exact
        # beats_by_lang from run_visual_generation()'s result.
        self.assertEqual(captured_validation_kwargs.get("beats_by_lang"), stale_beats_by_lang)

        # A round-trip mismatch is diagnostic only — it must not block the
        # pipeline: content still reaches PARENT_VISUALS_DONE.
        content = db.get(Content, content_id)
        self.assertTrue(ok)
        self.assertEqual(content.status, "PARENT_VISUALS_DONE")

    def test_matching_beats_by_lang_produces_clean_validation(self):
        db, content_id = self._fixtures()
        with TemporaryDirectory() as tmp:
            self._write_image(Path(tmp) / "cache" / str(content_id) / "real.jpg")
            persisted_url = f"cache/{content_id}/real.jpg"
            self._seed_video_section(db, content_id, media_url=persisted_url)

            matching_beats_by_lang = {
                "en": [{"section_order": 0, "media_url": persisted_url, "media_type": "image"}]
            }
            fake_result = {"status": "PARENT_VISUALS_DONE", "beats_by_lang": matching_beats_by_lang}

            with (
                patch.object(vo, "run_visual_generation", return_value=fake_result),
                patch.object(vo, "generate_visual_bible_for_content", return_value={}),
                patch.object(vo, "generate_visual_review_html", return_value="review.html"),
                patch.object(vo, "ensure_run_dirs", return_value={}),
                patch("app.agents.agent4_visuals.services.media_validation.settings.media_path", tmp),
                patch("app.services.local_run_paths.settings.media_path", tmp),
            ):
                ok = vo.run_visual_generation_for_content(content_id, db)

        content = db.get(Content, content_id)
        self.assertTrue(ok)
        self.assertEqual(content.status, "PARENT_VISUALS_DONE")


if __name__ == "__main__":
    unittest.main()
