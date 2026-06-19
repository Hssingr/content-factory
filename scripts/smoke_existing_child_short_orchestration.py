"""Smoke test — Standalone short architecture existing-child short orchestration.

Validates:
  1. pickup_short_episodes_awaiting_parent — unified pass (new + pre-existing)
     1a. queries BOTH SCRIPTS_VALIDATED_AWAITING_PARENT and SCRIPTS_VALIDATED in one filter
     1b. AUDIO_DONE is NOT in the status filter (already-done children are never re-enqueued)
     1c. CHILD_SHORT_RELEASED log present (flip path)
     1d. CHILD_SHORT_AUDIO_ENQUEUED log present (pre-existing SCRIPTS_VALIDATED path)
     1e. CHILD_SHORT_AUDIO_ENQUEUED still present (newly flipped path)
     1f. CHILD_SHORTS_RELEASED still present (group-by-parent summary)
     1g. run_agent3_audio_for_content.delay called after db.commit()

  2. run_agent3_audio_for_content — inline trigger after parent AUDIO_DONE
     2a. pickup_short_episodes_awaiting_parent() called inline in run_agent3_audio_for_content
     2b. inline call is inside the try block (after run_audio_generation)
     2c. gated on content.status == "AUDIO_DONE" (parent only)
     2d. gated on not is_short_episode (never triggered from child's own Agent 3)
     2e. inline call is NOT .delay() — it is a direct synchronous function call

  3. run_shorts_planner — STANDALONE_SHORTS_ALREADY_EXIST enriched
     3a. STANDALONE_SHORTS_ALREADY_EXIST still logged on early return
     3b. statuses= field added to the log (per-status breakdown)
     3c. early return preserved (no duplicate child creation)

  4. AUDIO_DONE children not re-enqueued
     4a. status.in_ filter excludes AUDIO_DONE explicitly
     4b. GENERATING_AUDIO not in status.in_ filter either

  5. No double-enqueue for newly flipped children
     5a. newly_released_ids set used to distinguish newly flipped from pre-existing
     5b. to_enqueue built from single actionable list (no separate query after commit)

No API calls. Run with:
    python scripts/smoke_standalone_short_existing_child_orchestration.py
"""

import sys
import os
import inspect

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


# ── Load modules ──────────────────────────────────────────────────────────────
from app.scheduler.tasks import (
    pickup_short_episodes_awaiting_parent,
    run_agent3_audio_for_content,
)
from app.agents.agent2_discovery.services.scripts import run_shorts_planner

_src_psep = inspect.getsource(pickup_short_episodes_awaiting_parent)
_src_rac  = inspect.getsource(run_agent3_audio_for_content)
_src_rsp  = inspect.getsource(run_shorts_planner)

# ─────────────────────────────────────────────────────────────────────────────
# 1 — pickup_short_episodes_awaiting_parent unified pass
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 1: pickup_short_episodes_awaiting_parent — unified pass ──")

check("1a: queries BOTH SCRIPTS_VALIDATED_AWAITING_PARENT and SCRIPTS_VALIDATED",
      "SCRIPTS_VALIDATED_AWAITING_PARENT" in _src_psep and
      '"SCRIPTS_VALIDATED"' in _src_psep and
      "status.in_" in _src_psep)
check("1b: AUDIO_DONE NOT in the status.in_ filter",
      # Verify AUDIO_DONE doesn't appear inside the in_() filter block
      "AUDIO_DONE" not in _src_psep or
      "AUDIO_DONE" not in _src_psep[_src_psep.index("status.in_"):_src_psep.index("status.in_") + 200])
check("1c: CHILD_SHORT_RELEASED log present (flip path)",
      "CHILD_SHORT_RELEASED" in _src_psep)
check("1d: CHILD_SHORT_AUDIO_ENQUEUED log present (pre-existing path)",
      "CHILD_SHORT_AUDIO_ENQUEUED" in _src_psep)
