"""Smoke test — Standalone short architecture Agent 3 cleanup + run_shorts_planner idempotency.

Validates:
  1. PARENT AUDIO PATH — no semantic_splits, no bookends
     1a. recalculate_breakpoints NOT imported in audio.py
     1b. semantic_splits NOT called in run_audio_generation source
     1c. generate_short_bookends NOT called in the per-language loop
     1d. PARENT_AUDIO_STANDALONE_SHORTS_ONLY log present in run_audio_generation
     1e. breakpoints_disabled=True in PARENT_AUDIO_STANDALONE_SHORTS_ONLY log
     1f. bookends_disabled=True in PARENT_AUDIO_STANDALONE_SHORTS_ONLY log

  2. CHILD SHORT AUDIO PATH — own TTS + Whisper, no bookends
     2a. is_short_episode read from content in run_audio_generation
     2b. CHILD_SHORT_AUDIO_START log present in run_audio_generation
     2c. own_audio=True in CHILD_SHORT_AUDIO_START log
     2d. own_whisper=True in CHILD_SHORT_AUDIO_START log
     2e. generate_audio called (TTS present in source)
     2f. transcribe called (Whisper present in source)

  3. BOOKEND BLOCK REMOVED FROM PER-LANGUAGE LOOP
     3a. is_short_episode branch that calls generate_short_bookends gone
     3b. Standalone short architecture comment replacing the bookend block is present
     3c. generate_short_bookends function removed from Agent 3 audio service
     3d. No try/except wrapping generate_short_bookends in the per-language loop

  4. RUN_SHORTS_PLANNER IDEMPOTENCY
     4a. STANDALONE_SHORTS_ALREADY_EXIST sentinel in run_shorts_planner source
     4b. db.query(Content) guard runs before the for-loop
     4c. parent_content_id filter present in the guard
     4d. is_short_episode.is_(True) filter present in the guard
     4e. early return after STANDALONE_SHORTS_ALREADY_EXIST log

  5. STANDALONE SHORT LOG STRUCTURE
     5a. PARENT_AUDIO_STANDALONE_SHORTS_ONLY contains content_id= token
     5b. CHILD_SHORT_AUDIO_START contains parent_content_id= token
     5c. STANDALONE_SHORTS_ALREADY_EXIST contains count= token

No API calls. Run with:
    python scripts/smoke_child_short_audio_idempotent.py
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
import app.agents.agent3_audio.services.audio as _audio_mod
from app.agents.agent3_audio.services.audio import run_audio_generation
import app.agents.agent2_discovery.services.scripts as _script_mod
from app.agents.agent2_discovery.services.scripts import run_shorts_planner

_src_audio = inspect.getsource(_audio_mod)
_src_rag   = inspect.getsource(run_audio_generation)
_src_rsp   = "\n".join([
    inspect.getsource(run_shorts_planner),
    inspect.getsource(_script_mod._child_shorts_already_exist),
])

# ─────────────────────────────────────────────────────────────────────────────
# 1 — Parent audio path: no semantic_splits, no bookends
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 1: Parent audio path — no semantic_splits, no bookends ──")

check("1a: recalculate_breakpoints NOT imported in audio.py",
      "from app.agents.agent3_audio.services.breakpoints import recalculate_breakpoints" not in _src_audio)
check("1b: semantic_splits NOT referenced in run_audio_generation",
      "semantic_splits" not in _src_rag)
check("1c: generate_short_bookends NOT called in per-language loop of run_audio_generation",
      "generate_short_bookends(" not in _src_rag)
check("1d: PARENT_AUDIO_STANDALONE_SHORTS_ONLY log in run_audio_generation",
      "PARENT_AUDIO_STANDALONE_SHORTS_ONLY" in _src_rag)
check("1e: standalone_child_shorts_only=True in PARENT_AUDIO_STANDALONE_SHORTS_ONLY log",
      "standalone_child_shorts_only=True" in _src_rag)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — Child short audio path: own TTS + Whisper present
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 2: Child short audio path — own TTS + Whisper ──")

check("2a: is_short_episode read from content in run_audio_generation",
      "is_short_episode" in _src_rag)
check("2b: CHILD_SHORT_AUDIO_START log in run_audio_generation",
      "CHILD_SHORT_AUDIO_START" in _src_rag)
check("2c: own_audio=True in CHILD_SHORT_AUDIO_START log",
      "own_audio=True" in _src_rag)
check("2d: own_whisper=True in CHILD_SHORT_AUDIO_START log",
      "own_whisper=True" in _src_rag)
check("2e: generate_audio called in run_audio_generation (TTS present)",
      "generate_audio(" in _src_rag)
check("2f: transcribe called in run_audio_generation (Whisper present)",
      "transcribe(" in _src_rag)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — Bookend block removed from per-language loop
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 3: Bookend block removed from per-language loop ──")

check("3a: is_short_episode branch calling generate_short_bookends gone from loop",
      "generate_short_bookends(" not in _src_rag)
check("3b: no bookend compatibility block remains in run_audio_generation",
      "bookend generation is disabled" not in _src_rag and
      "bookend audio tracks" not in _src_rag)
check("3c: generate_short_bookends function removed from Agent 3 audio service",
      not hasattr(_audio_mod, "generate_short_bookends"))
check("3d: no try/except generate_short_bookends in per-language loop",
      "generate_short_bookends" not in _src_rag)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — run_shorts_planner idempotency
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 4: run_shorts_planner idempotency guard ──")

check("4a: STANDALONE_SHORTS_ALREADY_EXIST sentinel in run_shorts_planner",
      "STANDALONE_SHORTS_ALREADY_EXIST" in _src_rsp)
check("4b: db.query(Content) guard before the for-loop",
      "db.query(Content)" in _src_rsp and "STANDALONE_SHORTS_ALREADY_EXIST" in _src_rsp)
check("4c: parent_content_id filter in the guard",
      "parent_content_id" in _src_rsp and "STANDALONE_SHORTS_ALREADY_EXIST" in _src_rsp)
check("4d: is_short_episode.is_(True) filter in the guard",
      "is_short_episode.is_(True)" in _src_rsp)
check("4e: early return after STANDALONE_SHORTS_ALREADY_EXIST log",
      "STANDALONE_SHORTS_ALREADY_EXIST" in _src_rsp and
      "return" in _src_rsp[_src_rsp.index("STANDALONE_SHORTS_ALREADY_EXIST"):])

# ─────────────────────────────────────────────────────────────────────────────
# 5 — Standalone short architecture log structure
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 5: Standalone short architecture log structure ──")

check("5a: PARENT_AUDIO_STANDALONE_SHORTS_ONLY contains content_id=",
      "content_id=" in _src_rag and "PARENT_AUDIO_STANDALONE_SHORTS_ONLY" in _src_rag)
check("5b: CHILD_SHORT_AUDIO_START contains parent_content_id=",
      "parent_content_id=" in _src_rag and "CHILD_SHORT_AUDIO_START" in _src_rag)
check("5c: STANDALONE_SHORTS_ALREADY_EXIST contains count=",
      "count=%d" in _src_rsp or "count=" in _src_rsp)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────
print()
if _failures == 0:
    print("SMOKE PASS — Standalone short architecture audio cleanup + idempotency: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
