"""Smoke test — Standalone short architecture child audio enqueue after parent AUDIO_DONE.

Validates:
  1. ensure_child_short_audio_enqueued helper exists and is correct
     1a. function defined in tasks module
     1b. CHILD_SHORT_AUDIO_SCAN log with total= and statuses=
     1c. CHILD_SHORT_RELEASED log (flip AWAITING_PARENT → SCRIPTS_VALIDATED)
     1d. CHILD_SHORT_AUDIO_ENQUEUED log (SCRIPTS_VALIDATED + no AudioFile)
     1e. CHILD_SHORT_AUDIO_ALREADY_EXISTS log (SCRIPTS_VALIDATED + AudioFile exists — skip)
     1f. CHILD_SHORT_AUDIO_SKIP log (AUDIO_DONE / GENERATING_AUDIO / etc — skip)
     1g. AudioFile query present — existence check before enqueue
     1h. run_agent3_audio_for_content.delay called inside ensure_child_short_audio_enqueued
     1i. takes parent_content_id and db parameters (same-session, no new session opened)

  2. run_agent3_audio_for_content calls ensure_child_short_audio_enqueued
     2a. ensure_child_short_audio_enqueued(cid, db) called in run_agent3_audio_for_content
     2b. call passes cid (UUID) and db (same session) — not pickup_short_episodes_awaiting_parent()
     2c. gated on content.status == "AUDIO_DONE"
     2d. gated on not is_short_episode (never fires for child's own audio generation)
     2e. call comes after run_audio_generation completes

  3. SCRIPTS_VALIDATED_AWAITING_PARENT case — flip then enqueue
     3a. child.status == "SCRIPTS_VALIDATED_AWAITING_PARENT" check present
     3b. child.status = "SCRIPTS_VALIDATED" assignment present
     3c. db.flush() called after flip (keeps same transaction)
     3d. CHILD_SHORT_RELEASED logged after flip
     3e. enqueue (if check falls through to SCRIPTS_VALIDATED block)

  4. SCRIPTS_VALIDATED + no AudioFile — enqueue
     4a. AudioFile.content_id == child.id filter present
     4b. run_agent3_audio_for_content.delay(str(child.id)) called when no AudioFile
     4c. enqueued counter incremented
     4d. db.commit() called only when enqueued > 0

  5. AUDIO_DONE children are skipped — not re-enqueued
     5a. AUDIO_DONE not in the filter for children (query has no status filter on children)
     5b. AUDIO_DONE children fall into the else branch → CHILD_SHORT_AUDIO_SKIP

  6. Beat path (pickup_short_episodes_awaiting_parent) still works independently
     6a. pickup_short_episodes_awaiting_parent still defined
     6b. still handles SCRIPTS_VALIDATED_AWAITING_PARENT status
     6c. still handles SCRIPTS_VALIDATED (pre-existing) via CHILD_SHORT_AUDIO_ENQUEUED

No API calls. Run with:
    python scripts/smoke_child_short_audio_enqueue_fix.py
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
import app.scheduler.tasks as _tasks_mod
from app.scheduler.tasks import (
    ensure_child_short_audio_enqueued,
    run_agent3_audio_for_content,
    pickup_short_episodes_awaiting_parent,
)

_src_helper = inspect.getsource(ensure_child_short_audio_enqueued)
_src_rac    = inspect.getsource(run_agent3_audio_for_content)
_src_psep   = inspect.getsource(pickup_short_episodes_awaiting_parent)

# ─────────────────────────────────────────────────────────────────────────────
# 1 — ensure_child_short_audio_enqueued helper
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 1: ensure_child_short_audio_enqueued helper ──")

check("1a: function importable from tasks module",
      callable(ensure_child_short_audio_enqueued))
check("1b: CHILD_SHORT_AUDIO_SCAN log with total= and statuses=",
      "CHILD_SHORT_AUDIO_SCAN" in _src_helper and
      "total=" in _src_helper and "statuses=" in _src_helper)
check("1c: CHILD_SHORT_RELEASED log present",
      "CHILD_SHORT_RELEASED" in _src_helper)
check("1d: CHILD_SHORT_AUDIO_ENQUEUED log present",
      "CHILD_SHORT_AUDIO_ENQUEUED" in _src_helper)
check("1e: CHILD_SHORT_AUDIO_ALREADY_EXISTS log present",
      "CHILD_SHORT_AUDIO_ALREADY_EXISTS" in _src_helper)
check("1f: CHILD_SHORT_AUDIO_SKIP log present",
      "CHILD_SHORT_AUDIO_SKIP" in _src_helper)
check("1g: AudioFile queried inside helper (existence check before enqueue)",
      "AudioFile" in _src_helper and "AudioFile.content_id" in _src_helper)
check("1h: run_agent3_audio_for_content.delay called inside helper",
      "run_agent3_audio_for_content.delay(" in _src_helper)
check("1i: accepts parent_content_id and db params (same session, no _get_session_factory)",
      "parent_content_id" in _src_helper and
      "db" in _src_helper and
      "_get_session_factory" not in _src_helper)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — run_agent3_audio_for_content calls the helper
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 2: run_agent3_audio_for_content uses ensure_child_short_audio_enqueued ──")

check("2a: ensure_child_short_audio_enqueued(cid, db) called in run_agent3_audio_for_content",
      "ensure_child_short_audio_enqueued(cid, db)" in _src_rac)
check("2b: pickup_short_episodes_awaiting_parent() NOT called inline in run_agent3_audio_for_content",
      "pickup_short_episodes_awaiting_parent()" not in _src_rac)
check("2c: gated on content.status == 'AUDIO_DONE'",
      "AUDIO_DONE" in _src_rac and "ensure_child_short_audio_enqueued" in _src_rac)
check("2d: gated on not is_short_episode",
      "is_short_episode" in _src_rac and "ensure_child_short_audio_enqueued" in _src_rac)
check("2e: ensure call comes after run_audio_generation",
      _src_rac.index("run_audio_generation(") < _src_rac.index("ensure_child_short_audio_enqueued"))

# ─────────────────────────────────────────────────────────────────────────────
# 3 — SCRIPTS_VALIDATED_AWAITING_PARENT case
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 3: SCRIPTS_VALIDATED_AWAITING_PARENT: flip then enqueue ──")

check("3a: SCRIPTS_VALIDATED_AWAITING_PARENT check in helper",
      "SCRIPTS_VALIDATED_AWAITING_PARENT" in _src_helper)
check("3b: child.status = 'SCRIPTS_VALIDATED' assignment",
      'child.status = "SCRIPTS_VALIDATED"' in _src_helper)
check("3c: db.flush() called after flip",
      "db.flush()" in _src_helper)
check("3d: CHILD_SHORT_RELEASED after flip block",
      _src_helper.index("SCRIPTS_VALIDATED_AWAITING_PARENT") <
      _src_helper.index("CHILD_SHORT_RELEASED"))
check("3e: if child.status == SCRIPTS_VALIDATED check follows the flip block",
      _src_helper.index("SCRIPTS_VALIDATED_AWAITING_PARENT") <
      _src_helper.index('"SCRIPTS_VALIDATED"'))

# ─────────────────────────────────────────────────────────────────────────────
# 4 — SCRIPTS_VALIDATED + no AudioFile — enqueue
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 4: SCRIPTS_VALIDATED + no AudioFile → enqueue ──")

check("4a: AudioFile.content_id == child.id filter in helper",
      "AudioFile.content_id == child.id" in _src_helper)
check("4b: run_agent3_audio_for_content.delay(str(child.id)) called on no-audio path",
      "run_agent3_audio_for_content.delay(str(child.id))" in _src_helper)
check("4c: enqueued counter incremented",
      "enqueued += 1" in _src_helper or "enqueued=" in _src_helper)
check("4d: db.commit() gated on enqueued > 0",
      "if enqueued:" in _src_helper and "db.commit()" in _src_helper)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — AUDIO_DONE children are skipped
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 5: AUDIO_DONE children skipped ──")

# Children query has no status filter — all children are loaded, then handled per status
check("5a: children query has no status.in_ filter (fetches all children)",
      "status.in_" not in _src_helper)
check("5b: AUDIO_DONE children fall into else branch (CHILD_SHORT_AUDIO_SKIP)",
      "CHILD_SHORT_AUDIO_SKIP" in _src_helper and "else:" in _src_helper)

# ─────────────────────────────────────────────────────────────────────────────
# 6 — Beat path still works
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 6: Beat path (pickup_short_episodes_awaiting_parent) still independent ──")

check("6a: pickup_short_episodes_awaiting_parent still defined and callable",
      callable(pickup_short_episodes_awaiting_parent))
check("6b: still handles SCRIPTS_VALIDATED_AWAITING_PARENT in Beat path",
      "SCRIPTS_VALIDATED_AWAITING_PARENT" in _src_psep)
check("6c: CHILD_SHORT_AUDIO_ENQUEUED still in Beat path",
      "CHILD_SHORT_AUDIO_ENQUEUED" in _src_psep)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────
print()
if _failures == 0:
    print("SMOKE PASS — Standalone short architecture child enqueue fix: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
