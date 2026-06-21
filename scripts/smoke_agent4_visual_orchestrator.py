"""Smoke test — Phase 4D-B Agent 4 visual orchestrator boundary.

Verifies:
  1. Agent 4 exposes a single public visual entrypoint: run_visual_generation().
  2. Agent 4 owns VideoSection persistence (_save_video_sections lives in the
     orchestrator, not in Agent 5's video.py).
  3. Agent 5 (video.py) calls only the Agent 4 entrypoint — it does not import
     or call Agent 4's internal storyboard/validation/Flux/remap helpers.
  4. Visual-readiness milestone logs (PARENT_VISUALS_*, CHILD_SHORT_VISUALS_*)
     are owned by Agent 4; render milestone logs (CHILD_SHORT_RENDER_*) stay
     owned by Agent 5.
  5. Runtime status handoffs (CHILD_SHORT_VISUALS_DEFERRED -> AUDIO_DONE,
     VISUALS_FAILED -> FAILED) are still wired from Agent 5's run_video_generation.

No live APIs, no DB, no Remotion render. Static/import checks only.
"""

import importlib
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, condition: bool) -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        _failures += 1


print("\n── 1: Agent 4 visual orchestrator entrypoint ──")
orchestrator = importlib.import_module(
    "app.agents.agent4_visuals.services.visual_orchestrator"
)
video_mod = importlib.import_module("app.agents.agent5_render.services.video")

check("1a: run_visual_generation exists and is callable",
      callable(getattr(orchestrator, "run_visual_generation", None)))
check("1b: internal parent visual pass helper lives in Agent 4",
      callable(getattr(orchestrator, "_run_visual_pass", None)))
check("1c: internal child short visual helper lives in Agent 4",
      callable(getattr(orchestrator, "_run_child_short_visuals", None)))

print("\n── 2: VideoSection persistence owned by Agent 4 ──")
check("2a: _save_video_sections lives in Agent 4 orchestrator",
      callable(getattr(orchestrator, "_save_video_sections", None)))
check("2b: _save_video_sections removed from Agent 5 video.py",
      not hasattr(video_mod, "_save_video_sections"))
check("2c: _load_sections_from_db removed from Agent 5 video.py",
      not hasattr(video_mod, "_load_sections_from_db"))

print("\n── 3: Agent 5 does not call Agent 4 at all (render-only since Phase 4D-C) ──")
video_src = inspect.getsource(video_mod)
import ast as _ast
_video_tree = _ast.parse(video_src)
_video_imports: list[str] = []
for _node in _ast.walk(_video_tree):
    if isinstance(_node, _ast.ImportFrom) and _node.module:
        _video_imports.append(_node.module)
    elif isinstance(_node, _ast.Import):
        _video_imports.extend(alias.name for alias in _node.names)
check("3a: video.py has no import statement naming an Agent 4 module",
      not any(m.startswith("app.agents.agent4_visuals") for m in _video_imports))
check("3b: video.py does not import Agent 4 storyboard internals",
      "app.agents.agent4_visuals.subagents.storyboard" not in video_src)
check("3c: video.py does not import Agent 4 Flux generator",
      "app.agents.agent4_visuals.services.flux_generator" not in video_src)
check("3d: video.py does not import Agent 4 storyboard validator",
      "app.agents.agent4_visuals.subagents.storyboard_validator" not in video_src)
check("3e: run_video_generation does not call run_visual_generation(...)",
      "run_visual_generation(" not in inspect.getsource(video_mod.run_video_generation))

print("\n── 4: Logging ownership ──")
src_orchestrator = inspect.getsource(orchestrator)
check("4a: PARENT_VISUALS_START logged in Agent 4",
      "PARENT_VISUALS_START" in src_orchestrator)
check("4b: PARENT_VISUALS_DONE logged in Agent 4",
      "PARENT_VISUALS_DONE" in src_orchestrator)
check("4c: CHILD_SHORT_VISUALS_DEFERRED logged in Agent 4",
      "CHILD_SHORT_VISUALS_DEFERRED" in src_orchestrator)
check("4d: CHILD_SHORT_VISUALS_DONE logged in Agent 4",
      "CHILD_SHORT_VISUALS_DONE" in src_orchestrator)
# video.py legitimately compares against the status strings returned by Agent 4
# (e.g. `visual_status == "CHILD_SHORT_VISUALS_DEFERRED"`); it must not contain
# the log-message form of these markers (i.e. the marker followed by a space,
# as used in logger.info/.warning calls).
check("4e: visual milestone log messages absent from Agent 5 video.py",
      "PARENT_VISUALS_START " not in video_src
      and "PARENT_VISUALS_DONE " not in video_src
      and "CHILD_SHORT_VISUALS_DEFERRED " not in video_src
      and "CHILD_SHORT_VISUALS_DONE " not in video_src)
check("4f: render milestone logs still owned by Agent 5",
      "CHILD_SHORT_RENDER_START" in video_src and "CHILD_SHORT_RENDER_DONE" in video_src)

print("\n── 5: Status handoff wiring owned by Agent 4 (since Phase 4D-C) ──")
check("5a: run_visual_generation_for_content exists as the Agent 4 task entrypoint",
      callable(getattr(orchestrator, "run_visual_generation_for_content", None)))
src_run_visual_generation_for_content = inspect.getsource(
    orchestrator.run_visual_generation_for_content
)
check("5b: CHILD_SHORT_VISUALS_DEFERRED maps to AUDIO_DONE in Agent 4",
      '"CHILD_SHORT_VISUALS_DEFERRED"' in src_run_visual_generation_for_content
      and 'content.status = "AUDIO_DONE"' in src_run_visual_generation_for_content)
check("5c: VISUALS_FAILED maps to FAILED in Agent 4",
      '"VISUALS_FAILED"' in src_run_visual_generation_for_content
      and 'content.status = "FAILED"' in src_run_visual_generation_for_content)
check("5d: Agent 5 video.py no longer contains this status-handoff wiring",
      '"CHILD_SHORT_VISUALS_DEFERRED"' not in video_src
      and '"VISUALS_FAILED"' not in video_src)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4D-B/4D-C Agent 4 visual orchestrator boundary")
