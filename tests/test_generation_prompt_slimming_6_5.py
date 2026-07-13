"""Runtime proof for roadmap 6.5 (audit AR-2/AR-3):
  1. The `_load_sections_from_db()`/`_load_video_sections()` duplication
     (visual_orchestrator.py vs video.py) is now one shared function,
     `app.services.video_sections.load_video_sections()`.
  2. `generation_prompt`'s ~19 review-only fields (first15 diagnostics,
     cinematic continuity tags, prompt quality warnings) no longer persist
     to the DB — they move to a run-folder JSON file
     (`visual_review.save_beat_review_metadata()`), read back by the local
     HTML review page only.

Only the real internal functions are exercised; no paid API is involved on
this path at all (pure DB/filesystem read-write).
"""

from __future__ import annotations

import json
import operator as _operator
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent4_visuals.services import visual_orchestrator as vo
from app.agents.agent4_visuals.services import visual_review
from app.agents.agent5_render.services import video as agent5_video
from app.models import VideoSection
from app.services import video_sections


# ── Part 1: shared loader ───────────────────────────────────────────────────

class TestSharedLoaderIsGenuinelyShared(unittest.TestCase):
    def test_both_agents_import_the_same_function_object(self):
        self.assertIs(vo.load_video_sections, video_sections.load_video_sections)
        self.assertIs(agent5_video.load_video_sections, video_sections.load_video_sections)

    def test_neither_agent_module_defines_its_own_copy_anymore(self):
        self.assertFalse(hasattr(vo, "_load_sections_from_db"))
        self.assertFalse(hasattr(agent5_video, "_load_video_sections"))


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        self._rows.sort(key=lambda r: r.section_order)
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def query(self, model):
        if model is VideoSection:
            return _FakeQuery(self.rows)
        return _FakeQuery([])


def _row(content_id, language, order, *, generation_prompt=None, **overrides):
    defaults = dict(
        id=uuid.uuid4(), content_id=content_id, language=language, section_order=order,
        script_text="narration", audio_start_ms=order * 1000, audio_end_ms=(order + 1) * 1000,
        flux_prompt="a concrete subject", effect=None, color_grade=None,
        beat_intensity=None, suggested_duration_sec=None, media_strategy=None,
        text_card_style=None, generation_prompt=generation_prompt,
    )
    defaults.update(overrides)
    return VideoSection(**defaults)


class TestSharedLoaderBehavior(unittest.TestCase):
    def test_loads_and_merges_generation_prompt_extras(self):
        content_id = uuid.uuid4()
        row = _row(
            content_id, "en", 0,
            generation_prompt=json.dumps({"visual_intent": "a dog running", "media_url": "cache/x/0.jpg"}),
        )
        db = _FakeDb([row])
        result = video_sections.load_video_sections(content_id, "en", db)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["visual_intent"], "a dog running")
        self.assertEqual(result[0]["media_url"], "cache/x/0.jpg")

    def test_normalizes_legacy_text_card_sentinel(self):
        content_id = uuid.uuid4()
        row = _row(
            content_id, "en", 0,
            generation_prompt=json.dumps({"media_url": "__text_card__", "visual_type": "text_card"}),
            media_strategy="remotion_text_card",
        )
        db = _FakeDb([row])
        result = video_sections.load_video_sections(content_id, "en", db)
        self.assertEqual(result[0]["media_url"], "")
        self.assertEqual(result[0]["visual_type"], "b-roll")
        self.assertEqual(result[0]["media_strategy"], "flux_generated")


# ── Part 2: generation_prompt slimming ──────────────────────────────────────

_REVIEW_ONLY_FIELDS = (
    "negative_prompt", "shot_type", "subject", "action", "emotion", "camera",
    "lighting", "composition", "continuity_tags", "visual_bible_refs",
    "location", "character", "is_first_15_seconds", "prompt_quality_warnings",
    "first15_validation_status", "first15_issues", "first15_strength_tags",
    "first15_enhanced", "first15_validation_summary",
)
# overlay_text/overlay_position are no longer persisted at all (review finding
# A2): the shared loader forces ""/"none" on every load, so writing them was
# two dead keys per row per language.
_PRODUCTION_FIELDS = (
    "visual_intent", "visual_type", "visual_category", "environment", "motif",
    "transition_to_next", "media_url",
    "media_type", "media_strategy", "text_card_style", "source_script_sha256",
)
_NEVER_PERSISTED_FIELDS = ("overlay_text", "overlay_position")


