"""Smoke test — Phase 4D-C Agent 5 render-only boundary.

Verifies:
  1. Agent 5 (video.py) imports no Agent 4 module at all.
  2. Agent 5 does not call run_visual_generation() or any Agent 4 entrypoint.
  3. Agent 5 does not persist VideoSection rows (read-only access only).
  4. Agent 4 owns the visual generation entrypoint
     (run_visual_generation_for_content) and VideoSection persistence.
  5. The render path requires existing VideoSection rows before rendering a
     language (defers rather than generating them or falling back to Agent 4).
  6. The render path requires an existing AudioFile before rendering a
     language.
  7. Scheduler wiring: Agent 4's pickup (pickup_audio_done) and Agent 5's
     pickup (pickup_visual_ready) are separate tasks, and Agent 5's render
     task is only ever reached through pickup_visual_ready / direct dispatch
     — never through a call out of Agent 4's task.

No live APIs, no DB, no Remotion render. Static/import checks only.
"""

import ast
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


def imported_modules(src: str) -> list[str]:
    tree = ast.parse(src)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


video_mod = importlib.import_module("app.agents.agent5_render.services.video")
orchestrator = importlib.import_module(
    "app.agents.agent4_visuals.services.visual_orchestrator"
)
tasks = importlib.import_module("app.scheduler.tasks")

video_src = inspect.getsource(video_mod)
video_imports = imported_modules(video_src)

print("\n── 1: Agent 5 imports no Agent 4 module ──")
agent4_imports = [m for m in video_imports if m.startswith("app.agents.agent4_visuals")]
check("1a: no import statement in video.py names an Agent 4 module",
      not agent4_imports)
check("1b: video.py module docstring states Agent 5 never calls Agent 4",
      "never calls Agent 4" in (video_mod.__doc__ or ""))

print("\n── 2: Agent 5 does not call any Agent 4 entrypoint ──")
check("2a: run_video_generation does not call run_visual_generation(...)",
      "run_visual_generation(" not in inspect.getsource(video_mod.run_video_generation))
check("2b: run_video_generation does not call run_visual_generation_for_content(...)",
      "run_visual_generation_for_content(" not in inspect.getsource(video_mod.run_video_generation))
check("2c: no function in video.py is named after an Agent 4 helper",
      not any(name in dir(video_mod) for name in (
          "run_visual_generation", "run_visual_generation_for_content",
          "remap_beats_for_short", "split_into_beats", "validate_storyboard",
          "generate_all_beat_images",
      )))

print("\n── 3: Agent 5 does not persist VideoSection rows ──")
check("3a: _save_video_sections not defined in video.py",
      not hasattr(video_mod, "_save_video_sections"))
check("3b: video.py has a read-only VideoSection loader instead",
      callable(getattr(video_mod, "_load_video_sections", None)))
load_src = inspect.getsource(video_mod._load_video_sections)
check("3c: the read-only loader does not call db.add/db.query(...).delete()",
      "db.add(" not in load_src and ".delete()" not in load_src)

print("\n── 4: Agent 4 owns the visual entrypoint and persistence ──")
check("4a: run_visual_generation_for_content exists in Agent 4",
      callable(getattr(orchestrator, "run_visual_generation_for_content", None)))
check("4b: run_visual_generation exists in Agent 4",
      callable(getattr(orchestrator, "run_visual_generation", None)))
check("4c: _save_video_sections lives in Agent 4",
      callable(getattr(orchestrator, "_save_video_sections", None)))

print("\n── 5: Render path requires existing VideoSection rows ──")
src_run_video_generation = inspect.getsource(video_mod.run_video_generation)
check("5a: run_video_generation loads VideoSection rows via _load_video_sections",
      "_load_video_sections(" in src_run_video_generation)
check("5b: missing VideoSection rows defer rather than fail or fall back",
      "RENDER_DEFERRED" in src_run_video_generation
      and "reason=visual_sections_missing" in src_run_video_generation)
check("5c: no Agent 4 fallback path exists in run_video_generation",
      "split_into_beats(" not in src_run_video_generation
      and "remap_beats_for_short(" not in src_run_video_generation)

print("\n── 6: Render path requires an existing AudioFile ──")
check("6a: run_video_generation loads AudioFile rows before rendering",
      "audio_by_lang" in src_run_video_generation and "AudioFile" in video_src)
check("6b: a language with no AudioFile is skipped, not rendered",
      'audio = audio_by_lang.get(language)' in src_run_video_generation)

print("\n── 7: Scheduler wiring keeps Agent 4 and Agent 5 pickups separate ──")
visual_pickup_src = inspect.getsource(tasks.pickup_audio_done)
render_pickup_src = inspect.getsource(tasks.pickup_visual_ready)
agent4_task_src = inspect.getsource(tasks.run_agent4_visual_generation_for_content)
agent5_task_src = inspect.getsource(tasks.run_agent5_render_for_content)
check("7a: pickup_audio_done dispatches the Agent 4 visual task",
      "run_agent4_visual_generation_for_content.delay" in visual_pickup_src)
check("7b: pickup_visual_ready dispatches the Agent 5 render task",
      "run_agent5_render_for_content.delay" in render_pickup_src)
check("7c: pickup_visual_ready gates on VideoSection rows (language != '__visual__')",
      'VideoSection.language != "__visual__"' in render_pickup_src)
check("7d: Agent 4's Celery task does not call Agent 5's render task",
      "run_agent5_render_for_content" not in agent4_task_src)
check("7e: Agent 5's Celery task does not call Agent 4's visual task",
      "run_agent4_visual_generation_for_content.delay" not in agent5_task_src
      and "run_agent4_visual_generation_for_content.run(" not in agent5_task_src
      and "run_visual_generation_for_content(" not in agent5_task_src)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4D-C Agent 5 render-only boundary")
