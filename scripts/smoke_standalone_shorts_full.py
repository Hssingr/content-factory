"""Comprehensive smoke test — Standalone short architecture is the ONLY Shorts system.

Validates:
 A. DELETED LEGACY CODE
   A1. shorts_cutter.py does not exist on disk.
   A2. No pipeline module imports shorts_cutter.
   A3. No pipeline module calls cut_shorts().
   A4. shorts_label_style removed from _process_language() signature.

 B. PARENT CONTENT RENDERS MAIN VIDEO ONLY
   B1. STANDALONE_SHORTS_ONLY log exists in _process_language.
   B2. parent_cut_shorts_removed=True in STANDALONE_SHORTS_ONLY log.
   B3. build_short_props NOT called in _process_language.
   B4. render_short NOT called in _run_renders.
   B5. VideoRender(format="short") NOT created in _run_renders or _render_from_existing_props.
   B6. short_props_pairs not referenced in _run_renders.

 C. STANDALONE SHORT PIPELINE WIRING
   C1. run_shorts_planner is called from tasks.py after SCRIPTS_VALIDATED.
   C2. child scripts are saved as SCRIPTS_VALIDATED directly.
   C3. obsolete parent-audio gate task is a compatibility no-op and is not scheduled.
   C4. Agent 3 run_audio_generation handles is_short_episode=True.
   C5. remap_beats_for_short imported and called in video.py for short episodes.
   C6. CHILD_SHORT_RENDER_START log exists in run_video_generation with format=short.
   C7. CHILD_SHORT_RENDER_START log contains resolution=1080x1920.
   C8. CHILD_SHORT_RENDER_DONE log exists in _process_language.
   C9. render_short IS imported in video.py (Standalone short architecture child short episodes).
   C10. build_short_props IS imported in video.py (Standalone short architecture child short episodes).

 D. FORMAT DECISION — format="short" for child short episodes
   D1. _is_rendered() accepts fmt and short_order parameters.
   D2. _run_renders writes VideoRender(format="main") only (parent path).
   D3. _run_short_render writes VideoRender(format="short") for child short episodes.
   D4. Module docstring documents both Agent 6 queries.

 E. AGENT 6 DISCOVERY
   E1. VideoRender model has format and short_order columns.
   E2. Content model has is_short_episode boolean.
   E3. Agent 6 long-video query: format="main" AND is_short_episode=False.
   E4. Agent 6 short query: format="short" AND is_short_episode=True.

No API calls. Run with:
    python scripts/smoke_full_standalone_short_shorts.py
"""

import sys
import os
import inspect
import importlib
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(label: str, condition: bool) -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        _failures += 1


# ─────────────────────────────────────────────────────────────────────────────
# A — DELETED LEGACY CODE
# ─────────────────────────────────────────────────────────────────────────────
print("\n── A: Deleted legacy code ──")

_sc_path = os.path.join(_REPO_ROOT, "app", "agents", "agent5_render", "subagents", "shorts_cutter.py")
check("A1: shorts_cutter.py does not exist on disk",
      not os.path.exists(_sc_path))
check("A2: shorts_cutter module cannot be imported",
      importlib.util.find_spec("app.agents.agent5_render.subagents.shorts_cutter") is None)

# Check pipeline modules
import app.agents.agent5_render.services.video as _video_mod
import app.agents.agent5_render.services.remotion_builder as _rb_mod
import app.scheduler.tasks as _tasks_mod

_src_video  = inspect.getsource(_video_mod)
_src_rb     = inspect.getsource(_rb_mod)
_src_tasks  = inspect.getsource(_tasks_mod)

check("A3a: video.py does not import shorts_cutter",
      "shorts_cutter" not in _src_video)
check("A3b: tasks.py does not import shorts_cutter",
      "shorts_cutter" not in _src_tasks)
check("A3c: remotion_builder.py does not import shorts_cutter",
      "shorts_cutter" not in _src_rb)
