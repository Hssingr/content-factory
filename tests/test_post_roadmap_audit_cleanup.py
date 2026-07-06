"""Runtime proofs for the post-roadmap deep audit cleanup
(code_report/deep_audit_post_roadmap_verification.md).

Covers, with only paid Claude boundaries stubbed:

1. Translation single-call: `_generate_validated_translated_parent_script()`
   and `_generate_validated_translated_short_script()` make exactly ONE
   `generate_native_script()` call each — MAJOR deterministic findings are
   telemetry only and never trigger a corrective re-generation (the former
   retry loops were the exact full-regeneration + override pattern audit
   P1-6 measured making drafts worse across rounds).
2. output_mode wiring: `run_script_workflow()` skips `run_shorts_planner()`
   entirely for `ChannelConfig.output_mode="youtube_long_only"` (logged
   SHORTS_PLANNER_SKIPPED) and still runs it for "youtube_and_shorts" —
   the first real runtime consumer of `output_mode`.
3. V3 rules + activation: `youtube_long_only` is now executable and a
   channel configured with it (plus a verified YouTube platform) activates.
4. Eliminated surfaces stay eliminated: Celery compat shims, the
   auto_correct_script prompt-repair layer, the translation retry-round
   constants, the storyboard override_instructions retry channel, and
   image_router's dead dataclasses/purpose branch.
"""

from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import script_workflow
from app.agents.agent2_discovery.services import scripts as agent2_scripts
from app.models import Channel, ChannelConfig, ChannelVoice, Content


# ── Shared fake DB (same shape as test_roadmap_closeout_fixes.py) ────────────

class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

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
            if getattr(row, "id", None) == key or getattr(row, "channel_id", None) == key:
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


_CHANNEL = SimpleNamespace(niche="horror", tone="suspenseful")

_SOURCE_SCRIPT = (
    "[INTRO]\nA sound echoes from the mountain every night.\n"
    "[SECTION 1]\nSam finds the first clue near the porch and follows it uphill.\n"
    "[OUTRO]\nNobody in town talks about what he found up there. Would you have looked?\n"
)


class TestTranslatedParentScriptSingleCall(unittest.TestCase):
    """Real `_collect_translated_parent_script_issues()` runs unmocked — only
    the paid `generate_native_script()` boundary is stubbed."""

    def _run(self, translated_text: str):
        calls = {"n": 0}

        def fake_native(**kwargs):
            calls["n"] += 1
            self.assertNotIn("override_instruction", kwargs)
            return {"voice_script": translated_text}

        with patch.object(agent2_scripts, "generate_native_script", side_effect=fake_native):
            adapted = agent2_scripts._generate_validated_translated_parent_script(
                source_voice_script=_SOURCE_SCRIPT,
                target_language="fr",
                channel=_CHANNEL,
                script_format="youtube_long",
                audio_tags_enabled=False,
                tts_model="sonic-3.5",
                tts_provider="cartesia",
                hook_context=None,
                content_id=uuid.uuid4(),
            )
        return adapted, calls["n"]

    def test_clean_translation_returns_after_one_call(self):
        clean = _SOURCE_SCRIPT.replace("Sam", "Samuel")
        adapted, n_calls = self._run(clean)
        self.assertEqual(n_calls, 1)
        self.assertEqual(adapted["voice_script"], clean)

    def test_major_issues_are_telemetry_only_no_second_call(self):
        # Section loss: source has 1 [SECTION N]; translation has none — a
        # genuine MAJOR the old loop would have burned 2 corrective calls on.
        broken = "[INTRO]\nUne seule ligne.\n[OUTRO]\nFin.\n"
        with self.assertLogs(
            "app.agents.agent2_discovery.services.scripts", level="WARNING"
        ) as log_ctx:
            adapted, n_calls = self._run(broken)

        self.assertEqual(n_calls, 1)
        self.assertEqual(adapted["voice_script"], broken)  # latest draft still used
        joined = " ".join(log_ctx.output)
        self.assertIn("PARENT_TRANSLATION_VALIDATION_ISSUES", joined)
        self.assertIn("telemetry only, no retry", joined)

    def test_generation_exception_returns_none(self):
        def raising_native(**kwargs):
            raise RuntimeError("stubbed transport failure")

        with patch.object(agent2_scripts, "generate_native_script", side_effect=raising_native):
            adapted = agent2_scripts._generate_validated_translated_parent_script(
                source_voice_script=_SOURCE_SCRIPT,
                target_language="fr",
                channel=_CHANNEL,
                script_format="youtube_long",
                audio_tags_enabled=False,
                tts_model="sonic-3.5",
                tts_provider="cartesia",
                hook_context=None,
                content_id=uuid.uuid4(),
            )
        self.assertIsNone(adapted)


