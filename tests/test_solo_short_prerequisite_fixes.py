"""Runtime proofs for the prerequisite fixes ahead of Solo Short support
(code_report/output_mode_shorts_only_and_youtube_long_only_roadmap.md, Phase C).

Three independent latent bugs the roadmap's research surfaced, all fixed here
with only the paid/expensive boundaries stubbed:

1. Finding A: three call sites hardcoded `is_short_episode=False`, correct
   only because their one caller was always a long-form parent. They now
   thread the real content shape through, so a Solo Short reaching this
   same code gets short-form wpm calibration instead of silently reusing
   the long-form window.
2. Finding C: `run_visual_generation()`'s dispatch used to route ANY
   `is_short_episode=True` row into `_run_child_short_visuals()`, which
   hard-fails (`VISUALS_FAILED`) the instant it finds no `parent_content_id`
   — a parentless short is now routed to `_run_solo_short_visuals()`
   instead, a new function that (this phase) raises `NotImplementedError`
   as a deliberate placeholder — proving the ROUTING decision independently
   of the real implementation, which a later phase supplies.
3. Finding B: `run_shorts_planner()` now refuses to act on a content row
   that is itself already a short, as a second, independent line of
   defense beyond `run_script_workflow()`'s own dispatch never reaching it.
"""

from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import script_workflow
from app.agents.agent2_discovery.services import scripts as agent2_scripts
from app.agents.agent4_visuals.services import visual_orchestrator
from app.agents.agent4_visuals.subagents import storyboard
from app.models import Channel, ChannelConfig, Content, Script


# ── Shared fake DB (same shape as test_post_roadmap_audit_cleanup.py) ────────

class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        return _FakeQuery(self.rows[:n])

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def count(self):
        # Always "no existing children" — this fake never models a real
        # parent-to-child relationship (filter() ignores its conditions
        # entirely, same as elsewhere in this file), and no test in this
        # file ever adds a genuine child Content row. Presence/absence of
        # existing children is _child_shorts_already_exist()'s own concern,
        # covered by its own tests elsewhere — not what these tests target.
        return 0


class _FakeDb:
    def __init__(self):
        self.tables: dict = {}

    def get(self, model, key):
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key or getattr(row, "channel_id", None) == key:
                return row
        return None

    def query(self, model):
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def flush(self):
        pass

    def refresh(self, row):
        pass

    def commit(self):
        pass


def _content(**overrides) -> Content:
    defaults = dict(
        id=uuid.uuid4(), channel_id=uuid.uuid4(), is_short_episode=False,
        parent_content_id=None, source_language="en", status="APPROVED",
        title="T", source_url="https://example.com/x",
    )
    defaults.update(overrides)
    return Content(**defaults)


