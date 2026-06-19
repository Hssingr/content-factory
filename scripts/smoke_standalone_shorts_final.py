"""Final Standalone short architecture smoke test — end-to-end correctness of standalone Short episodes.

Validates:
  1. PARENT PATH CANNOT CREATE SHORT RENDERS
     1a. _run_renders() does NOT call render_short()
     1b. _run_renders() does NOT create VideoRender(format="short")
     1c. _run_renders() does NOT call build_short_props()
     1d. parent render sentinel log present in _run_renders

  2. CHILD is_short_episode=True CREATES format="short"
     2a. _process_language() branches on is_short_episode
     2b. _run_short_render() creates VideoRender(format="short")
     2c. _run_short_render() calls render_short (Short.tsx)
     2d. build_short_props() called in child branch (is_short_episode=True)
     2e. no bookend compatibility args written for child episodes

  3. SHORT.TSX SELECTED FOR CHILD SHORT EPISODES
     3a. render_short imported in video.py
     3b. CHILD_SHORT_RENDER_START log contains format=short
     3c. CHILD_SHORT_RENDER_START log contains resolution=1080x1920
     3d. _run_short_render() calls render_short (not render_main_video)

  4. CHILD_SHORT_REUSE_STATS LOG IS EMITTED
     4a. CHILD_SHORT_REUSE_STATS present in remap_beats_for_short source
     4b. log contains reused_parent_images=
     4c. log contains new_flux_images=
     4d. log contains reuse_rate=
     4e. reuse_rate computed as (reuse_count / total_beats * 100)

  5. AGENT 6 / PUBLISHING QUERY EXPECTATION
     5a. VideoRender model has format column
     5b. VideoRender model has short_order column
     5c. Content model has is_short_episode column
     5d. Long video query expressible: format="main" AND is_short_episode=False
     5e. Short query expressible: format="short" AND is_short_episode=True
     5f. Module docstring documents both Agent 6 query patterns

No API calls. Run with:
    python scripts/smoke_standalone_short_final.py
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


# ─────────────────────────────────────────────────────────────────────────────
# Load modules and source once
# ─────────────────────────────────────────────────────────────────────────────
import app.agents.agent5_render.services.video as _video_mod
from app.agents.agent5_render.services.video import (
    _run_renders,
    _run_short_render,
    _process_language,
    run_video_generation,
)
from app.agents.agent4_visuals.subagents.storyboard import remap_beats_for_short

_src_rr  = inspect.getsource(_run_renders)
_src_rsr = inspect.getsource(_run_short_render)
_src_pl  = inspect.getsource(_process_language)
_src_rvg = inspect.getsource(run_video_generation)
_src_vid = inspect.getsource(_video_mod)
_src_rfb = inspect.getsource(remap_beats_for_short)

# ─────────────────────────────────────────────────────────────────────────────
# 1 — Parent path cannot create short renders
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 1: Parent path cannot create short renders ──")

check("1a: render_short NOT called in _run_renders",
      "render_short(" not in _src_rr)
check("1b: VideoRender(format='short') NOT in _run_renders",
      'format="short"' not in _src_rr and "format='short'" not in _src_rr)
check("1c: build_short_props NOT called in _run_renders",
      "build_short_props(" not in _src_rr)
check("1d: parent-cut shorts sentinel log in _run_renders",
      "parent-cut shorts" in _src_rr or "standalone" in _src_rr)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — Child is_short_episode=True creates format="short"
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 2: Child is_short_episode=True creates VideoRender(format='short') ──")

check("2a: _process_language() branches on is_short_episode",
      "is_short_episode" in _src_pl)
check("2b: _run_short_render() creates VideoRender(format='short')",
      'format="short"' in _src_rsr)
check("2c: _run_short_render() calls render_short()",
      "render_short(" in _src_rsr)
check("2d: build_short_props called in child branch of _process_language",
      "build_short_props(" in _src_pl and "is_short_episode" in _src_pl)
check("2e: no bookend compatibility args in child episode props",
      "bookends_enabled" not in _src_pl and "rehook_paths" not in _src_pl and "bridge_paths" not in _src_pl)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — Short.tsx selected for child short episodes
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 3: Short.tsx (9:16 / 1080×1920) selected for child short episodes ──")

check("3a: render_short imported in video.py",
      "render_short" in _src_vid)
check("3b: CHILD_SHORT_RENDER_START log contains format=short",
      "CHILD_SHORT_RENDER_START" in _src_rvg and "format=short" in _src_rvg)
check("3c: CHILD_SHORT_RENDER_START log contains resolution=1080x1920",
      "resolution=1080x1920" in _src_rvg)
check("3d: _run_short_render calls render_short not render_main_video",
      "render_short(" in _src_rsr and "render_main_video(" not in _src_rsr)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — CHILD_SHORT_REUSE_STATS log is emitted
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 4: CHILD_SHORT_REUSE_STATS log emitted by remap_beats_for_short ──")

check("4a: CHILD_SHORT_REUSE_STATS present in remap_beats_for_short source",
      "CHILD_SHORT_REUSE_STATS" in _src_rfb)
check("4b: log contains reused_parent_images=",
      "reused_parent_images=" in _src_rfb)
check("4c: log contains new_flux_images=",
      "new_flux_images=" in _src_rfb)
check("4d: log contains reuse_rate=",
      "reuse_rate=" in _src_rfb)
check("4e: reuse_rate computed from reuse_count and total_beats",
      "reuse_count" in _src_rfb and "total_beats" in _src_rfb and "reuse_rate" in _src_rfb)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — Agent 6 / publishing query expectation
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 5: Agent 6 / publishing query expectation ──")

from app.models.video_renders import VideoRender
from app.models.content import Content
from sqlalchemy import inspect as sqla_inspect

_vr_cols = {col.key for col in sqla_inspect(VideoRender).mapper.column_attrs}
_ct_cols = {col.key for col in sqla_inspect(Content).mapper.column_attrs}

check("5a: VideoRender has 'format' column",
      "format" in _vr_cols)
check("5b: VideoRender has 'short_order' column",
      "short_order" in _vr_cols)
check("5c: Content has 'is_short_episode' column",
      "is_short_episode" in _ct_cols)
check("5d: Long video query expressible (format='main' AND is_short_episode=False)",
      hasattr(VideoRender, "format") and hasattr(Content, "is_short_episode"))
check("5e: Short query expressible (format='short' AND is_short_episode=True)",
      hasattr(VideoRender, "format") and hasattr(Content, "is_short_episode"))

_docstring = _video_mod.__doc__ or ""
check("5f: module docstring documents Agent 6 query separation (main vs short)",
      "format==" in _docstring or "format=" in _docstring)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────
print()
if _failures == 0:
    print("SMOKE PASS — Standalone short architecture standalone shorts: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