class TestTranslatedShortScriptSingleCall(unittest.TestCase):
    def test_major_issues_are_telemetry_only_no_second_call(self):
        source = " ".join(["word"] * 150)  # in-band source Short
        too_short = "Trop court."  # fails the calibrated floor — MAJOR
        calls = {"n": 0}

        def fake_native(**kwargs):
            calls["n"] += 1
            return {"voice_script": too_short}

        with (
            patch.object(agent2_scripts, "generate_native_script", side_effect=fake_native),
            self.assertLogs(
                "app.agents.agent2_discovery.services.scripts", level="WARNING"
            ) as log_ctx,
        ):
            adapted = agent2_scripts._generate_validated_translated_short_script(
                source_voice_script=source,
                target_language="fr",
                channel=_CHANNEL,
                script_format="youtube_long",
                audio_tags_enabled=False,
                tts_model="sonic-3.5",
                tts_provider="cartesia",
                hook_context=None,
                content_id=uuid.uuid4(),
            )

        self.assertEqual(calls["n"], 1)
        self.assertEqual(adapted["voice_script"], too_short)
        joined = " ".join(log_ctx.output)
        self.assertIn("CHILD_SHORT_TRANSLATION_ISSUES", joined)
        self.assertIn("telemetry only, no retry", joined)


class TestOutputModeLongOnlySkipsShortsPlanner(unittest.TestCase):
    """Wiring proof for the first real runtime consumer of
    ChannelConfig.output_mode: the real `run_script_workflow()` orchestration
    runs; its paid inner stages (source-script generation, multilingual
    generation, shorts planning — each proven separately elsewhere) are
    replaced with recorders at their call boundary."""

    def _fixtures(self, output_mode: str):
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db = _FakeDb()
        db.add(Channel(id=channel_id, niche="horror", tone="suspenseful"))
        db.add(ChannelConfig(
            channel_id=channel_id, script_format="youtube_long",
            output_mode=output_mode, audio_tags_enabled=False,
            visual_style="story_driven", image_style="photorealistic",
        ))
        db.add(ChannelVoice(
            id=uuid.uuid4(), channel_id=channel_id, language="en",
            provider="cartesia", voice_id="v1", tts_model="sonic-3.5",
        ))
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="APPROVED", title="T",
            source_url="https://example.com/x",
            source_excerpt=" ".join(["word"] * 950),
        )
        db.add(content)
        return db, content

    def _run(self, output_mode: str):
        db, content = self._fixtures(output_mode)
        planner_calls: list = []

        with (
            patch.object(script_workflow, "ensure_run_dirs", return_value={}),
            patch.object(script_workflow, "generate_parent_source_script",
                         return_value="[INTRO]\nHook line.\n[OUTRO]\nDone.\n"),
            patch.object(script_workflow, "generate_multilingual_scripts",
                         return_value={"en": {"voice_script": "x"}}),
            patch.object(script_workflow, "run_shorts_planner",
                         side_effect=lambda *a, **k: planner_calls.append(a)),
        ):
            script_workflow.run_script_workflow(content, db)

        return content, planner_calls

    def test_youtube_long_only_skips_planner_and_logs(self):
        with self.assertLogs(
            "app.agents.agent2_discovery.services.script_workflow", level="INFO"
        ) as log_ctx:
            content, planner_calls = self._run("youtube_long_only")

        self.assertEqual(planner_calls, [])
        self.assertEqual(content.status, "SCRIPTS_VALIDATED")
        self.assertIn("SHORTS_PLANNER_SKIPPED", " ".join(log_ctx.output))

    def test_youtube_and_shorts_still_runs_planner(self):
        content, planner_calls = self._run("youtube_and_shorts")
        self.assertEqual(len(planner_calls), 1)
        self.assertEqual(content.status, "SCRIPTS_VALIDATED")


