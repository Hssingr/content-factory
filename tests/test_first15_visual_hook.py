import json
import uuid
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent4_visuals.services.first15_validator import (
    apply_first15_enhancement_and_validation,
    enhance_first15_prompt_data,
    validate_first_15_seconds,
)
from app.agents.agent4_visuals.services.visual_review import generate_visual_review_html
from app.models import AudioFile, Content, Script, VideoSection


def bible():
    return {
        "config_context": {
            "visual_style": "documentary horror",
            "image_style": "cinematic photorealistic",
            "video_color_grade": "cold blue-gray",
        },
        "global_style": {
            "visual_style": "documentary horror",
            "image_style": "cinematic photorealistic",
            "color_grade": "cold blue-gray",
        },
        "first_15_seconds_rules": ["open with a dangerous clue", "show a readable human reaction"],
        "negative_prompt_rules": ["no readable text"],
        "forbidden_generic_shots": ["generic dark street", "generic empty room"],
    }


def beat(**extra):
    data = {
        "beat_order": 0,
        "audio_start_ms": 0,
        "audio_end_ms": 4500,
        "script_text": "Mara's frightened face catches the cold light as muddy footprints lead from the closet.",
        "flux_prompt": "Cinematic high-tension close-up of Mara's frightened face beside muddy footprints leading from the closet, cold light, shallow focus, specific bedroom geography, readable dread, no text.",
        "subject": "Mara's frightened face",
        "action": "stares at muddy footprints",
        "emotion": "dread",
        "environment": "child bedroom with muddy footprints",
        "is_first_15_seconds": True,
    }
    data.update(extra)
    return data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
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
        if model in (Script, AudioFile):
            return _FakeQuery([])
        return _FakeQuery([])


class TestFirst15VisualHook(unittest.TestCase):
    def test_passes_high_tension_human_subject(self):
        result = validate_first_15_seconds([beat()], visual_bible=bible())
        self.assertIn(result.status, {"PASS", "PASS_WITH_WARNINGS"})
        self.assertEqual(result.strong_hook_count, 1)

    def test_passes_mysterious_object_hook(self):
        result = validate_first_15_seconds([
            beat(
                script_text="A wet key turns by itself beside a family portrait scratched until every face is gone.",
                flux_prompt="Cinematic close-up of a wet key turning by itself beside a scratched family portrait, cold moonlight, mystery, specific hallway detail, immediate visual question.",
                subject="wet key beside scratched portrait",
                action="turns by itself",
                emotion="mystery",
            )
        ], visual_bible=bible())
        self.assertIn(result.status, {"PASS", "PASS_WITH_WARNINGS"})
        self.assertEqual(result.strong_hook_count, 1)

    def test_flags_generic_dark_street(self):
        result = validate_first_15_seconds([
            beat(script_text="A generic dark street at night.", flux_prompt="A generic dark street at night.", subject="", action="", emotion="")
        ], visual_bible=bible())
        codes = {issue.code for issue in result.issues}
        self.assertIn("generic_opening_shot", codes)
        self.assertEqual(result.status, "FAIL_BLOCKING")

    def test_flags_generic_forest_and_empty_room(self):
        forest = validate_first_15_seconds([
            beat(script_text="A generic forest at night.", flux_prompt="A generic forest at night.", subject="", action="", emotion="")
        ], visual_bible=bible())
        room = validate_first_15_seconds([
            beat(script_text="A generic empty room.", flux_prompt="A generic empty room.", subject="", action="", emotion="")
        ], visual_bible=bible())
        self.assertIn("first_15_all_generic", {issue.code for issue in forest.issues})
        self.assertIn("empty_room_without_specific_detail", {issue.code for issue in room.issues})

    def test_allows_specific_empty_room_with_clue(self):
        result = validate_first_15_seconds([
            beat(
                script_text="An empty child's bedroom with muddy footprints leading from the closet and one chair pulled back.",
                flux_prompt="Cinematic shot of an empty child's bedroom with muddy footprints leading from the closet, one chair pulled back, cold moonlight, immediate mystery.",
                subject="muddy footprints leading from the closet",
                action="leading from the closet",
                emotion="dread",
            )
        ], visual_bible=bible())
        self.assertNotEqual(result.status, "FAIL_BLOCKING")
        self.assertGreaterEqual(result.strong_hook_count, 1)

    def test_blocks_missing_first15_beats(self):
        result = validate_first_15_seconds([beat(audio_start_ms=16000)], visual_bible=bible())
        self.assertEqual(result.status, "FAIL_BLOCKING")
        self.assertIn("first_15_no_visual_sections", {issue.code for issue in result.issues})

    def test_enhancer_uses_visual_bible_rules_and_marks_metadata(self):
        enhanced = enhance_first15_prompt_data(
            {"audio_start_ms": 0, "flux_prompt": "A scary place at night.", "prompt_metadata": {}},
            visual_bible=bible(),
            beat_index=0,
        )
        self.assertTrue(enhanced["is_first_15_seconds"])
        self.assertTrue(enhanced["first15_enhanced"])
        self.assertIn("open with a dangerous clue", enhanced["flux_prompt"])
        self.assertIn("documentary horror", enhanced["flux_prompt"])

    def test_apply_marks_first15_metadata_with_child_timing(self):
        beats, result = apply_first15_enhancement_and_validation(
            [beat(audio_start_ms=200), beat(audio_start_ms=17000)],
            visual_bible=bible(),
            content_kind="child_short",
        )
        self.assertIn(result.status, {"PASS", "PASS_WITH_WARNINGS"})
        self.assertTrue(beats[0]["is_first_15_seconds"])
        self.assertEqual(beats[0]["first15_validation_status"], result.status)
        self.assertNotIn("first15_issues", beats[1])

    def test_visual_review_renders_first15_metadata_safely(self):
        with TemporaryDirectory() as tmp:
            content_id = uuid.uuid4()
            extras = {
                "media_url": "cache/missing.jpg",
                "is_first_15_seconds": True,
                "first15_validation_status": "PASS_WITH_WARNINGS",
                "first15_strength_tags": ["human_focus", "<unsafe>"],
                "first15_enhanced": True,
                "first15_issues": [{"severity": "WARNING", "code": "missing_emotion", "message": "Needs <emotion>."}],
                "first15_validation_summary": {
                    "status": "PASS_WITH_WARNINGS",
                    "checked_count": 1,
                    "strong_hook_count": 1,
                    "weak_generic_count": 0,
                    "issues": [{"code": "missing_emotion"}],
                },
            }
            section = SimpleNamespace(
                content_id=content_id,
                language="en",
                section_order=1,
                script_text="Opening",
                audio_start_ms=0,
                audio_end_ms=3000,
                flux_prompt="Prompt",
                effect="",
                color_grade="",
                generation_prompt=json.dumps(extras),
                beat_intensity="high",
                suggested_duration_sec=3,
                media_strategy="flux_generated",
                text_card_style="default",
            )
            content = SimpleNamespace(id=content_id, title="Title", status="PARENT_VISUALS_DONE", is_short_episode=False)
            db = _FakeDb(content, [section])
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_review.settings.media_path", tmp):
                html = generate_visual_review_html(content_id, db).read_text(encoding="utf-8")

        self.assertIn("First 15 Seconds", html)
        self.assertIn("PASS_WITH_WARNINGS", html)
        self.assertIn("&lt;unsafe&gt;", html)
        self.assertIn("Needs &lt;emotion&gt;.", html)


if __name__ == "__main__":
    unittest.main()
