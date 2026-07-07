"""Runtime proofs for the roadmap close-out fixes (review findings R1/R2/R4).

R1 (roadmap 6.1 / audit S-8, C-2): run_shorts_planner() must check for
    existing child Shorts BEFORE its paid Claude plan call — a re-run against
    a parent whose Shorts already exist costs zero API calls.
R2 (roadmap 6.7 / audit §8.6): settings.whisper_local_primary=True makes
    local faster-whisper the primary transcription engine and the OpenAI
    API the fallback; default False preserves the pre-existing order.
R4 (roadmap 4.7 / audit AR-1): generate_parent_source_script() is the single
    shared blueprint→sections→quality-gate→persist implementation, called by
    BOTH run_script_workflow() and the operator harness.

Only paid boundaries are stubbed (Claude plan call, OpenAI Whisper API,
faster-whisper model, blueprint/section/gate Claude calls) — never internal
orchestration logic.
"""

from __future__ import annotations

import operator as _operator
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import scripts as agent2_scripts
from app.agents.agent2_discovery.services import script_workflow
from app.agents.agent3_audio.services import whisper as whisper_svc
from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script


# ── Shared SQLAlchemy-expression-aware fake DB (same precedent as
#    test_script_validation_bypass_fix.py) ────────────────────────────────────

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

    def join(self, *a, **k):
        return self

    def filter(self, *conditions):
        return _FakeQuery(self._rows, self._conditions + conditions)

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _FakeQuery(self.all()[:n], ())

    def all(self):
        return [r for r in self._rows if _row_matches(r, self._conditions)]

    def first(self):
        matched = self.all()
        return matched[0] if matched else None

    def count(self):
        return len(self.all())


