"""Smoke test — child short audio no longer enqueues after parent AUDIO_DONE.

Phase 3B keeps the old helper/task names only as compatibility no-ops. Child
short audio is picked up through pickup_scripts_validated once Agent 2 marks the
child content SCRIPTS_VALIDATED.

No API calls, no DB access, no rendering.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, condition: bool) -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        _failures += 1


import app.scheduler as _sched_pkg
from app.scheduler.tasks import (
    ensure_child_short_audio_enqueued,
    pickup_scripts_validated,
    pickup_short_episodes_awaiting_parent,
    run_agent3_audio_for_content,
)
import app.agents.agent2_discovery.services.scripts as _script_mod
from app.agents.agent2_discovery.services.scripts import run_shorts_planner

_src_helper = inspect.getsource(ensure_child_short_audio_enqueued)
_src_psep = inspect.getsource(pickup_short_episodes_awaiting_parent)
_src_pickup = inspect.getsource(pickup_scripts_validated)
_src_rac = inspect.getsource(run_agent3_audio_for_content)
_src_planner = "\n".join([
    inspect.getsource(run_shorts_planner),
    inspect.getsource(_script_mod._persist_child_short_script),
])
_src_sched = inspect.getsource(_sched_pkg)

print("\n-- 1: child scripts are immediately audio-eligible --")
check("1a: run_shorts_planner marks child SCRIPTS_VALIDATED", 'short_content.status = "SCRIPTS_VALIDATED"' in _src_planner)
check("1b: run_shorts_planner does not write awaiting-parent status", "SCRIPTS_VALIDATED_AWAITING_PARENT" not in _src_planner)
check("1c: pickup_scripts_validated picks SCRIPTS_VALIDATED rows", 'Content.status == "SCRIPTS_VALIDATED"' in _src_pickup)
check("1d: pickup_scripts_validated does not filter out short episodes", "is_short_episode.is_(False)" not in _src_pickup and "is_short_episode.is_(True)" not in _src_pickup)
check("1e: AUDIO_PICKUP log includes is_short_episode", "AUDIO_PICKUP content_id=%s is_short_episode=%s" in _src_pickup)

print("\n-- 2: parent AUDIO_DONE release path is removed --")
check("2a: ensure_child_short_audio_enqueued remains callable", callable(ensure_child_short_audio_enqueued))
check("2b: ensure_child_short_audio_enqueued is documented no-op", "Compatibility no-op" in _src_helper)
check("2c: ensure_child_short_audio_enqueued returns 0", "return 0" in _src_helper)
check("2d: ensure_child_short_audio_enqueued does not enqueue Agent 3", "run_agent3_audio_for_content.delay" not in _src_helper)
check("2e: ensure_child_short_audio_enqueued does not query AudioFile", "AudioFile" not in _src_helper)
check("2f: ensure_child_short_audio_enqueued does not mutate status", ".status =" not in _src_helper)
check("2g: run_agent3_audio_for_content does not call the helper", "ensure_child_short_audio_enqueued(" not in _src_rac)

print("\n-- 3: old Beat task is compatibility only --")
check("3a: pickup_short_episodes_awaiting_parent remains callable", callable(pickup_short_episodes_awaiting_parent))
check("3b: pickup_short_episodes_awaiting_parent is documented no-op", "Compatibility no-op" in _src_psep)
check("3c: pickup_short_episodes_awaiting_parent returns 0", "return 0" in _src_psep)
check("3d: pickup_short_episodes_awaiting_parent does not enqueue Agent 3", "run_agent3_audio_for_content.delay" not in _src_psep)
check("3e: obsolete Beat schedule removed", "pickup-short-episodes-awaiting-parent" not in _src_sched)

print("\n-- 4: obsolete release wording is absent from live helper/task bodies --")
_live = "\n".join([_src_helper, _src_psep, _src_rac, _src_pickup, _src_planner])
check("4a: no SCRIPTS_VALIDATED_AWAITING_PARENT in live audio handoff code", "SCRIPTS_VALIDATED_AWAITING_PARENT" not in _live)
check("4b: no CHILD_SHORT_RELEASED log in live audio handoff code", "CHILD_SHORT_RELEASED" not in _live)
check("4c: no CHILD_SHORT_AUDIO_ENQUEUED log in live audio handoff code", "CHILD_SHORT_AUDIO_ENQUEUED" not in _live)

print()
if _failures == 0:
    print("SMOKE PASS — child short audio parent-release path removed")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)