class TestV3OutputModeExecutability(unittest.TestCase):
    def test_youtube_long_only_is_now_executable(self):
        from app.agents.agent1_setup.services.v3_config_rules import (
            is_executable_output_mode, validate_v3_channel_config,
        )
        self.assertTrue(is_executable_output_mode("youtube_and_shorts"))
        self.assertTrue(is_executable_output_mode("youtube_long_only"))
        self.assertFalse(is_executable_output_mode("shorts_only"))

        result = validate_v3_channel_config({
            "content_mode": "single_story",
            "script_source": "reddit",
            "output_mode": "youtube_long_only",
        })
        self.assertTrue(result["executable"])
        self.assertEqual(result["issues"], [])

    def test_activation_ready_with_long_only_and_youtube_platform(self):
        from app.agents.agent1_setup.services.activation_readiness import (
            check_activation_readiness,
        )
        config = SimpleNamespace(
            content_mode="single_story", script_source="reddit",
            output_mode="youtube_long_only",
        )
        channel = SimpleNamespace(
            niche="cooking", tone="conversational", config=config,
            languages=[SimpleNamespace(language="en")],
            voices=[SimpleNamespace(language="en")],
            sources=[SimpleNamespace()],
            publish_timings=[SimpleNamespace()],
            platforms=[SimpleNamespace(platform="youtube", language="en", verified=True)],
        )
        result = check_activation_readiness(channel)
        self.assertTrue(result["ready"], result["issues"])

    def test_activation_blocked_for_long_only_without_youtube_platform(self):
        from app.agents.agent1_setup.services.activation_readiness import (
            check_activation_readiness,
        )
        config = SimpleNamespace(
            content_mode="single_story", script_source="reddit",
            output_mode="youtube_long_only",
        )
        channel = SimpleNamespace(
            niche="cooking", tone="conversational", config=config,
            languages=[SimpleNamespace(language="en")],
            voices=[SimpleNamespace(language="en")],
            sources=[SimpleNamespace()],
            publish_timings=[SimpleNamespace()],
            platforms=[SimpleNamespace(platform="tiktok", language="en", verified=True)],
        )
        result = check_activation_readiness(channel)
        self.assertFalse(result["ready"])
        codes = {i["code"] for i in result["issues"]}
        self.assertIn("youtube_required_for_output_mode", codes)


class TestEliminatedSurfacesStayEliminated(unittest.TestCase):
    def test_celery_compat_shims_removed(self):
        from app.scheduler import tasks
        for name in (
            "pickup_short_episodes_awaiting_parent",
            "ensure_child_short_audio_enqueued",
            "run_agent4_for_content",
            "run_agent5_for_content",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(tasks, name))

    def test_auto_correct_prompt_repair_layer_removed(self):
        from app.agents.agent2_discovery import system_prompt
        for name in (
            "auto_correct_script",
            "_CORRECTION_SYSTEM_PROMPT_BASE",
            "_split_long_sentences_agent2",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(system_prompt, name))

    def test_translation_retry_round_constants_removed(self):
        for name in (
            "_MAX_PARENT_TRANSLATION_CORRECTION_ROUNDS",
            "_MAX_SHORT_CORRECTION_ROUNDS",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(agent2_scripts, name))

    def test_storyboard_override_retry_channel_removed(self):
        import inspect
        from app.agents.agent4_visuals import system_prompt as agent4_prompt
        from app.agents.agent4_visuals.subagents import storyboard

        sig = inspect.signature(agent4_prompt.generate_storyboard_batch)
        self.assertNotIn("override_instructions", sig.parameters)
        sig2 = inspect.signature(storyboard.split_into_beats)
        self.assertNotIn("storyboard_constraints", sig2.parameters)
        self.assertNotIn("RETRY REQUIRED", inspect.getsource(agent4_prompt))

    def test_image_router_dead_surfaces_removed(self):
        import inspect
        from app.agents.agent4_visuals.services import image_router

        self.assertFalse(hasattr(image_router, "ImageRequest"))
        self.assertFalse(hasattr(image_router, "ImageResult"))
        self.assertTrue(hasattr(image_router, "ImageRoute"))  # still the return type
        sig = inspect.signature(image_router.select_route)
        self.assertNotIn("purpose", sig.parameters)

    def test_section_and_native_generation_lost_override_instruction(self):
        import inspect
        from app.agents.agent2_discovery import system_prompt

        for fn in (
            system_prompt.generate_section,
            system_prompt.generate_native_script,
            system_prompt.generate_short_episode_script,
        ):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("override_instruction", inspect.signature(fn).parameters)


