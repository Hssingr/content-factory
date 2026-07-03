import json
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.agents.agent4_visuals.services.visual_bible import (
    generate_visual_bible_for_content,
    get_visual_bible_path,
    load_visual_bible_for_content,
    validate_visual_bible,
)
from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelPlatform, Content, Script


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
    def __init__(self, content, channel, config, scripts=None, languages=None, platforms=None):
        self.content = content
        self.channel = channel
        self.config = config
        self.scripts = scripts or []
        self.languages = languages or []
        self.platforms = platforms or []

    def get(self, model, row_id):
        if model is Content and str(row_id) == str(self.content.id):
            return self.content
        if model is Channel and str(row_id) == str(self.channel.id):
            return self.channel
        if model is ChannelConfig and str(row_id) == str(self.channel.id):
            return self.config
        return None

    def query(self, model):
        if model is Script:
            return _FakeQuery(self.scripts)
        if model is ChannelLanguage:
            return _FakeQuery(self.languages)
        if model is ChannelPlatform:
            return _FakeQuery(self.platforms)
        return _FakeQuery([])


def _valid_payload():
    return {
        "story_visual_summary": "A grounded visual world with recurring hallway shadows and stable character identity.",
        "global_style": {
            "realism_level": "photorealistic",
            "visual_style": "documentary horror",
            "image_style": "cinematic photorealistic",
            "color_grade": "cool desaturated",
            "lighting_rules": ["single motivated light source", "no neon randomness"],
            "camera_language": ["slow push-ins", "locked-off tense frames"],
            "lens_style": "35mm natural perspective",
            "composition_rules": ["foreground obstruction", "clear subject silhouette"],
        },
        "characters": [{
            "name": "Mara",
            "role": "witness",
            "approx_age": "30s",
            "appearance": "dark hair, tired eyes",
            "clothing": "grey coat",
            "body_language": "guarded",
            "emotional_arc": "controlled fear to resolve",
            "continuity_tags": ["mara-grey-coat"],
        }],
        "locations": [{
            "name": "Apartment hallway",
            "description": "narrow old hallway",
            "time_of_day": "night",
            "lighting": "flickering warm ceiling light",
            "color_palette": "green-grey walls",
            "recurring_details": ["peeling paint"],
            "continuity_tags": ["hallway-green-grey"],
        }],
        "recurring_motifs": [{
            "name": "half-open door",
            "visual_description": "door open a finger width",
            "symbolic_role": "intrusion",
            "usage_rule": "use at turning points only",
        }],
        "continuity_rules": ["Mara always wears the grey coat", "Hallway walls remain green-grey"],
        "negative_prompt_rules": ["no readable text", "no logos", "no random extra characters"],
        "first_15_seconds_rules": ["show the hallway geography", "establish Mara's silhouette"],
        "forbidden_generic_shots": ["generic screaming face", "random cemetery", "floating skull"],
    }


def _content(channel_id, *, snapshot=None, is_short=False, parent_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        channel_id=channel_id,
        source_language="en",
        title="The Hallway",
        story_blueprint={"major_turns": ["door", "whisper"]},
        channel_config_snapshot=snapshot,
        is_short_episode=is_short,
        parent_content_id=parent_id,
        short_part_number=1,
        short_total_parts=3,
    )


def _db(snapshot=None, *, content=None):
    channel_id = content.channel_id if content is not None else uuid.uuid4()
    channel = SimpleNamespace(
        id=channel_id,
        name="Night Channel",
        description="Narrative horror stories",
        niche="horror",
        tone="tense documentary",
    )
    content = content or _content(channel.id, snapshot=snapshot)
    config = SimpleNamespace(
        content_mode="single_story",
        script_source="reddit",
        output_mode="youtube_and_shorts",
        visual_style="documentary horror",
        image_style="cinematic photorealistic",
        video_style_type="documentary",
        video_color_grade="cool desaturated",
    )
    script = SimpleNamespace(
        content_id=content.id,
        language="en",
        voice_script="Mara walks down the hallway and hears a whisper.",
        validated=True,
        version=1,
    )
    languages = [SimpleNamespace(channel_id=channel.id, language="en")]
    platforms = [SimpleNamespace(channel_id=channel.id, platform="youtube", active=True, credentials_encrypted="secret")]
    return _FakeDb(content, channel, config, [script], languages, platforms)