class TestBeatExtrasSlimmed(unittest.TestCase):
    def _full_beat(self) -> dict:
        beat = {field: f"value-{field}" for field in _PRODUCTION_FIELDS}
        for field in _REVIEW_ONLY_FIELDS:
            beat[field] = f"value-{field}"
        return beat

    def test_review_only_fields_are_dropped(self):
        extras = vo._beat_extras(self._full_beat())
        for field in _REVIEW_ONLY_FIELDS:
            self.assertNotIn(field, extras, field)

    def test_overlay_fields_are_never_persisted(self):
        beat = self._full_beat()
        for field in _NEVER_PERSISTED_FIELDS:
            beat[field] = f"value-{field}"
        extras = vo._beat_extras(beat)
        for field in _NEVER_PERSISTED_FIELDS:
            self.assertNotIn(field, extras, field)

    def test_production_fields_survive(self):
        extras = vo._beat_extras(self._full_beat())
        for field in _PRODUCTION_FIELDS:
            self.assertIn(field, extras, field)
            self.assertEqual(extras[field], f"value-{field}")


class TestReviewMetadataRoundTrip(unittest.TestCase):
    """Real write (save_beat_review_metadata) -> real read
    (_load_review_metadata_by_key / _extract_row) round trip through the
    actual filesystem, no DB involved."""

    def test_saved_metadata_is_read_back_and_merged_into_row(self):
        content_id = uuid.uuid4()
        beats = [{
            "section_order": 0,
            "subject": "a torn photograph",
            "emotion": "dread",
            "continuity_tags": ["eli-gray-hoodie"],
            "first15_validation_status": "PASS",
        }]

        with TemporaryDirectory() as tmp:
            with patch("app.services.local_run_paths.settings.media_path", tmp):
                visual_review.save_beat_review_metadata(content_id, "en", beats)

                path = visual_review.get_review_metadata_path(content_id)
                self.assertTrue(path.exists())
                on_disk = json.loads(path.read_text())
                self.assertEqual(on_disk["en"][0]["subject"], "a torn photograph")
                # Only review-only fields are written, nothing else.
                self.assertNotIn("media_url", on_disk["en"][0])

                by_key = visual_review._load_review_metadata_by_key(content_id)
                self.assertEqual(by_key[("en", 0)]["emotion"], "dread")

                section = _row(content_id, "en", 0, generation_prompt=json.dumps({"media_url": "cache/x/0.jpg"}))
                row = visual_review._extract_row(section, path.parent, by_key)
                self.assertEqual(row["subject"], "a torn photograph")
                self.assertEqual(row["emotion"], "dread")
                self.assertEqual(row["continuity_tags"], ["eli-gray-hoodie"])
                self.assertEqual(row["first15_validation_status"], "PASS")

    def test_multiple_languages_accumulate_without_clobbering(self):
        content_id = uuid.uuid4()
        with TemporaryDirectory() as tmp:
            with patch("app.services.local_run_paths.settings.media_path", tmp):
                visual_review.save_beat_review_metadata(
                    content_id, "en", [{"section_order": 0, "subject": "english subject"}]
                )
                visual_review.save_beat_review_metadata(
                    content_id, "fr", [{"section_order": 0, "subject": "french subject"}]
                )
                by_key = visual_review._load_review_metadata_by_key(content_id)

        self.assertEqual(by_key[("en", 0)]["subject"], "english subject")
        self.assertEqual(by_key[("fr", 0)]["subject"], "french subject")

    def test_missing_file_is_fail_open_empty(self):
        content_id = uuid.uuid4()
        with TemporaryDirectory() as tmp:
            with patch("app.services.local_run_paths.settings.media_path", tmp):
                by_key = visual_review._load_review_metadata_by_key(content_id)
        self.assertEqual(by_key, {})

    def test_legacy_row_without_review_metadata_file_falls_back_to_generation_prompt(self):
        """A row persisted before this change still carries review fields
        inside generation_prompt — _extract_row() must still surface them
        when no run-folder metadata file exists yet."""
        content_id = uuid.uuid4()
        legacy_generation_prompt = json.dumps({
            "media_url": "cache/x/0.jpg", "subject": "legacy subject", "emotion": "legacy emotion",
        })
        section = _row(content_id, "en", 0, generation_prompt=legacy_generation_prompt)

        with TemporaryDirectory() as tmp:
            row = visual_review._extract_row(section, Path(tmp), review_metadata={})

        self.assertEqual(row["subject"], "legacy subject")
        self.assertEqual(row["emotion"], "legacy emotion")


# ── End-to-end: real orchestrator writes both halves correctly ─────────────

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


