"""Runtime proof: test_pipeline/test_full_pipeline.py correctly handles a
Solo Short (``output_mode="shorts_only"`` — a short episode with no parent
at all: ``is_short_episode=True``, ``parent_content_id=None``,
``short_part_number=1``, ``short_total_parts=1``. See CLAUDE.md
§4.4/§5.3/§9.6/§11.4).

Covers three real bugs found and fixed in this harness:
  1. ``_run_step_scripts()`` called ``generate_parent_source_script()`` (the
     long-form blueprint->sections->quality-gate pipeline, with no
     Solo-Short awareness at all) unconditionally — wrong for a Solo Short,
     which needs ``run_script_workflow()``'s own dispatch to
     ``_run_solo_short_script_workflow()`` instead. Calling the wrong
     function would apply the channel's long-form 900-word source-material
     floor (instead of the Solo Short's ~420-word one) and, if that somehow
     cleared, produce a long-form-shaped script on a row Agent 5 still
     renders as a vertical Short.
  2. The `parent = content if not content.is_short_episode else
     db.get(Content, content.parent_content_id)` pattern (5 call sites,
     since consolidated into ``_resolve_parent_and_children()``) crashed
     for a Solo Short: ``db.get(Content, None)`` resolves to ``None``
     rather than raising (a `WHERE id = NULL` query matches nothing), and
     every caller dereferenced that ``None`` a few lines later (e.g. inside
     ``_label()``).
  3. ``_label()``/``_summary_row()`` mislabeled a Solo Short as
     "child part 1/1" — architecturally wrong, since it has no parent at
     all to be a child of.

Only the paid boundaries are stubbed (``run_script_workflow``,
``generate_parent_source_script``, ``generate_multilingual_scripts``,
``run_shorts_planner``) — every harness function under test runs
unmodified against a fake DB (CLAUDE.md §19.4).

No live external API calls anywhere in this file (CLAUDE.md §19.1).
"""

from __future__ import annotations

import operator as _operator_module
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.sql.elements import False_, True_, UnaryExpression
from sqlalchemy.sql.operators import desc_op, is_, isnot

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "test_pipeline") not in sys.path:
    sys.path.insert(0, str(ROOT / "test_pipeline"))

import test_full_pipeline as pipeline
from app.agents.agent2_discovery.services import script_workflow as script_workflow_mod
from app.agents.agent2_discovery.services import scripts as scripts_mod
from app.models import Channel, ChannelLanguage, Content, Script


# ── Fake DB — REAL filtering (not this repo's usual no-op-filter fake),
# needed here because several tests seed more than one Content row in the
# same table and rely on the query actually distinguishing between them
# (e.g. _existing_children()'s parent_content_id filter must not return the
# parent's own row). Generalized to dispatch on the real SQLAlchemy
# comparison operator so it also supports `.is_(True)` (`ChannelPlatform`-
# style boolean columns) alongside plain equality — same pattern already
# established in tests/test_agent6_scheduler_and_approval.py.

def _extract_value(node):
    if isinstance(node, True_):
        return True
    if isinstance(node, False_):
        return False
    return node.value


class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *conditions):
        rows = self.rows
        for cond in conditions:
            key = cond.left.key
            value = _extract_value(cond.right)
            op = cond.operator
            if op is is_:
                op = _operator_module.eq
            elif op is isnot:
                op = _operator_module.ne
            rows = [r for r in rows if op(getattr(r, key, None), value)]
        return _FakeQuery(rows)

    def order_by(self, *columns):
        rows = list(self.rows)
        # Stable-sort applied in reverse column order so the first argument
        # ends up primary — supports both plain columns (Content.short_
        # part_number) and Script.version.desc()-style descending ones.
        for column in reversed(columns):
            if isinstance(column, UnaryExpression) and column.modifier is desc_op:
                key_col = column.element
                rows = sorted(rows, key=lambda r, c=key_col: getattr(r, c.key), reverse=True)
            else:
                rows = sorted(rows, key=lambda r, c=column: getattr(r, c.key) or 0)
        return _FakeQuery(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self):
        self.tables: dict = {}

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def get(self, model, key):
        if key is None:
            return None
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key:
                return row
        return None

    def query(self, model):
        return _FakeQuery(self.tables.get(model, []))

    def commit(self):
        pass

    def refresh(self, _obj):
        pass

    def close(self):
        pass


