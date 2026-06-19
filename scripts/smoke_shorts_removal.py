"""Smoke test — Standalone short architecture parent render pipeline does NOT render parent-cut shorts.

Validates:
1. cut_shorts is NOT imported into video.py (legacy cutter deleted).
2. build_short_props IS imported (for Standalone short architecture child short episodes),
   but NOT called inside _run_renders (the parent-only main-render helper).
3. render_short IS imported (for Standalone short architecture child short episodes),
   but NOT called inside _run_renders.
4. STANDALONE_SHORTS_ONLY log is present in _process_language source.
5. CHILD_SHORT_RENDER_START log with format=short is present in run_video_generation.
6. CHILD_SHORT_RENDER_DONE log is present in _process_language source.
7. _run_renders() does NOT have a short_props_pairs parameter.
8. _run_renders() does NOT call render_short.
9. _render_from_existing_props() does NOT glob for *_short_*.json files.
10. build_main_props() does NOT have a shorts parameter.
11. build_main_props() does not write parent-short compatibility props.
12. shorts_cutter.py is fully deleted.
13. VideoRender(format="short") is NOT created in _run_renders source.

No API calls. Run with:
    python scripts/smoke_shorts_removal.py
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
# 1 — video.py imports
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 1: video.py does NOT import removed symbols ──")

import app.agents.agent5_render.services.video as video_mod

src_video = inspect.getsource(video_mod)

check("1.1: cut_shorts NOT imported in video.py (legacy cutter deleted)",
      "import cut_shorts" not in src_video and "from app.agents.agent5_render.subagents.shorts_cutter import" not in src_video)
check("1.2: build_short_props IS imported in video.py (Standalone short architecture child short episodes)",
      "build_short_props" in src_video)
check("1.3: render_short IS imported in video.py (Standalone short architecture child short episodes)",
      "render_short" in src_video)
check("1.4: shorts_cutter NOT in video.py imports section",
      "shorts_cutter" not in src_video)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — STANDALONE_SHORTS_ONLY log
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 2: STANDALONE_SHORTS_ONLY log present ──")

from app.agents.agent5_render.services.video import _process_language

src_pl = inspect.getsource(_process_language)

check("2.1: STANDALONE_SHORTS_ONLY in _process_language source",
      "STANDALONE_SHORTS_ONLY" in src_pl)
check("2.2: shorts_path=standalone_episodes_only in STANDALONE_SHORTS_ONLY log",
      "shorts_path=standalone_episodes_only" in src_pl)
check("2.3: parent_cut_shorts_removed=True in STANDALONE_SHORTS_ONLY log",
      "parent_cut_shorts_removed=True" in src_pl)
check("2.4: cut_shorts NOT called in _process_language",
      "cut_shorts(" not in src_pl)
check("2.5: build_short_props IS called in _process_language (child short episodes — guarded by is_short_episode=True)",
      "build_short_props(" in src_pl and "is_short_episode" in src_pl)
check("2.6: short_props_pairs NOT referenced in _process_language",
      "short_props_pairs" not in src_pl)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — CHILD_SHORT_RENDER_START log
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 3: CHILD_SHORT_RENDER_START and CHILD_SHORT_RENDER_DONE logs present ──")

from app.agents.agent5_render.services.video import run_video_generation

src_rvg = inspect.getsource(run_video_generation)

check("3.1: CHILD_SHORT_RENDER_START in run_video_generation source",
      "CHILD_SHORT_RENDER_START" in src_rvg)
check("3.2: format=short in CHILD_SHORT_RENDER_START log",
      "format=short" in src_rvg)
check("3.3: resolution=1080x1920 in CHILD_SHORT_RENDER_START log",
      "resolution=1080x1920" in src_rvg)
check("3.4: parent_content_id= field in CHILD_SHORT_RENDER_START log",
      "parent_content_id=" in src_rvg)
check("3.5: CHILD_SHORT_RENDER_DONE in _process_language source",
      "CHILD_SHORT_RENDER_DONE" in src_pl)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — _run_renders() has no short_props_pairs and no render_short
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 4: _run_renders() — parent path, no short renders ──")

from app.agents.agent5_render.services.video import _run_renders

src_rr = inspect.getsource(_run_renders)

check("4.1: short_props_pairs NOT in _run_renders source",
      "short_props_pairs" not in src_rr)
check("4.2: render_short NOT called in _run_renders (parent-only helper)",
      "render_short(" not in src_rr)
check("4.3: format='short' VideoRender NOT created in _run_renders",
      'format="short"' not in src_rr and "format='short'" not in src_rr)
check("4.4: standalone-shorts-only sentinel log present in _run_renders",
      "parent-cut shorts" in src_rr or "standalone shorts only" in src_rr)
check("4.5: build_short_props NOT called in _run_renders (parent-only helper)",
      "build_short_props(" not in src_rr)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — _render_from_existing_props() — no short glob
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 5: _render_from_existing_props() — no short glob ──")

from app.agents.agent5_render.services.video import _render_from_existing_props

src_rfep = inspect.getsource(_render_from_existing_props)

check("5.1: short_prop_files NOT in _render_from_existing_props",
      "short_prop_files" not in src_rfep)
check("5.2: *_short_*.json glob removed from _render_from_existing_props (no glob loop)",
      "glob(" not in src_rfep and "*_short_*" not in src_rfep)

# ─────────────────────────────────────────────────────────────────────────────
# 6 — remotion_builder.build_main_props() — no shorts param
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 6: build_main_props() — no parent-short compatibility props ──")

from app.agents.agent5_render.services.remotion_builder import build_main_props

src_bmp = inspect.getsource(build_main_props)
import inspect as _inspect
sig = _inspect.signature(build_main_props)

check("6.1: 'shorts' NOT in build_main_props signature",
      "shorts" not in sig.parameters)
check("6.2: 'shorts': [] sentinel NOT written in build_main_props",
      '"shorts": []' not in src_bmp)
check("6.3: shorts_with_subs NOT in build_main_props source",
      "shorts_with_subs" not in src_bmp)
check("6.4: cut_shorts NOT referenced in remotion_builder",
      "cut_shorts" not in inspect.getsource(__import__(
          "app.agents.agent5_render.services.remotion_builder",
          fromlist=["remotion_builder"]
      )))

# ─────────────────────────────────────────────────────────────────────────────
# 7 — shorts_cutter.py is fully deleted
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 7: shorts_cutter.py is fully deleted ──")

import importlib.util as _ilu
import os as _os

_sc_path = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "app", "agents", "agent5_render", "subagents", "shorts_cutter.py",
)
check("7.1: shorts_cutter.py file does not exist on disk",
      not _os.path.exists(_sc_path))
check("7.2: shorts_cutter module cannot be imported (deleted)",
      _ilu.find_spec("app.agents.agent5_render.subagents.shorts_cutter") is None)

# ─────────────────────────────────────────────────────────────────────────────
# 8 — build_main_props call in _process_language has no shorts= kwarg
# ─────────────────────────────────────────────────────────────────────────────
print("\n── 8: _process_language calls build_main_props without shorts= ──")

check("8.1: build_main_props call present in _process_language",
      "build_main_props(" in src_pl)
check("8.2: shorts= kwarg NOT passed to build_main_props in _process_language",
      "shorts=shorts" not in src_pl)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — Standalone short architecture Shorts removal: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