class _FakeDb:
    def __init__(self):
        self.tables: dict[type, list] = {
            Content: [], Channel: [], ChannelConfig: [], ChannelVoice: [], Script: [],
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

    def refresh(self, row):
        pass


# ── R1: shorts planner checks existing children before the plan call ────────

class TestShortsPlannerChecksChildrenBeforePlanCall(unittest.TestCase):
    def _fixtures(self, *, with_children: bool):
        channel_id = uuid.uuid4()
        parent_id = uuid.uuid4()
        db = _FakeDb()
        channel = Channel(id=channel_id, niche="horror", tone="tense")
        db.add(channel)
        config = ChannelConfig(channel_id=channel_id, script_format="youtube_long")
        db.add(config)
        parent = Content(
            id=parent_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="SCRIPTS_VALIDATED",
            story_blueprint={"major_turns": ["t1", "t2"]},
        )
        db.add(parent)
        db.add(Script(
            id=uuid.uuid4(), content_id=parent_id, language="en",
            voice_script="[SECTION 1]\nNarration.", version=1, validated=True,
        ))
        if with_children:
            db.add(Content(
                id=uuid.uuid4(), channel_id=channel_id, is_short_episode=True,
                parent_content_id=parent_id, source_language="en",
                status="SCRIPTS_VALIDATED", short_part_number=1, short_total_parts=3,
            ))
        return db, channel, config, parent_id

    def test_existing_children_return_before_any_plan_call(self):
        db, channel, config, parent_id = self._fixtures(with_children=True)
        with patch.object(
            agent2_scripts, "generate_shorts_plan",
            side_effect=AssertionError("paid plan call must not happen when children exist"),
        ):
            agent2_scripts.run_shorts_planner(parent_id, channel, config, db)
        # Reaching here without the AssertionError = the check ran first.

    def test_no_children_still_reaches_the_plan_call(self):
        db, channel, config, parent_id = self._fixtures(with_children=False)
        calls = []

        def fake_plan(voice_script, blueprint, channel_arg):
            calls.append(1)
            return None  # planner declines — planner exits cleanly after the call

        with patch.object(agent2_scripts, "generate_shorts_plan", side_effect=fake_plan):
            agent2_scripts.run_shorts_planner(parent_id, channel, config, db)
        self.assertEqual(len(calls), 1)


# ── R2: whisper_local_primary flag ───────────────────────────────────────────

class TestWhisperLocalPrimaryFlag(unittest.TestCase):
    _WORDS = [{"word": "hi", "start": 0.0, "end": 0.4}]

    def _run(self, *, flag: bool, local_result, openai_result):
        order: list[str] = []

        def fake_local(path, language):
            order.append("local")
            return local_result

        def fake_openai(path, language):
            order.append("openai")
            return openai_result

        with (
            patch.object(whisper_svc.settings, "whisper_local_primary", flag),
            patch.object(whisper_svc, "_try_faster_whisper", side_effect=fake_local),
            patch.object(whisper_svc, "_try_openai_whisper", side_effect=fake_openai),
            patch.object(whisper_svc.Path, "exists", return_value=True),
        ):
            result = whisper_svc.transcribe("/fake/audio.mp3", language="en")
        return order, result

    def test_flag_on_local_first_and_no_api_call_on_success(self):
        order, result = self._run(flag=True, local_result=self._WORDS, openai_result=None)
        self.assertEqual(order, ["local"])
        self.assertEqual(result, self._WORDS)

    def test_flag_on_falls_back_to_openai_when_local_fails(self):
        order, result = self._run(flag=True, local_result=None, openai_result=self._WORDS)
        self.assertEqual(order, ["local", "openai"])
        self.assertEqual(result, self._WORDS)

    def test_flag_off_preserves_openai_primary_order(self):
        order, result = self._run(flag=False, local_result=self._WORDS, openai_result=None)
        self.assertEqual(order, ["openai", "local"])
        self.assertEqual(result, self._WORDS)

    def test_flag_on_both_engines_failing_returns_empty(self):
        order, result = self._run(flag=True, local_result=None, openai_result=None)
        self.assertEqual(order, ["local", "openai"])
        self.assertEqual(result, [])

    def test_default_flag_is_off(self):
        from app.config import Settings
        self.assertFalse(Settings(_env_file=None).whisper_local_primary)


# ── R4: shared parent source-script service ──────────────────────────────────

class TestGenerateParentSourceScriptShared(unittest.TestCase):
    def _fixtures(self):
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
            source_url="https://example.com/x",
            # >=900 words: clears the youtube_long source-material floor
            # (roadmap 4b / audit P1-5, check_source_material_floor()) — this
            # fixture is testing shared persistence/versioning, not the floor.
            source_excerpt=" ".join(["word"] * 950),
        )
        db.add(content)
        return db, content

    def test_generates_persists_and_returns_source_script(self):
        db, content = self._fixtures()
        captured = {}

        def fake_blueprint(story, channel, **kwargs):
            captured["blueprint_kwargs"] = kwargs
            return {"major_turns": ["t1", "t2"], "suggested_section_count": 2,
                    "hook": "h", "final_payoff": "p", "comment_trigger": "c?",
                    "midpoint_retention_trap": "m", "central_question": "q",
                    "suggested_title": "T"}

        def fake_sections(**kwargs):
            captured["sections_kwargs"] = kwargs
            return {"title": "T", "voice_script": "[INTRO]\nGenerated narration.",
                    "visual_intent_history": []}

        def fake_gate(scripts, **kwargs):
            return scripts

        with (
            patch.object(script_workflow, "generate_story_blueprint", side_effect=fake_blueprint),
            patch.object(script_workflow, "generate_script_sections", side_effect=fake_sections),
            patch.object(script_workflow, "run_script_quality_gate", side_effect=fake_gate),
        ):
            voice_script = script_workflow.generate_parent_source_script(content, db)

        self.assertEqual(voice_script, "[INTRO]\nGenerated narration.")
        # Channel visual/image style really reached both Claude boundaries.
        self.assertEqual(captured["blueprint_kwargs"]["visual_style"], "documentary")
        self.assertEqual(captured["sections_kwargs"]["image_style"], "photorealistic")
        # A validated source Script row was persisted via _persist_source_script.
        rows = [s for s in db.tables[Script] if s.language == "en"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].validated)
        self.assertEqual(rows[0].voice_script, "[INTRO]\nGenerated narration.")
        self.assertEqual(content.status, "GENERATING_SCRIPTS")

    def test_run_script_workflow_delegates_to_the_shared_service(self):
        """Static-plus-runtime wiring proof: run_script_workflow() calls
        generate_parent_source_script() rather than re-implementing it."""
        import inspect
        src = inspect.getsource(script_workflow.run_script_workflow)
        self.assertIn("generate_parent_source_script(", src)
        # And the removed inline sequence is gone from run_script_workflow.
        self.assertNotIn("generate_story_blueprint(", src)
        self.assertNotIn("generate_script_sections(", src)

    def test_harness_step_delegates_to_the_shared_service(self):
        harness_src = open("test_pipeline/test_full_pipeline.py").read()
        self.assertIn("generate_parent_source_script", harness_src)
        # The harness no longer re-implements the generation sequence inline.
        self.assertNotIn("generate_story_blueprint(", harness_src)
        self.assertNotIn("run_script_quality_gate(", harness_src)


if __name__ == "__main__":
    unittest.main()