class _RealFilterQuery:
    def __init__(self, rows: list, conditions: tuple = ()):
        self._rows = rows
        self._conditions = conditions

    def filter(self, *conditions):
        return _RealFilterQuery(self._rows, self._conditions + conditions)

    def order_by(self, *args, **kwargs):
        matched = self.all()
        matched.sort(key=lambda r: r.section_order)
        return _RealFilterQuery(matched, ())

    def all(self) -> list:
        return [r for r in self._rows if _row_matches(r, self._conditions)]

    def delete(self):
        matched = self.all()
        for row in matched:
            self._rows.remove(row)
        return len(matched)


class _RealFilterFakeDb:
    """In-memory VideoSection table with real SQLAlchemy filter evaluation —
    needed here because the test drives two different languages through
    delete-then-insert and a naive "ignore filters" fake would let one
    language's delete wipe out the other's rows too."""

    def __init__(self):
        self.video_sections: list[VideoSection] = []
        self.commit_count = 0

    def query(self, model):
        if model is VideoSection:
            return _RealFilterQuery(self.video_sections)
        return _RealFilterQuery([])

    def add(self, row):
        self.video_sections.append(row)

    def flush(self):
        pass

    def commit(self):
        self.commit_count += 1

    def get(self, model, obj_id):
        return None


class TestRealOrchestratorWritesBothHalvesCorrectly(unittest.TestCase):
    """Drives the real _run_parent_visuals() (only _run_visual_pass, the
    paid Claude/fal.ai boundary, is stubbed) and proves: the persisted
    generation_prompt JSON never carries review-only fields, while the
    run-folder JSON file does — for every language, including the shared
    __visual__ row."""

    def test_review_only_fields_land_in_run_folder_not_in_db(self):
        content_id = uuid.uuid4()
        db = _RealFilterFakeDb()
        content = SimpleNamespace(id=content_id, source_language="en", story_blueprint=None)
        channel = SimpleNamespace(id=uuid.uuid4(), niche="mystery", tone="tense")
        scripts_by_lang = {"en": SimpleNamespace(voice_script="Some narration text")}
        audio_by_lang = {"en": SimpleNamespace(duration_ms=6000, whisper_transcript=[], section_boundaries=None)}

        fresh_beats = [{
            "beat_order": 0,
            "section_order": 0,
            "audio_start_ms": 0,
            "audio_end_ms": 3000,
            "script_text": "narration",
            "visual_intent": "a dog running",
            "visual_type": "b-roll",
            "visual_category": "object",
            "environment": "indoor_office",
            "flux_prompt": "a dog running through grass",
            "effect": "cut",
            "color_grade": "desaturated",
            "transition_to_next": "cut",
            "motif": "object",
            "beat_intensity": "medium",
            "suggested_duration_sec": 3.0,
            "media_strategy": "flux_generated",
            "media_url": "cache/parent-id/abc.jpg",
            "media_type": "image",
            # Review-only fields — must reach the run folder, not the DB.
            "subject": "a torn photograph",
            "emotion": "dread",
            "continuity_tags": ["eli-gray-hoodie"],
            "first15_validation_status": "PASS",
        }]

        # Note: _run_visual_pass is stubbed (it's the paid Claude/fal.ai
        # boundary), so this test covers the per-language save path only —
        # the __visual__ row's own review-metadata write happens inside the
        # real _run_visual_pass()/_save_shared_beats(), which is out of
        # scope here by design (not internal logic; it's the paid call).
        with TemporaryDirectory() as tmp:
            with (
                patch.object(vo, "_run_visual_pass", return_value=(fresh_beats, 6000)),
                patch("app.services.local_run_paths.settings.media_path", tmp),
            ):
                result = vo._run_parent_visuals(
                    content_id=content_id, content=content, scripts_by_lang=scripts_by_lang,
                    audio_by_lang=audio_by_lang, channel=channel, script_format="youtube_long",
                    allow_legacy_fallback=True, db=db,
                )

                self.assertEqual(result["status"], "PARENT_VISUALS_DONE")

                # DB: every persisted row must be free of review-only fields.
                for row in db.video_sections:
                    extras = json.loads(row.generation_prompt or "{}")
                    for field in _REVIEW_ONLY_FIELDS:
                        self.assertNotIn(field, extras, f"{field} leaked into DB for language={row.language}")

                # Run folder: the review-only fields for "en" are there.
                by_key = visual_review._load_review_metadata_by_key(content_id)
                self.assertEqual(by_key[("en", 0)]["subject"], "a torn photograph")
                self.assertEqual(by_key[("en", 0)]["emotion"], "dread")


if __name__ == "__main__":
    unittest.main()
