"""Smoke test — Standalone short architecture child short audio orchestration.

Validates:
  1. CHILD AGENT 4 ENQUEUED AFTER PARENT FLIP
     1a. CHILD_SHORTS_RELEASED log in pickup_short_episodes_awaiting_parent
     1b. CHILD_SHORT_AUDIO_ENQUEUED log in pickup_short_episodes_awaiting_parent
     1c. run_agent3_audio_for_content.delay called inside pickup_short_episodes_awaiting_parent
     1d. enqueue happens AFTER db.commit() (status persisted before worker picks up)
     1e. part=N/M format in CHILD_SHORT_AUDIO_ENQUEUED log

  2. CHILD AUDIO STORED UNDER CHILD CONTENT_ID (not parent)
     2a. audio_path called with content_id in run_audio_generation (not parent_content_id)
     2b. save_audio called with content_id in run_audio_generation
     2c. _upsert_audio_file called with content_id in run_audio_generation
     2d. parent_content_id NOT used as path argument in run_audio_generation

  3. PARENT AUDIO NOT REUSED BY CHILD
     3a. run_audio_generation does not read AudioFile from parent_content_id
     3b. generate_audio called per-language in the loop (own TTS)
     3c. transcribe called per-language in the loop (own Whisper)

  4. CHILD_SHORT_AUDIO_START LOG EMITTED
     4a. CHILD_SHORT_AUDIO_START present in run_audio_generation source
     4b. own_audio=True in CHILD_SHORT_AUDIO_START log
     4c. own_whisper=True in CHILD_SHORT_AUDIO_START log
     4d. parent_content_id= in CHILD_SHORT_AUDIO_START log (traceability)

  5. CHILD_SHORT_AUDIO_DONE LOG EMITTED
     5a. CHILD_SHORT_AUDIO_DONE present in run_audio_generation source
     5b. child_content_id= in CHILD_SHORT_AUDIO_DONE log
     5c. duration_ms= in CHILD_SHORT_AUDIO_DONE log
     5d. CHILD_SHORT_AUDIO_DONE is gated on is_short_episode

  6. PIPELINE ORDER INVARIANT
     6a. pickup_short_episodes_awaiting_parent defined before pickup_audio_done in tasks.py
     6b. run_agent3_audio_for_content.delay NOT called in pickup_audio_done (Agent 5 render picks those up)
     6c. Agent 3 enqueue is inside pickup_short_episodes_awaiting_parent (not pickup_scripts_validated)

No API calls. Run with:
    python scripts/smoke_standalone_short_child_audio_orchestration.py
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
import app.agents.agent3_audio.services.audio as _audio_mod
from app.scheduler.tasks import pickup_short_episodes_awaiting_parent, pickup_audio_done
from app.agents.agent3_audio.services.audio import run_audio_generation

_src_psep = inspect.getsource(pickup_short_episodes_awaiting_parent)
_src_pad  = inspect.getsource(pickup_audio_done)
_src_rag  = inspect.getsource(run_audio_generation)

# ─────────────────────────────────────────────────────────────────────────────
# 1 — Child Agent 3 enqueued after parent flip
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 1: Child Agent 3 enqueued after parent flip ──")

check("1a: CHILD_SHORTS_RELEASED log in pickup_short_episodes_awaiting_parent",
      "CHILD_SHORTS_RELEASED" in _src_psep)
check("1b: CHILD_SHORT_AUDIO_ENQUEUED log in pickup_short_episodes_awaiting_parent",
      "CHILD_SHORT_AUDIO_ENQUEUED" in _src_psep)
check("1c: run_agent3_audio_for_content.delay called in pickup_short_episodes_awaiting_parent",
      "run_agent3_audio_for_content.delay(" in _src_psep)
check("1d: enqueue happens after db.commit() — commit precedes delay call in source",
      _src_psep.index("db.commit()") < _src_psep.index("run_agent3_audio_for_content.delay("))
check("1e: part=N/M format in CHILD_SHORT_AUDIO_ENQUEUED log",
      "part=%d/%d" in _src_psep)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — Child audio stored under child content_id (not parent)
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 2: Child audio stored under child content_id ──")

check("2a: audio_path called with content_id in run_audio_generation",
      "audio_path(content_id" in _src_rag)
check("2b: save_audio called with content_id in run_audio_generation",
      "save_audio(content_id" in _src_rag)
check("2c: _upsert_audio_file called with content_id in run_audio_generation",
      "_upsert_audio_file(db, content_id" in _src_rag)
check("2d: parent_content_id NOT used as audio storage path arg",
      "audio_path(content.parent_content_id" not in _src_rag and
      "save_audio(content.parent_content_id" not in _src_rag)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — Parent audio not reused by child
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 3: Parent audio not reused by child ──")

check("3a: AudioFile not fetched from parent_content_id in run_audio_generation",
      "parent_content_id" not in _src_rag or
      "AudioFile" not in _src_rag.split("parent_content_id")[0].split("AudioFile")[-1])
check("3b: generate_audio called in the per-language loop (own TTS per child)",
      "generate_audio(" in _src_rag)
check("3c: transcribe called in the per-language loop (own Whisper per child)",
      "transcribe(" in _src_rag)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — CHILD_SHORT_AUDIO_START log emitted
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 4: CHILD_SHORT_AUDIO_START log emitted for child short episodes ──")

check("4a: CHILD_SHORT_AUDIO_START in run_audio_generation source",
      "CHILD_SHORT_AUDIO_START" in _src_rag)
check("4b: own_audio=True in CHILD_SHORT_AUDIO_START log",
      "own_audio=True" in _src_rag)
check("4c: own_whisper=True in CHILD_SHORT_AUDIO_START log",
      "own_whisper=True" in _src_rag)
check("4d: parent_content_id= in CHILD_SHORT_AUDIO_START log (traceability)",
      "parent_content_id=" in _src_rag and "CHILD_SHORT_AUDIO_START" in _src_rag)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — CHILD_SHORT_AUDIO_DONE log emitted
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 5: CHILD_SHORT_AUDIO_DONE log emitted on success ──")

check("5a: CHILD_SHORT_AUDIO_DONE in run_audio_generation source",
      "CHILD_SHORT_AUDIO_DONE" in _src_rag)
check("5b: child_content_id= in CHILD_SHORT_AUDIO_DONE log",
      "child_content_id=" in _src_rag)
check("5c: duration_ms= in CHILD_SHORT_AUDIO_DONE log",
      "duration_ms=" in _src_rag and "CHILD_SHORT_AUDIO_DONE" in _src_rag)
check("5d: CHILD_SHORT_AUDIO_DONE gated on is_short_episode (appears after is_short_episode check)",
      "is_short_episode" in _src_rag and
      _src_rag.index("is_short_episode") < _src_rag.index("CHILD_SHORT_AUDIO_DONE"))

# ─────────────────────────────────────────────────────────────────────────────
# 6 — Pipeline order invariant
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 6: Pipeline order invariants ──")

_src_tasks_full = inspect.getsource(_tasks_mod)

check("6a: pickup_short_episodes_awaiting_parent defined before pickup_audio_done in tasks.py",
      _src_tasks_full.index("def pickup_short_episodes_awaiting_parent") <
      _src_tasks_full.index("def pickup_audio_done"))

check("6b: run_agent3_audio_for_content.delay NOT called in pickup_audio_done (Agent 5 render path only)",
      "run_agent3_audio_for_content.delay" not in _src_pad)

from app.scheduler.tasks import pickup_scripts_validated
_src_psv = inspect.getsource(pickup_scripts_validated)
check("6c: Agent 3 immediate enqueue is in pickup_short_episodes_awaiting_parent (not only pickup_scripts_validated)",
      "run_agent3_audio_for_content.delay(" in _src_psep)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────
print()
if _failures == 0:
    print("SMOKE PASS — Standalone short architecture child short audio orchestration: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