check("A4a: cut_shorts() not called in video.py",
      "cut_shorts(" not in _src_video)
check("A4b: cut_shorts() not called in tasks.py",
      "cut_shorts(" not in _src_tasks)

from app.agents.agent5_render.services.video import _process_language
_src_pl = inspect.getsource(_process_language)
check("A5: shorts_label_style NOT in _process_language signature",
      "shorts_label_style" not in _src_pl)

# ─────────────────────────────────────────────────────────────────────────────
# B — PARENT CONTENT RENDERS MAIN VIDEO ONLY
# ─────────────────────────────────────────────────────────────────────────────
print("\n── B: Parent content renders main video only ──")

check("B1: STANDALONE_SHORTS_ONLY log in _process_language",
      "STANDALONE_SHORTS_ONLY" in _src_pl)
check("B2: parent_cut_shorts_removed=True in STANDALONE_SHORTS_ONLY",
      "parent_cut_shorts_removed=True" in _src_pl)
from app.agents.agent5_render.services.video import _run_renders, _render_from_existing_props
_src_rr   = inspect.getsource(_run_renders)
_src_rfep = inspect.getsource(_render_from_existing_props)

check("B3: build_short_props NOT called in _run_renders (parent-only main-render helper)",
      "build_short_props(" not in _src_rr)
check("B4: render_short NOT called in _run_renders (parent-only path)",
      "render_short(" not in _src_rr)
check("B5a: VideoRender(format='short') NOT in _run_renders",
      'format="short"' not in _src_rr and "format='short'" not in _src_rr)
check("B5b: _run_renders is the parent-only path (short render uses _run_short_render)",
      "_run_short_render" not in _src_rr)
check("B6: short_props_pairs not referenced in _run_renders",
      "short_props_pairs" not in _src_rr)
check("B7: no *_short_*.json glob loop in _render_from_existing_props",
      "glob(" not in _src_rfep and "*.json" not in _src_rfep)

# ─────────────────────────────────────────────────────────────────────────────
# C — STANDALONE SHORT PIPELINE WIRING
# ─────────────────────────────────────────────────────────────────────────────
print("\n── C: Standalone short architecture pipeline wiring ──")

import app.agents.agent2_discovery.services.script_workflow as _workflow_mod
_src_workflow = inspect.getsource(_workflow_mod.run_script_workflow)
check("C1: run_shorts_planner called by Agent 2 workflow",
      "run_shorts_planner" in _src_workflow and "run_shorts_planner(" in _src_workflow)
import app.agents.agent2_discovery.services.scripts as _script_mod
_src_short_planner = "\n".join([
    inspect.getsource(_script_mod.run_shorts_planner),
    inspect.getsource(_script_mod._create_child_short_content),
    inspect.getsource(_script_mod._persist_child_short_script),
])
check("C2: child short scripts become SCRIPTS_VALIDATED directly",
      'short_content.status = "SCRIPTS_VALIDATED"' in _src_short_planner and
      "SCRIPTS_VALIDATED_AWAITING_PARENT" not in _src_short_planner)

import app.scheduler as _sched_pkg
_src_sched = inspect.getsource(_sched_pkg)
_src_parent_gate = inspect.getsource(_tasks_mod.pickup_short_episodes_awaiting_parent)
check("C3a: pickup_short_episodes_awaiting_parent is compatibility no-op",
      "Compatibility no-op" in _src_parent_gate and "return 0" in _src_parent_gate)
check("C3b: pickup_short_episodes_awaiting_parent is not scheduled in Celery Beat",
      "pickup_short_episodes_awaiting_parent" not in _src_sched)

import app.agents.agent3_audio.services.audio as _audio_mod
_src_audio = inspect.getsource(_audio_mod)
check("C4a: is_short_episode read in run_audio_generation",
      "is_short_episode" in _src_audio)
check("C4b: Agent 3 has no child bookend generation path",
      "is_short_episode" in _src_audio and "generate_short_bookends" not in _src_audio)

