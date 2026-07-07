"""Runtime proof for the source-material floor (roadmap 4b / audit P1-5,
code_report/forensic_output_audit_borrasca_run.md).

A real production run generated a 1,384-word long-form script grounded on a
3,070-char (~560-word) source_excerpt for a ~45,000-word novella — every
specific detail beyond that thin summary was necessarily model-invented, and
no validator could check faithfulness against a source that isn't there.
This proves: (1) check_source_material_floor() flags a too-thin source per
the channel's configured script_format, and (2) the real (unmocked)
generate_parent_source_script() fails the discovery->script handoff loud —
Content.status becomes FAILED, it returns None, and it makes ZERO calls to
blueprint/section/quality-gate generation — before touching a sufficiently
grounded source, which must proceed exactly as before this change.
"""

from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from app.agents.agent2_discovery.services import script_workflow
from app.models import Channel, ChannelConfig, ChannelVoice, Content
from app.services.script_checks import check_source_material_floor


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        return _FakeQuery(self.rows[:n])

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self):
        self.tables: dict = {}
        self.commits = 0

    def get(self, model, key):
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key:
                return row
        return None

    def query(self, model):
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def commit(self):
        self.commits += 1

    def refresh(self, row):
        pass


def _long_enough_source(word_count: int = 950) -> str:
    return " ".join(f"detail{i}" for i in range(word_count))


class TestCheckSourceMaterialFloor(unittest.TestCase):
    def test_thin_source_flags_major_for_youtube_long(self):
        # The audited real incident: ~560 source words for a youtube_long target.
        issues = check_source_material_floor(
            " ".join(["word"] * 560), "en", "youtube_long",
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "MAJOR")
        self.assertEqual(issues[0]["category"], "source_material_floor")
        self.assertEqual(issues[0]["language"], "en")

    def test_adequate_source_passes_for_youtube_long(self):
        issues = check_source_material_floor(_long_enough_source(900), "en", "youtube_long")
        self.assertEqual(issues, [])

    def test_floor_is_lower_for_non_long_form(self):
        # 500 words fails the 900-word youtube_long floor but clears the
        # 420-word floor used by every other script_format.
        thin_for_long = " ".join(["word"] * 500)
        self.assertEqual(
            len(check_source_material_floor(thin_for_long, "en", "youtube_long")), 1,
        )
        self.assertEqual(
            check_source_material_floor(thin_for_long, "en", "short_form"), [],
        )

    def test_empty_source_flags_major(self):
        issues = check_source_material_floor("", "en", "youtube_long")
        self.assertEqual(len(issues), 1)


class TestGenerateParentSourceScriptFailsHandoffOnThinSource(unittest.TestCase):
    """Full-chain proof: only the paid Claude boundaries are poisoned — the
    real generate_parent_source_script() must never reach them when the
    source-material floor fails."""

    def _fixtures(self, *, source_excerpt: str):
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db = _FakeDb()
        db.add(Channel(id=channel_id, niche="horror", tone="tense"))
        db.add(ChannelConfig(
            channel_id=channel_id, script_format="youtube_long",
            visual_style="documentary", image_style="photorealistic",
            audio_tags_enabled=False,
        ))
        db.add(ChannelVoice(
            id=uuid.uuid4(), channel_id=channel_id, language="en",
            provider="cartesia", voice_id="v1", tts_model="sonic-3.5",
        ))
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="APPROVED", title="T",
            source_url="https://example.com/x", source_excerpt=source_excerpt,
        )
        db.add(content)
        return db, content

    def _poisoned(self, name):
        def _raise(*args, **kwargs):
            raise AssertionError(
                f"{name}() must never be called when the source-material "
                "floor already failed the discovery->script handoff"
            )
        return _raise

    def test_thin_source_sets_failed_and_returns_none_with_no_claude_calls(self):
        # Real audited incident: 3,070 chars (~560 words) for a youtube_long target.
        db, content = self._fixtures(source_excerpt=" ".join(["word"] * 560))

        with (
            patch.object(script_workflow, "generate_story_blueprint",
                         side_effect=self._poisoned("generate_story_blueprint")),
            patch.object(script_workflow, "run_script_quality_gate",
                         side_effect=self._poisoned("run_script_quality_gate")),
        ):
            result = script_workflow.generate_parent_source_script(content, db)

        self.assertIsNone(result)
        self.assertEqual(content.status, "FAILED")
        self.assertEqual(db.commits, 1)

    def test_empty_source_excerpt_sets_failed(self):
        db, content = self._fixtures(source_excerpt="")

        with patch.object(script_workflow, "generate_story_blueprint",
                           side_effect=self._poisoned("generate_story_blueprint")):
            result = script_workflow.generate_parent_source_script(content, db)

        self.assertIsNone(result)
        self.assertEqual(content.status, "FAILED")

    def test_logs_source_material_floor_failed_marker(self):
        db, content = self._fixtures(source_excerpt=" ".join(["word"] * 100))

        with (
            patch.object(script_workflow, "generate_story_blueprint",
                         side_effect=self._poisoned("generate_story_blueprint")),
            self.assertLogs(
                "app.agents.agent2_discovery.services.script_workflow", level="ERROR",
            ) as log_ctx,
        ):
            script_workflow.generate_parent_source_script(content, db)

        joined = " ".join(log_ctx.output)
        self.assertIn("SOURCE_MATERIAL_FLOOR_FAILED", joined)

    def test_adequate_source_proceeds_unchanged(self):
        """Backward compatibility: a well-grounded source must still generate
        exactly as before this change — real (unmocked) blueprint/section
        stubs run, quality gate runs for real, and status reaches
        GENERATING_SCRIPTS, never FAILED."""
        db, content = self._fixtures(source_excerpt=_long_enough_source(950))

        def fake_blueprint(story, channel, **kwargs):
            return {
                "major_turns": ["t1"], "suggested_section_count": 1,
                "hook": "h", "final_payoff": "p", "comment_trigger": "c?",
                "midpoint_retention_trap": "m", "central_question": "q",
                "suggested_title": "T",
            }

        def fake_sections(**kwargs):
            return {
                "title": "T",
                "voice_script": "[INTRO]\nSome narration here.\n[OUTRO]\nDone.\n",
                "visual_intent_history": [],
            }

        with (
            patch.object(script_workflow, "generate_story_blueprint", side_effect=fake_blueprint),
            patch.object(script_workflow, "generate_script_sections", side_effect=fake_sections),
        ):
            result = script_workflow.generate_parent_source_script(content, db)

        self.assertIsNotNone(result)
        self.assertEqual(content.status, "GENERATING_SCRIPTS")


if __name__ == "__main__":
    unittest.main()