def _solo_short_content(channel_id, **overrides) -> Content:
    defaults = dict(
        id=uuid.uuid4(), channel_id=channel_id, source_language="en",
        title="A Solo Short", source_url="https://x", status="APPROVED",
        is_short_episode=True, parent_content_id=None,
        short_part_number=1, short_total_parts=1,
    )
    defaults.update(overrides)
    return Content(**defaults)


def _parent_content(channel_id, **overrides) -> Content:
    defaults = dict(
        id=uuid.uuid4(), channel_id=channel_id, source_language="en",
        title="A Parent", source_url="https://x", status="APPROVED",
        is_short_episode=False, parent_content_id=None,
    )
    defaults.update(overrides)
    return Content(**defaults)


def _child_of_parent_content(channel_id, parent_id, **overrides) -> Content:
    defaults = dict(
        id=uuid.uuid4(), channel_id=channel_id, source_language="en",
        title="A Child Short", source_url="https://x", status="APPROVED",
        is_short_episode=True, parent_content_id=parent_id,
        short_part_number=1, short_total_parts=2,
    )
    defaults.update(overrides)
    return Content(**defaults)


# ── Bug 1: _run_step_scripts() dispatch ─────────────────────────────────────

class RunStepScriptsSoloShortTest(unittest.TestCase):
    def test_solo_short_routes_to_run_script_workflow_not_generate_parent_source_script(self):
        channel_id = uuid.uuid4()
        channel = Channel(id=channel_id, user_id=uuid.uuid4(), name="C", niche="n", tone="t")
        content = _solo_short_content(channel_id)
        db = _FakeDb()
        db.add(channel)
        db.add(content)

        def _fake_run_script_workflow(c, _db):
            c.status = "SCRIPTS_VALIDATED"

        with (
            patch.object(
                script_workflow_mod, "run_script_workflow", side_effect=_fake_run_script_workflow,
            ) as workflow_stub,
            patch.object(script_workflow_mod, "generate_parent_source_script") as parent_stub,
        ):
            result = pipeline._run_step_scripts(content, channel, None, db)

        self.assertTrue(result)
        workflow_stub.assert_called_once()
        parent_stub.assert_not_called()
        self.assertEqual(content.status, "SCRIPTS_VALIDATED")

    def test_solo_short_generation_failure_returns_false_without_crashing(self):
        channel_id = uuid.uuid4()
        channel = Channel(id=channel_id, user_id=uuid.uuid4(), name="C", niche="n", tone="t")
        content = _solo_short_content(channel_id)
        db = _FakeDb()
        db.add(channel)
        db.add(content)

        def _fake_run_script_workflow(c, _db):
            c.status = "FAILED"  # e.g. source-material floor failed

        with patch.object(
            script_workflow_mod, "run_script_workflow", side_effect=_fake_run_script_workflow,
        ):
            result = pipeline._run_step_scripts(content, channel, None, db)

        self.assertFalse(result)
        self.assertEqual(content.status, "FAILED")

    def test_solo_short_with_already_validated_script_is_reused(self):
        channel_id = uuid.uuid4()
        channel = Channel(id=channel_id, user_id=uuid.uuid4(), name="C", niche="n", tone="t")
        content = _solo_short_content(channel_id, status="SCRIPTS_VALIDATED")
        db = _FakeDb()
        db.add(channel)
        db.add(content)
        db.add(Script(content_id=content.id, language="en", voice_script="x", validated=True))

        with (
            patch.object(script_workflow_mod, "run_script_workflow") as workflow_stub,
            patch.object(script_workflow_mod, "generate_parent_source_script") as parent_stub,
        ):
            result = pipeline._run_step_scripts(content, channel, None, db)

        self.assertTrue(result)
        workflow_stub.assert_not_called()
        parent_stub.assert_not_called()

    def test_parent_content_still_routes_to_generate_parent_source_script(self):
        """Regression: the unmodified parent path must be unaffected."""
        channel_id = uuid.uuid4()
        channel = Channel(id=channel_id, user_id=uuid.uuid4(), name="C", niche="n", tone="t")
        content = _parent_content(channel_id)
        db = _FakeDb()
        db.add(channel)
        db.add(content)

        with (
            patch.object(
                script_workflow_mod, "generate_parent_source_script",
                return_value="voice script text",
            ) as parent_stub,
            patch.object(
                scripts_mod, "generate_multilingual_scripts",
                return_value=[Script(content_id=content.id, language="en", voice_script="x", validated=True)],
            ),
            patch.object(script_workflow_mod, "run_script_workflow") as workflow_stub,
        ):
            result = pipeline._run_step_scripts(content, channel, None, db)

        self.assertTrue(result)
        parent_stub.assert_called_once()
        workflow_stub.assert_not_called()
        self.assertEqual(content.status, "SCRIPTS_VALIDATED")