class TestVisualBible(unittest.TestCase):
    def test_valid_visual_bible_passes_validation(self):
        bible = _valid_payload()
        bible.update({
            "version": "1.0",
            "content_id": "123",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "config_context": {"source": "live_channel_config", "visual_style": "documentary horror", "image_style": "cinematic photorealistic"},
        })
        self.assertFalse([i for i in validate_visual_bible(bible) if i.severity == "BLOCKING"])

    def test_missing_required_fields_block(self):
        issues = validate_visual_bible({"story_visual_summary": ""})
        codes = {issue.code for issue in issues if issue.severity == "BLOCKING"}
        self.assertIn("missing_config_context", codes)
        self.assertIn("global_style_invalid", codes)
        self.assertIn("continuity_rules_invalid", codes)
        self.assertIn("story_visual_summary_empty", codes)

    def test_empty_continuity_rules_warns_without_blocking(self):
        bible = _valid_payload()
        bible["continuity_rules"] = []
        bible.update({
            "version": "1.0",
            "content_id": "123",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "config_context": {"source": "live_channel_config", "visual_style": "documentary horror", "image_style": "cinematic photorealistic"},
        })
        issues = validate_visual_bible(bible)
        blocking_codes = {issue.code for issue in issues if issue.severity == "BLOCKING"}
        warning_codes = {issue.code for issue in issues if issue.severity == "WARNING"}
        self.assertNotIn("continuity_rules_empty", blocking_codes)
        self.assertIn("continuity_rules_empty", warning_codes)

    def test_generates_file_with_live_channel_config(self):
        with TemporaryDirectory() as tmp:
            db = _db()
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_bible.call_claude_structured", return_value=_valid_payload()) as call:
                bible = generate_visual_bible_for_content(db.content.id, db)
                path_exists = get_visual_bible_path(db.content.id).is_file()

        self.assertTrue(path_exists)
        self.assertEqual(bible["config_context"]["source"], "live_channel_config")
        self.assertEqual(bible["config_context"]["visual_style"], "documentary horror")
        self.assertEqual(bible["config_context"]["image_style"], "cinematic photorealistic")
        self.assertEqual(bible["config_context"]["target_platforms"], ["youtube"])
        self.assertNotIn("credentials_encrypted", json.dumps(bible))
        call.assert_called_once()

    def test_snapshot_config_context_is_preferred(self):
        snapshot = {"visual_style": "noir", "image_style": "grainy photo", "target_languages": ["en", "fr"]}
        with TemporaryDirectory() as tmp:
            db = _db(snapshot=snapshot)
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_bible.call_claude_structured", return_value=_valid_payload()):
                bible = generate_visual_bible_for_content(db.content.id, db)

        self.assertEqual(bible["config_context"]["source"], "channel_config_snapshot")
        self.assertEqual(bible["config_context"]["visual_style"], "noir")
        self.assertEqual(bible["config_context"]["image_style"], "grainy photo")

    def test_existing_valid_file_reused_when_force_false(self):
        with TemporaryDirectory() as tmp:
            db = _db()
            with patch("app.services.local_run_paths.settings.media_path", tmp):
                path = get_visual_bible_path(db.content.id)
                path.parent.mkdir(parents=True, exist_ok=True)
                existing = _valid_payload()
                existing.update({
                    "version": "1.0",
                    "content_id": str(db.content.id),
                    "generated_at": "existing",
                    "config_context": {"source": "live_channel_config", "visual_style": "documentary horror", "image_style": "cinematic photorealistic"},
                })
                path.write_text(json.dumps(existing), encoding="utf-8")
                with patch("app.agents.agent4_visuals.services.visual_bible.call_claude_structured") as call:
                    bible = generate_visual_bible_for_content(db.content.id, db)

        self.assertEqual(bible["generated_at"], "existing")
        call.assert_not_called()

    def test_force_true_regenerates(self):
        with TemporaryDirectory() as tmp:
            db = _db()
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_bible.call_claude_structured", return_value=_valid_payload()) as call:
                generate_visual_bible_for_content(db.content.id, db)
                generate_visual_bible_for_content(db.content.id, db, force=True)

        self.assertEqual(call.call_count, 2)

    def test_invalid_claude_response_rejected_after_retry(self):
        with TemporaryDirectory() as tmp:
            db = _db()
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_bible.call_claude_structured", return_value={"story_visual_summary": ""}):
                with self.assertRaises(ValueError):
                    generate_visual_bible_for_content(db.content.id, db)

    def test_parent_bible_copied_for_child(self):
        with TemporaryDirectory() as tmp:
            channel_id = uuid.uuid4()
            parent = _content(channel_id)
            child = _content(channel_id, is_short=True, parent_id=parent.id)
            parent_db = _db(content=parent)
            child_db = _db(content=child)
            with patch("app.services.local_run_paths.settings.media_path", tmp), \
                 patch("app.agents.agent4_visuals.services.visual_bible.call_claude_structured", return_value=_valid_payload()) as call:
                generate_visual_bible_for_content(parent.id, parent_db)
                child_bible = generate_visual_bible_for_content(child.id, child_db)
                loaded = load_visual_bible_for_content(child.id)

        self.assertEqual(call.call_count, 1)
        self.assertEqual(child_bible["config_context"]["source"], "parent_visual_bible")
        self.assertEqual(loaded["parent_content_id"], str(parent.id))


if __name__ == "__main__":
    unittest.main()