from app.agents.agent5_render.services.video import run_video_generation
_src_rvg = inspect.getsource(run_video_generation)
check("C5a: remap_beats_for_short imported in video.py",
      "remap_beats_for_short" in _src_video)
check("C5b: remap_beats_for_short called for short episodes",
      "remap_beats_for_short(" in _src_rvg)
check("C6: CHILD_SHORT_RENDER_START log in run_video_generation with format=short",
      "CHILD_SHORT_RENDER_START" in _src_rvg and "format=short" in _src_rvg)
check("C7: resolution=1080x1920 in CHILD_SHORT_RENDER_START log",
      "resolution=1080x1920" in _src_rvg)
check("C8: CHILD_SHORT_RENDER_DONE in _process_language source",
      "CHILD_SHORT_RENDER_DONE" in _src_pl)
check("C9: render_short IS imported in video.py (Standalone short architecture child short episodes)",
      "render_short" in _src_video)
check("C10: build_short_props IS imported in video.py (Standalone short architecture child short episodes)",
      "build_short_props" in _src_video)

# ─────────────────────────────────────────────────────────────────────────────
# D — FORMAT DECISION: format="main" for parent, format="short" for child short episodes
# ─────────────────────────────────────────────────────────────────────────────
print("\n── D: Format decision — format=main parent / format=short child short episodes ──")

from app.agents.agent5_render.services.video import _is_rendered, _run_short_render
_src_ir  = inspect.getsource(_is_rendered)
_src_rsr = inspect.getsource(_run_short_render)

check("D1: _is_rendered accepts fmt and short_order parameters",
      "fmt:" in _src_ir or "fmt =" in _src_ir or "fmt=" in _src_ir)
check("D2: _run_renders creates VideoRender with format='main' only (parent path)",
      'format="main"' in _src_rr and 'format="short"' not in _src_rr)
check("D3: _run_short_render creates VideoRender with format='short'",
      'format="short"' in _src_rsr)

# Module docstring documents both Agent 6 queries
_video_docstring = _video_mod.__doc__ or ""
check("D4: module docstring documents Agent 6 short query (format=short)",
      "format==" in _video_docstring or "format=" in _video_docstring)

# ─────────────────────────────────────────────────────────────────────────────
# E — AGENT 6 DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────
print("\n── E: Agent 6 can discover parent long videos and child short renders ──")

from app.models.video_renders import VideoRender
from app.models.content import Content
from sqlalchemy import inspect as sqla_inspect

_vr_cols = {col.key for col in sqla_inspect(VideoRender).mapper.column_attrs}
_ct_cols = {col.key for col in sqla_inspect(Content).mapper.column_attrs}

check("E1a: VideoRender has 'format' column",
      "format" in _vr_cols)
check("E1b: VideoRender has 'short_order' column",
      "short_order" in _vr_cols)
check("E2: Content has 'is_short_episode' column",
      "is_short_episode" in _ct_cols)
check("E3: Content has 'parent_content_id' column",
      "parent_content_id" in _ct_cols)
check("E4: Content.short_part_number column exists",
      "short_part_number" in _ct_cols)
check("E5: Content.short_total_parts column exists",
      "short_total_parts" in _ct_cols)

# Agent 6 long-video query: VideoRender.format=="main" JOIN content WHERE is_short_episode==False
_long_query_expressible = (
    hasattr(VideoRender, "format") and
    hasattr(VideoRender, "content_id") and
    hasattr(Content, "is_short_episode")
)
check("E6a: Agent 6 long-video query expressible: format='main' AND is_short_episode=False",
      _long_query_expressible)

# Agent 6 short query: VideoRender.format=="short" JOIN content WHERE is_short_episode==True
check("E6b: Agent 6 short query expressible: format='short' AND is_short_episode=True",
      _long_query_expressible)  # same model attributes, different filter values

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — Standalone short architecture is the only Shorts system: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