# ── Bug 1b: shorts-planning step must skip immediately for a Solo Short ────

class RunStepShortsPlanningSoloShortTest(unittest.TestCase):
    def test_solo_short_skips_without_calling_run_shorts_planner(self):
        channel_id = uuid.uuid4()
        channel = Channel(id=channel_id, user_id=uuid.uuid4(), name="C", niche="n", tone="t")
        content = _solo_short_content(channel_id, status="SCRIPTS_VALIDATED")
        db = _FakeDb()
        db.add(channel)
        db.add(content)

        with patch.object(scripts_mod, "run_shorts_planner") as planner_stub:
            children = pipeline._run_step_shorts_planning(content, channel, None, db)

        self.assertEqual(children, [])
        planner_stub.assert_not_called()

    def test_parent_content_still_calls_run_shorts_planner(self):
        """Regression: the unmodified parent path must be unaffected."""
        channel_id = uuid.uuid4()
        channel = Channel(id=channel_id, user_id=uuid.uuid4(), name="C", niche="n", tone="t")
        content = _parent_content(channel_id, status="SCRIPTS_VALIDATED")
        db = _FakeDb()
        db.add(channel)
        db.add(content)

        with patch.object(scripts_mod, "run_shorts_planner") as planner_stub:
            pipeline._run_step_shorts_planning(content, channel, None, db)

        planner_stub.assert_called_once()


# ── Bug 2: _resolve_parent_and_children() must not crash on a Solo Short ───

class ResolveParentAndChildrenTest(unittest.TestCase):
    def test_solo_short_resolves_to_itself_with_no_children(self):
        channel_id = uuid.uuid4()
        content = _solo_short_content(channel_id)
        db = _FakeDb()
        db.add(content)

        parent, children = pipeline._resolve_parent_and_children(content, db)

        self.assertIs(parent, content)
        self.assertEqual(children, [])

    def test_parent_content_resolves_to_itself_with_real_children_query(self):
        channel_id = uuid.uuid4()
        parent_content = _parent_content(channel_id)
        child = _child_of_parent_content(channel_id, parent_content.id)
        db = _FakeDb()
        db.add(parent_content)
        db.add(child)

        parent, children = pipeline._resolve_parent_and_children(parent_content, db)

        self.assertIs(parent, parent_content)
        self.assertEqual(children, [child])

    def test_child_of_parent_resolves_to_looked_up_parent(self):
        """Regression: the unmodified child-of-parent behavior (look up the
        real parent via parent_content_id) must be unaffected."""
        channel_id = uuid.uuid4()
        parent_content = _parent_content(channel_id)
        child = _child_of_parent_content(channel_id, parent_content.id)
        db = _FakeDb()
        db.add(parent_content)
        db.add(child)

        parent, children = pipeline._resolve_parent_and_children(child, db)

        self.assertIs(parent, parent_content)
        self.assertEqual(children, [])  # unchanged pre-existing behavior


# ── Bug 3: labeling ──────────────────────────────────────────────────────

class LabelingTest(unittest.TestCase):
    def test_solo_short_label_says_solo_short_not_child(self):
        content = _solo_short_content(uuid.uuid4())
        label = pipeline._label(content)
        self.assertIn("solo short", label)
        self.assertNotIn("child part", label)

    def test_child_of_parent_label_unchanged(self):
        content = _child_of_parent_content(uuid.uuid4(), uuid.uuid4())
        label = pipeline._label(content)
        self.assertIn("child part 1/2", label)

    def test_parent_label_unchanged(self):
        content = _parent_content(uuid.uuid4())
        label = pipeline._label(content)
        self.assertTrue(label.startswith("parent ("))


if __name__ == "__main__":
    unittest.main()