class TestFindingAWpmCalibrationScoping(unittest.TestCase):
    """The three call sites that used to hardcode is_short_episode=False now
    thread the real content shape through."""

    def test_persist_source_script_passes_real_is_short_episode(self):
        for shape in (False, True):
            with self.subTest(is_short_episode=shape):
                content = _content(is_short_episode=shape, status="GENERATING_SCRIPTS")
                db = _FakeDb()
                calls: list = []

                def fake_estimate(*a, **k):
                    calls.append(k.get("is_short_episode"))
                    return 42.0

                with patch.object(script_workflow, "estimate_duration_sec", side_effect=fake_estimate):
                    script_workflow._persist_source_script(
                        content, {"title": "T", "voice_script": "Hello there."}, db,
                    )
                self.assertEqual(calls, [shape])

    def test_set_multilingual_durations_passes_real_is_short_episode(self):
        for shape in (False, True):
            with self.subTest(is_short_episode=shape):
                content_id = uuid.uuid4()
                content = _content(id=content_id, is_short_episode=shape, status="SCRIPTS_VALIDATED")
                db = _FakeDb()
                db.add(content)
                db.add(Script(
                    id=uuid.uuid4(), content_id=content_id, language="fr",
                    voice_script="Bonjour tout le monde.", version=1, validated=False,
                ))
                calls: list = []

                def fake_estimate(*a, **k):
                    calls.append(k.get("is_short_episode"))
                    return 42.0

                with patch.object(script_workflow, "estimate_duration_sec", side_effect=fake_estimate):
                    script_workflow._set_multilingual_durations(content, db)
                self.assertEqual(calls, [shape])

    def test_split_into_beats_passes_real_is_short_episode_to_wpm_calibration(self):
        for shape in (False, True):
            with self.subTest(is_short_episode=shape):
                calls: list = []

                def fake_calibrated_wpm(db, language, is_short_episode=None, channel_id=None):
                    calls.append(is_short_episode)
                    return 130.0

                with (
                    patch.object(storyboard, "get_calibrated_wpm", side_effect=fake_calibrated_wpm),
                    # Stop execution the instant the code under test has run —
                    # _estimate_beat_count() is the very next call after the
                    # diagnostic_wpm computation this test targets, so raising
                    # here means nothing downstream (the real, paid storyboard
                    # generation loop) needs to be satisfied at all.
                    patch.object(storyboard, "_estimate_beat_count",
                                 side_effect=RuntimeError("stop — only testing the wpm call above")),
                ):
                    with self.assertRaises(RuntimeError):
                        storyboard.split_into_beats(
                            voice_script="[INTRO]\nHello there.\n[OUTRO]\nGoodbye.\n",
                            duration_ms=9000,
                            channel=SimpleNamespace(niche="horror", tone="suspenseful"),
                            script_format="youtube_long",
                            whisper_transcript=[{"word": "hi", "start": 0.0, "end": 0.5}],
                            language="en",
                            content_id="cid",
                            db=object(),
                            is_short_episode=shape,
                        )
                self.assertEqual(calls, [shape])


class TestFindingCVisualDispatchStub(unittest.TestCase):
    """`run_visual_generation()`'s 3-way dispatch. Only the branch under test
    is exercised for real in each case; the other two branches' own real
    functions are stubbed so this stays a routing proof, not a re-proof of
    their (separately covered) internal behavior."""

    def test_solo_short_routes_to_solo_short_path_not_child_remap(self):
        # _run_solo_short_visuals() was a raise-only NotImplementedError stub
        # when this test was first written (Phase C, dispatch-only proof);
        # Phase D replaced it with a real implementation (covered by
        # tests/test_solo_short_pipeline.py) — this test now proves the same
        # routing fact the same way the sibling child-of-parent test below
        # does: stub the target function and assert the dispatch reaches it,
        # never _run_child_short_visuals().
        content = _content(is_short_episode=True, parent_content_id=None,
                            status="GENERATING_VISUALS")
        channel = Channel(id=content.channel_id, niche="horror", tone="suspenseful")
        db = _FakeDb()
        calls: list = []

        def fake_solo_short_visuals(*a, **k):
            calls.append(True)
            return {"status": "CHILD_SHORT_VISUALS_DONE", "beats_by_lang": {}}

        with (
            patch.object(visual_orchestrator, "_run_solo_short_visuals", side_effect=fake_solo_short_visuals),
            patch.object(visual_orchestrator, "_run_child_short_visuals",
                         side_effect=AssertionError("must not route a parentless short here")),
        ):
            result = visual_orchestrator.run_visual_generation(
                content=content, channel=channel,
                scripts_by_lang={}, audio_by_lang={},
                script_format="youtube_long", allow_legacy_fallback=False, db=db,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "CHILD_SHORT_VISUALS_DONE")

    def test_child_of_parent_short_still_routes_to_remap_path_unchanged(self):
        parent_id = uuid.uuid4()
        content = _content(is_short_episode=True, parent_content_id=parent_id,
                            status="GENERATING_VISUALS")
        channel = Channel(id=content.channel_id, niche="horror", tone="suspenseful")
        db = _FakeDb()
        calls: list = []

        def fake_child_visuals(*a, **k):
            calls.append(True)
            return {"status": "CHILD_SHORT_VISUALS_DEFERRED", "beats_by_lang": {}}

        with patch.object(visual_orchestrator, "_run_child_short_visuals", side_effect=fake_child_visuals):
            result = visual_orchestrator.run_visual_generation(
                content=content, channel=channel,
                scripts_by_lang={}, audio_by_lang={},
                script_format="youtube_long", allow_legacy_fallback=False, db=db,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "CHILD_SHORT_VISUALS_DEFERRED")

    def test_parent_still_routes_to_parent_path_unchanged(self):
        content = _content(is_short_episode=False, parent_content_id=None,
                            status="GENERATING_VISUALS")
        channel = Channel(id=content.channel_id, niche="horror", tone="suspenseful")
        db = _FakeDb()

        with patch.object(
            visual_orchestrator, "_run_parent_visuals",
            return_value={"status": "PARENT_VISUALS_DONE", "beats_by_lang": {}},
        ) as mock_parent:
            result = visual_orchestrator.run_visual_generation(
                content=content, channel=channel,
                scripts_by_lang={}, audio_by_lang={},
                script_format="youtube_long", allow_legacy_fallback=False, db=db,
            )
        mock_parent.assert_called_once()
        self.assertEqual(result["status"], "PARENT_VISUALS_DONE")


