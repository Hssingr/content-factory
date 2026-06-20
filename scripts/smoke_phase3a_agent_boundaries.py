"""Smoke test — Phase 3A agent package and task boundaries.

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


print("\n── 1: New agent packages import ──")
agent3_audio = importlib.import_module("app.agents.agent3_audio.services.audio")
agent4_storyboard = importlib.import_module("app.agents.agent4_visuals.subagents.storyboard")
agent4_flux = importlib.import_module("app.agents.agent4_visuals.services.flux_generator")
agent5_builder = importlib.import_module("app.agents.agent5_render.services.remotion_builder")
agent5_renderer = importlib.import_module("app.agents.agent5_render.services.renderer")
agent5_render_mod = importlib.import_module("app.agents.agent5_render.services.video")

check("1a: Agent 3 audio service imports", hasattr(agent3_audio, "run_audio_generation"))
check("1b: Agent 4 storyboard imports", hasattr(agent4_storyboard, "remap_beats_for_short"))
check("1c: Agent 4 Flux service imports", hasattr(agent4_flux, "generate_all_beat_images"))
check("1d: Agent 5 Remotion builder imports", hasattr(agent5_builder, "build_short_props"))
check("1e: Agent 5 renderer imports", hasattr(agent5_renderer, "render_short"))

print("\n── 2: Old package paths are not importable ──")
old_paths = [
    ".".join(("app", "agents", "agent4" + "_audio", "services", "audio")),
    ".".join(("app", "agents", "agent5" + "_video", "services", "video")),
    ".".join(("app", "agents", "agent5" + "_video", "subagents", "storyboard")),
]
for old_path in old_paths:
    try:
        importlib.import_module(old_path)
        old_importable = True
    except ModuleNotFoundError:
        old_importable = False
    check(f"2: {old_path} not importable", not old_importable)

print("\n── 3: Visual logic lives in Agent 4 package ──")
video_src = inspect.getsource(agent5_render_mod)
check("3a: Agent 5 render imports remap from Agent 4 visuals",
      "app.agents.agent4_visuals.subagents.storyboard" in video_src)
check("3b: Agent 5 render imports Flux from Agent 4 visuals",
      "app.agents.agent4_visuals.services.flux_generator" in video_src)
check("3c: remap_beats_for_short remains reachable from Agent 4 visuals",
      callable(agent4_storyboard.remap_beats_for_short))

print("\n── 4: Render logic lives in Agent 5 package ──")
check("4a: build_short_props remains reachable from Agent 5 render",
      callable(agent5_builder.build_short_props))
check("4b: render_short remains reachable from Agent 5 render",
      callable(agent5_renderer.render_short))
check("4c: parent render path still uses render_main_video",
      "render_main_video" in video_src)
check("4d: child render path still uses render_short",
      "render_short" in video_src)

print("\n── 5: Scheduler task names and compatibility aliases ──")
tasks = importlib.import_module("app.scheduler.tasks")
helper_src = inspect.getsource(tasks.ensure_child_short_audio_enqueued)
pickup_src = inspect.getsource(tasks.pickup_scripts_validated)
render_pickup_src = inspect.getsource(tasks.pickup_audio_done)
check("5a: new Agent 3 task exists", hasattr(tasks, "run_agent3_audio_for_content"))
check("5b: old Agent 4 task alias exists", hasattr(tasks, "run_agent4_for_content"))
check("5c: obsolete child audio helper is compatibility no-op",
      "Compatibility no-op" in helper_src and "return 0" in helper_src and
      "run_agent3_audio_for_content.delay" not in helper_src)
check("5d: pickup_scripts_validated enqueues new Agent 3 task",
      "run_agent3_audio_for_content.delay" in pickup_src)
check("5e: new Agent 5 render task exists", hasattr(tasks, "run_agent5_render_for_content"))
check("5f: old Agent 5 task alias exists", hasattr(tasks, "run_agent5_for_content"))
check("5g: pickup_audio_done enqueues new Agent 5 render task",
      "run_agent5_render_for_content.delay" in render_pickup_src)

print("\n── 6: Parent-cut short code remains absent ──")
check("6a: old shorts_cutter module path absent",
      importlib.util.find_spec("app.agents.agent5_render.subagents.shorts_cutter") is None)
run_renders_src = inspect.getsource(agent5_render_mod._run_renders)
run_short_src = inspect.getsource(agent5_render_mod._run_short_render)
check("6b: parent _run_renders does not call render_short",
      "render_short(" not in run_renders_src)
check("6c: child _run_short_render creates format=short",
      'format="short"' in run_short_src)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 3A agent boundaries")