_DROPPED_CONFIG_COLUMNS = (
    "shorts_rule", "subtitle_style_main", "subtitle_style_shorts",
    "shorts_part_label_style", "runway_enabled", "strict_quality_gate",
    "video_style_type", "video_color_grade",
)


class TestChannelConfigDeadColumnsDropped(unittest.TestCase):
    """channel_config cleanup: eight columns with no live runtime reader are
    dropped by migration 009, removed from the ORM model and Pydantic
    schemas, and their dead Agent 5 consumer chain (channel_style /
    channel_color_grade → props "config" key that no Remotion component ever
    read) is removed end to end."""

    def test_model_no_longer_maps_dropped_columns(self):
        for col in _DROPPED_CONFIG_COLUMNS:
            with self.subTest(col=col):
                self.assertFalse(hasattr(ChannelConfig, col))

    def test_model_keeps_live_columns(self):
        for col in (
            "videos_per_week", "validation_timeout_hours",
            "validation_max_revisions", "validation_on_limit_reached",
            "subtitle_karaoke_active_color", "script_format",
            "allow_legacy_fallback", "audio_tags_enabled", "content_mode",
            "script_source", "output_mode", "visual_style", "image_style",
            "narration_pov",
        ):
            with self.subTest(col=col):
                self.assertTrue(hasattr(ChannelConfig, col))

    def test_schemas_no_longer_carry_dropped_columns(self):
        from app.schemas.channel import ChannelConfigUpsert, ChannelConfigResponse
        for schema in (ChannelConfigUpsert, ChannelConfigResponse):
            for col in _DROPPED_CONFIG_COLUMNS:
                with self.subTest(schema=schema.__name__, col=col):
                    self.assertNotIn(col, schema.model_fields)

    def test_props_builders_emit_no_config_key(self):
        """Runtime proof: the real build_main_props()/build_short_props()
        write props JSON without the dead "config" key (no Remotion
        component ever read it)."""
        import json
        import tempfile
        from unittest.mock import patch as mock_patch
        from app.agents.agent5_render.services import remotion_builder

        section = {"audio_start_ms": 0, "audio_end_ms": 9000, "media_url": "cache/x/a.jpg"}
        caption = {"start_ms": 0, "end_ms": 9000, "text": "hello"}
        with tempfile.TemporaryDirectory() as tmp:
            with mock_patch.object(remotion_builder.settings, "media_path", tmp):
                main_path = remotion_builder.build_main_props(
                    content_id="c1", language="en", audio_file_path=f"{tmp}/a.mp3",
                    duration_ms=9000, sections=[section],
                    standard_subtitles=[caption], karaoke_subtitles=[caption],
                )
                short_path = remotion_builder.build_short_props(
                    content_id="c1", language="en", audio_file_path=f"{tmp}/a.mp3",
                    short={"short_index": 0, "start_ms": 0, "end_ms": 9000,
                           "sections": [section]},
                    karaoke_subtitles=[caption],
                )
            for path in (main_path, short_path):
                with self.subTest(path=path):
                    props = json.loads(open(path).read())
                    self.assertNotIn("config", props)
                    self.assertIn("sections", props)

    def test_migration_009_drops_exactly_the_dead_columns(self):
        import importlib.util
        from pathlib import Path as _P
        path = _P("alembic/versions/009_drop_dead_channel_config_columns.py")
        spec = importlib.util.spec_from_file_location("migration_009", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        self.assertEqual(mod.revision, "009")
        self.assertEqual(mod.down_revision, "008")
        self.assertEqual(set(mod._DROPPED), set(_DROPPED_CONFIG_COLUMNS))

        dropped = []

        class _FakeOp:
            @staticmethod
            def drop_column(table, column):
                dropped.append((table, column))

        with patch.object(mod, "op", _FakeOp):
            mod.upgrade()
        self.assertEqual(
            {c for _, c in dropped}, set(_DROPPED_CONFIG_COLUMNS),
        )
        self.assertTrue(all(t == "channel_config" for t, _ in dropped))


if __name__ == "__main__":
    unittest.main()
