"""Smoke test — Phase 4D-D status & pickup alignment.

Verifies:
  1. Parent script -> child script dependency is enforced (run_shorts_planner
     requires a validated parent Script row before generating child scripts).
  2. Parent visuals -> child visuals dependency is enforced (Agent 4 gates
     child remap on the parent's persisted __visual__ VideoSection row).
  3. Agent 4 writes PARENT_VISUALS_DONE.
  4. Agent 4 writes CHILD_SHORT_VISUALS_DONE.
  5. Agent 5's pickup (pickup_visual_ready) consumes those statuses.
  6. Agent 5 writes RENDERED.
  7. No runtime path depends on VIDEO_DONE.
  8. No runtime path depends on GENERATING_VIDEO.
  9. pickup_visual_ready uses Content.status as the primary signal, not
     VideoSection existence (VideoSection is checked only defensively).

No live APIs, no DB, no Remotion render. Static/import checks only.
"""

import inspect
import importlib
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


script_service = importlib.import_module("app.agents.agent2_discovery.services.scripts")
orchestrator = importlib.import_module("app.agents.agent4_visuals.services.visual_orchestrator")
video_mod = importlib.import_module("app.agents.agent5_render.services.video")
tasks = importlib.import_module("app.scheduler.tasks")

print("\n── 1: Parent script -> child script dependency ──")
src_shorts_source = inspect.getsource(script_service._load_shorts_planner_source)
check("1a: run_shorts_planner's source loader requires a validated parent Script",
      "Script.validated.is_(True)" in src_shorts_source
      and "Script.content_id == long_content_id" in src_shorts_source)
check("1b: missing validated parent script aborts shorts planning",
      "no validated source script" in src_shorts_source)

print("\n── 2: Parent visuals -> child visuals dependency ──")
src_child_visuals = inspect.getsource(orchestrator._run_child_short_visuals)
check("2a: child visual readiness queries the parent's __visual__ VideoSection row",
      'VideoSection.content_id == parent_content_id' in src_child_visuals
      and 'VideoSection.language   == _VISUAL_LANGUAGE' in src_child_visuals)
check("2b: missing parent visuals defers the child rather than proceeding",
      "CHILD_SHORT_VISUALS_DEFERRED" in src_child_visuals
      and "parent_visual_ready" in src_child_visuals)

print("\n── 3/4: Agent 4 writes the visual-done statuses ──")
src_run_visual_generation_for_content = inspect.getsource(
    orchestrator.run_visual_generation_for_content
)
check("3: Agent 4 writes PARENT_VISUALS_DONE (returned status assigned to content.status)",
      "content.status = status" in src_run_visual_generation_for_content)
src_run_visual_generation = inspect.getsource(orchestrator.run_visual_generation)
src_parent_visuals = inspect.getsource(orchestrator._run_parent_visuals)
src_child_visuals_status = inspect.getsource(orchestrator._run_child_short_visuals)
check("3b: _run_parent_visuals returns status PARENT_VISUALS_DONE",
      '"status": "PARENT_VISUALS_DONE"' in src_parent_visuals)
check("4: _run_child_short_visuals returns status CHILD_SHORT_VISUALS_DONE",
      '"CHILD_SHORT_VISUALS_DONE"' in src_child_visuals_status)

print("\n── 5: Agent 5's pickup consumes Agent 4's visual-done statuses ──")
src_pickup_visual_ready = inspect.getsource(tasks.pickup_visual_ready)
check("5a: pickup_visual_ready filters on PARENT_VISUALS_DONE",
      '"PARENT_VISUALS_DONE"' in src_pickup_visual_ready)
check("5b: pickup_visual_ready filters on CHILD_SHORT_VISUALS_DONE",
      '"CHILD_SHORT_VISUALS_DONE"' in src_pickup_visual_ready)

print("\n── 6: Agent 5 writes RENDERED ──")
src_run_video_generation = inspect.getsource(video_mod.run_video_generation)
check("6a: run_video_generation sets content.status = RENDERED on success",
      'content.status = "RENDERED"' in src_run_video_generation)
check("6b: RENDER_DONE log accompanies the RENDERED transition",
      "RENDER_DONE" in src_run_video_generation)

print("\n── 7/8: No runtime path depends on VIDEO_DONE / GENERATING_VIDEO ──")
runtime_files = {
    "video.py": src_run_video_generation,
    "visual_orchestrator.py (run_visual_generation_for_content)":
        src_run_visual_generation_for_content,
    "tasks.py (pickup_audio_done)": inspect.getsource(tasks.pickup_audio_done),
    "tasks.py (run_agent4_visual_generation_for_content)":
        inspect.getsource(tasks.run_agent4_visual_generation_for_content),
    "tasks.py (pickup_visual_ready)": src_pickup_visual_ready,
    "tasks.py (run_agent5_render_for_content)":
        inspect.getsource(tasks.run_agent5_render_for_content),
}
for label, src in runtime_files.items():
    check(f"7/8: {label} does not reference VIDEO_DONE", "VIDEO_DONE" not in src)
    check(f"7/8: {label} does not reference GENERATING_VIDEO", "GENERATING_VIDEO" not in src)

print("\n── 9: pickup_visual_ready uses status as primary signal ──")
check("9a: pickup_visual_ready's Content query filters on status.in_(...)",
      "Content.status.in_" in src_pickup_visual_ready
      and "PARENT_VISUALS_DONE" in src_pickup_visual_ready)
check("9b: VideoSection existence is checked only as a defensive validation",
      "Defensive validation only" in src_pickup_visual_ready
      and "status_videosection_mismatch" in src_pickup_visual_ready)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4D-D status & pickup alignment")