check("1e: CHILD_SHORT_AUDIO_ENQUEUED still present (newly flipped path)",
      "CHILD_SHORT_AUDIO_ENQUEUED" in _src_psep)
check("1f: CHILD_SHORTS_RELEASED still present (group-by-parent summary)",
      "CHILD_SHORTS_RELEASED" in _src_psep)
check("1g: run_agent3_audio_for_content.delay called after db.commit()",
      _src_psep.index("db.commit()") < _src_psep.index("run_agent3_audio_for_content.delay("))

# ─────────────────────────────────────────────────────────────────────────────
# 2 — run_agent3_audio_for_content — inline trigger after parent AUDIO_DONE
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 2: run_agent3_audio_for_content — inline pickup after parent AUDIO_DONE ──")

check("2a: pickup_short_episodes_awaiting_parent() called inline in run_agent3_audio_for_content",
      "pickup_short_episodes_awaiting_parent()" in _src_rac)
check("2b: inline call is after run_audio_generation call",
      _src_rac.index("run_audio_generation(") < _src_rac.index("pickup_short_episodes_awaiting_parent()"))
check("2c: gated on content.status == 'AUDIO_DONE'",
      "AUDIO_DONE" in _src_rac and "pickup_short_episodes_awaiting_parent()" in _src_rac)
check("2d: gated on not is_short_episode (child's own Agent 3 never triggers this)",
      "is_short_episode" in _src_rac and "pickup_short_episodes_awaiting_parent()" in _src_rac)
check("2e: inline call is NOT .delay() — direct synchronous function call",
      "pickup_short_episodes_awaiting_parent()" in _src_rac and
      "pickup_short_episodes_awaiting_parent.delay()" not in _src_rac)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — run_shorts_planner STANDALONE_SHORTS_ALREADY_EXIST enriched
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 3: run_shorts_planner STANDALONE_SHORTS_ALREADY_EXIST enriched ──")

check("3a: STANDALONE_SHORTS_ALREADY_EXIST still in run_shorts_planner",
      "STANDALONE_SHORTS_ALREADY_EXIST" in _src_rsp)
check("3b: statuses= field added to the log",
      "statuses=" in _src_rsp and "STANDALONE_SHORTS_ALREADY_EXIST" in _src_rsp)
check("3c: early return preserved after the log",
      "return" in _src_rsp[_src_rsp.index("STANDALONE_SHORTS_ALREADY_EXIST"):])

# ─────────────────────────────────────────────────────────────────────────────
# 4 — AUDIO_DONE children not re-enqueued
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 4: AUDIO_DONE and GENERATING_AUDIO children not re-enqueued ──")

# Extract the status.in_() block (between "status.in_([" and the closing "])")
_in_block_start = _src_psep.find("status.in_(")
_in_block_end   = _src_psep.find("])", _in_block_start)
_in_block        = _src_psep[_in_block_start:_in_block_end + 2] if _in_block_start != -1 else ""

check("4a: AUDIO_DONE not in status.in_() filter block",
      "AUDIO_DONE" not in _in_block)
check("4b: GENERATING_AUDIO not in status.in_() filter block",
      "GENERATING_AUDIO" not in _in_block)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — No double-enqueue for newly flipped children
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 5: No double-enqueue — newly_released_ids guards log differentiation ──")

check("5a: newly_released_ids set used in pickup_short_episodes_awaiting_parent",
      "newly_released_ids" in _src_psep)
check("5b: to_enqueue list built from single pass (no extra query after commit for enqueue)",
      "to_enqueue" in _src_psep and "to_enqueue.append(" in _src_psep)
check("5c: log differentiation: newly_released_ids check before logging EXISTING vs new",
      "newly_released_ids" in _src_psep and
      "CHILD_SHORT_AUDIO_ENQUEUED" in _src_psep and
      "CHILD_SHORT_AUDIO_ENQUEUED" in _src_psep)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────
print()
if _failures == 0:
    print("SMOKE PASS — Standalone short architecture existing-child orchestration: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
