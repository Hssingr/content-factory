"""Stuck-state recovery + retry-guard fixes (fresh full-system audit §1.2).

Runtime proofs (per CLAUDE.md §19.4 — real task/service code, fake DB, no
external API on any path):

1. `_stale_in_progress()` queries exactly the given in-progress status with a
   cutoff derived from that stage's recovery threshold.
2. The four Beat pickups re-dispatch stale in-progress rows (and
   `pickup_visual_ready` dispatches stale RENDERING rows directly, bypassing
   the has_render skip that would strand a partially-rendered row).
3. The Agent 2/3 task guards now accept their own in-progress status, so a
   Celery retry after a mid-run exception actually re-enters the service
   instead of skipping itself.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import sys

import pytest

# Optional provider SDKs are not installed in the test environment — stub the
# import surface only (same pattern as the other agent3-importing tests).
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

import app.agents.agent2_discovery.services.script_workflow  # noqa: F401,E402 — patch target
import app.agents.agent3_audio.services.audio  # noqa: F401,E402 — patch target
from app.scheduler import tasks as tasks_mod  # noqa: E402
from app.scheduler.tasks import _stale_in_progress, _STALE_RECOVERY_MINUTES


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filter_args = []

    def filter(self, *args):
        self.filter_args.extend(args)
        return self

    def order_by(self, *args):
        return self

    def limit(self, *_):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Returns a dedicated row list per (model, call index)."""

    def __init__(self, rows_by_call=None):
        self.rows_by_call = list(rows_by_call or [])
        self.queries: list[_FakeQuery] = []

    def query(self, *_models):
        rows = self.rows_by_call.pop(0) if self.rows_by_call else []
        q = _FakeQuery(rows)
        self.queries.append(q)
        return q

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def get(self, _model, _pk):
        return None


def _stuck_row(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), status=status, is_short_episode=False,
        updated_at=datetime.now(timezone.utc) - timedelta(hours=12),
    )


# ── 1. _stale_in_progress query shape ────────────────────────────────────────

class TestStaleInProgressQuery:
    @pytest.mark.parametrize("status", sorted(_STALE_RECOVERY_MINUTES))
    def test_filters_on_status_and_threshold_cutoff(self, status):
        db = _FakeDb([[_stuck_row(status)]])
        before = datetime.now(timezone.utc)
        rows = _stale_in_progress(db, status)
        after = datetime.now(timezone.utc)

        assert len(rows) == 1
        # Two filter expressions: Content.status == <status>, Content.updated_at < cutoff
        exprs = db.queries[0].filter_args
        assert len(exprs) == 2
        assert exprs[0].right.value == status
        cutoff = exprs[1].right.value
        expected_lo = before - timedelta(minutes=_STALE_RECOVERY_MINUTES[status])
        expected_hi = after - timedelta(minutes=_STALE_RECOVERY_MINUTES[status])
        assert expected_lo <= cutoff <= expected_hi

    def test_unknown_status_raises(self):
        with pytest.raises(KeyError):
            _stale_in_progress(_FakeDb(), "RENDERED")


# ── 2. Pickups re-dispatch stale rows ────────────────────────────────────────

def _run_pickup_with(db, task_name, delay_targets):
    """Run a pickup task with the session factory and .delay targets patched."""
    patches = [patch("app.database._get_session_factory", return_value=lambda: db)]
    captured: dict[str, list] = {name: [] for name in delay_targets}
    for name in delay_targets:
        target = getattr(tasks_mod, name)
        patches.append(patch.object(
            target, "delay",
            side_effect=lambda cid, _n=name: captured[_n].append(cid),
        ))
    with patches[0]:
        started = [p.start() for p in patches[1:]]
        try:
            result = getattr(tasks_mod, task_name).apply().result
        finally:
            for p in patches[1:]:
                p.stop()
        del started
    return result, captured


class TestPickupsRecoverStuckRows:
    def test_pickup_approved_content_redispatches_stale_generating_scripts(self):
        stuck = _stuck_row("GENERATING_SCRIPTS")
        db = _FakeDb([[], [stuck]])  # APPROVED query, then stale query
        _, captured = _run_pickup_with(
            db, "pickup_approved_content", ["run_agent2_scripts_for_content"],
        )
        assert captured["run_agent2_scripts_for_content"] == [str(stuck.id)]

    def test_pickup_scripts_validated_redispatches_stale_generating_audio(self):
        stuck = _stuck_row("GENERATING_AUDIO")
        db = _FakeDb([[], [stuck]])
        _, captured = _run_pickup_with(
            db, "pickup_scripts_validated", ["run_agent3_audio_for_content"],
        )
        assert captured["run_agent3_audio_for_content"] == [str(stuck.id)]

    def test_pickup_audio_done_redispatches_stale_generating_visuals(self):
        stuck = _stuck_row("GENERATING_VISUALS")
        # Query order: AUDIO_DONE list, stale list, then per-row AudioFile + VideoRender
        db = _FakeDb([[], [stuck], [SimpleNamespace()], []])
        _, captured = _run_pickup_with(
            db, "pickup_audio_done", ["run_agent4_visual_generation_for_content"],
        )
        assert captured["run_agent4_visual_generation_for_content"] == [str(stuck.id)]

    def test_pickup_visual_ready_dispatches_stale_rendering_directly(self):
        """A stale RENDERING row with a partial render must dispatch anyway —
        it bypasses the has_render skip (it already passed readiness gates)."""
        stuck = _stuck_row("RENDERING")
        # Query order: stale RENDERING list first, then the *_VISUALS_DONE candidates.
        db = _FakeDb([[stuck], []])
        _, captured = _run_pickup_with(
            db, "pickup_visual_ready", ["run_agent5_render_for_content"],
        )
        assert captured["run_agent5_render_for_content"] == [str(stuck.id)]


# ── 3. Task guards accept their own in-progress status ──────────────────────

class TestRetryGuardsAcceptInProgress:
    def _run_task_with_content(self, task_name, service_path, content):
        db = _FakeDb()
        db.get = lambda model, pk: content
        called = []
        with patch("app.database._get_session_factory", return_value=lambda: db), \
             patch(service_path, side_effect=lambda *a, **k: called.append(a)) :
            getattr(tasks_mod, task_name).apply(args=[str(content.id)])
        return called

    def test_agent2_guard_accepts_generating_scripts(self):
        content = _stuck_row("GENERATING_SCRIPTS")
        called = self._run_task_with_content(
            "run_agent2_scripts_for_content",
            "app.agents.agent2_discovery.services.script_workflow.run_script_workflow",
            content,
        )
        assert len(called) == 1, "retried task must re-enter run_script_workflow"

    def test_agent3_guard_accepts_generating_audio(self):
        content = _stuck_row("GENERATING_AUDIO")
        called = self._run_task_with_content(
            "run_agent3_audio_for_content",
            "app.agents.agent3_audio.services.audio.run_audio_generation",
            content,
        )
        assert len(called) == 1, "retried task must re-enter run_audio_generation"

    def test_agent2_guard_still_skips_terminal_status(self):
        content = _stuck_row("RENDERED")
        called = self._run_task_with_content(
            "run_agent2_scripts_for_content",
            "app.agents.agent2_discovery.services.script_workflow.run_script_workflow",
            content,
        )
        assert called == []