class TestFindingBShortsPlannerGuard(unittest.TestCase):
    """`run_shorts_planner()` refuses to act on content that is itself
    already a short, independent of whether its caller would ever route it
    there — a second, direct-invocation-level line of defense."""

    def test_run_shorts_planner_refuses_to_act_on_a_short_row(self):
        content_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content = _content(id=content_id, channel_id=channel_id,
                            is_short_episode=True, parent_content_id=None,
                            status="SCRIPTS_VALIDATED")
        db = _FakeDb()
        db.add(content)
        db.add(Script(
            id=uuid.uuid4(), content_id=content_id, language="en",
            voice_script="Hello world, this is a short.", version=1, validated=True,
        ))
        channel = Channel(id=channel_id, niche="horror", tone="suspenseful")
        config = ChannelConfig(channel_id=channel_id)

        with (
            patch.object(agent2_scripts, "_generate_shorts_plan_with_retry",
                         side_effect=AssertionError("must not be called for a short row")),
            patch.object(agent2_scripts, "_child_shorts_already_exist",
                         side_effect=AssertionError("must not even check for existing children")),
            self.assertLogs(
                "app.agents.agent2_discovery.services.scripts", level="INFO"
            ) as log_ctx,
        ):
            agent2_scripts.run_shorts_planner(content_id, channel, config, db)

        joined = " ".join(log_ctx.output)
        self.assertIn("SHORTS_PLANNER_SKIPPED", joined)
        self.assertIn("content_is_already_a_short", joined)

        all_content = db.tables.get(Content, [])
        self.assertEqual(len(all_content), 1)
        self.assertIs(all_content[0], content)

    def test_run_shorts_planner_unaffected_for_a_real_parent(self):
        # Regression: a genuine long-form parent must still reach the real
        # planning call — the guard must not over-fire.
        content_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content = _content(id=content_id, channel_id=channel_id,
                            is_short_episode=False, status="SCRIPTS_VALIDATED")
        db = _FakeDb()
        db.add(content)
        db.add(Script(
            id=uuid.uuid4(), content_id=content_id, language="en",
            voice_script="Hello world, this is a long-form script.",
            version=1, validated=True,
        ))
        channel = Channel(id=channel_id, niche="horror", tone="suspenseful")
        config = ChannelConfig(channel_id=channel_id)
        calls: list = []

        with patch.object(agent2_scripts, "_generate_shorts_plan_with_retry",
                           side_effect=lambda *a, **k: calls.append(True) or None):
            agent2_scripts.run_shorts_planner(content_id, channel, config, db)

        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
