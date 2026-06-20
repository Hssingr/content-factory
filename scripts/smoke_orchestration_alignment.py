"""Smoke test: V2 standalone-short orchestration alignment.

Pure source-inspection smoke. It must not call live APIs, render, run migrations,
or touch the database.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.scheduler import celery_app
from app.scheduler import tasks
from app.agents.agent2_discovery.services import scripts as script_service
from app.agents.agent3_audio.services import audio as audio_service
from app.agents.agent5_render.services import video as video_service

checks = 0
failures: list[str] = []


def check(name: str, condition: bool) -> None:
    global checks
    checks += 1
    if condition:
        print(f"PASS {checks:02d}: {name}")
    else:
        print(f"FAIL {checks:02d}: {name}")
        failures.append(name)


src_short_planner = "\n".join([
    inspect.getsource(script_service.run_shorts_planner),
    inspect.getsource(script_service._create_child_short_content),
    inspect.getsource(script_service._generate_validated_short_script),
    inspect.getsource(script_service._collect_short_script_major_issues),
    inspect.getsource(script_service._persist_child_short_script),
])
src_tasks = inspect.getsource(tasks)
src_pickup_scripts = inspect.getsource(tasks.pickup_scripts_validated)
src_parent_gate_task = inspect.getsource(tasks.pickup_short_episodes_awaiting_parent)
src_parent_gate_helper = inspect.getsource(tasks.ensure_child_short_audio_enqueued)
src_run_agent3 = inspect.getsource(tasks.run_agent3_audio_for_content)
src_audio = inspect.getsource(audio_service.run_audio_generation)
src_video_generation = inspect.getsource(video_service.run_video_generation)
src_visual_pass = inspect.getsource(video_service._run_visual_pass)
src_process_language = inspect.getsource(video_service._process_language)
src_run_short_render = inspect.getsource(video_service._run_short_render)
src_remap = inspect.getsource(video_service.remap_beats_for_short)
src_pickup_audio_done = inspect.getsource(tasks.pickup_audio_done)
scheduler_init = (ROOT / "app" / "scheduler" / "__init__.py").read_text(encoding="utf-8")

print("\n-- Agent 2 child status --")
check(
    "child short script is saved as SCRIPTS_VALIDATED directly",
    'short_content.status = "SCRIPTS_VALIDATED"' in src_short_planner,
)
check(
    "short planner does not write SCRIPTS_VALIDATED_AWAITING_PARENT",
    "SCRIPTS_VALIDATED_AWAITING_PARENT" not in src_short_planner,
)

print("\n-- Agent 3 audio pickup --")
check(
    "pickup_scripts_validated dispatches all SCRIPTS_VALIDATED content",
    'Content.status == "SCRIPTS_VALIDATED"' in src_pickup_scripts
    and "is_short_episode.is_(False)" not in src_pickup_scripts
    and "is_short_episode.is_(True)" not in src_pickup_scripts,
)
check("AUDIO_PICKUP log includes is_short_episode", "AUDIO_PICKUP content_id=%s is_short_episode=%s" in src_pickup_scripts)
check("CHILD_SHORT_AUDIO_START log includes parent_content_id", "CHILD_SHORT_AUDIO_START content_id=%s parent_content_id=%s" in src_audio)
check("CHILD_SHORT_AUDIO_DONE log includes content_id and duration_ms", "CHILD_SHORT_AUDIO_DONE content_id=%s duration_ms=%d" in src_audio)

print("\n-- Parent audio no longer releases children --")
check("parent gate task is a compatibility no-op", "Compatibility no-op" in src_parent_gate_task and "return 0" in src_parent_gate_task)
check("parent gate task does not enqueue Agent 3", "run_agent3_audio_for_content.delay" not in src_parent_gate_task)
check("parent gate helper is a compatibility no-op", "Compatibility no-op" in src_parent_gate_helper and "return 0" in src_parent_gate_helper)
check("parent gate helper does not mutate child status", ".status =" not in src_parent_gate_helper)
check("run_agent3_audio_for_content does not call child release helper", "ensure_child_short_audio_enqueued(" not in src_run_agent3)
check("obsolete parent-gate Beat schedule removed", "pickup-short-episodes-awaiting-parent" not in scheduler_init)

print("\n-- Agent 4 visual readiness --")
check("parent visuals start log present", "PARENT_VISUALS_START content_id=%s" in src_visual_pass)
check("parent visuals done log present", "PARENT_VISUALS_DONE content_id=%s" in src_visual_pass)
check("child visuals defer when parent visuals are missing", "CHILD_SHORT_VISUALS_DEFERRED content_id=%s reason=parent_visuals_missing" in src_video_generation)
check("child visuals start after parent visuals exist", "CHILD_SHORT_VISUALS_START content_id=%s parent_content_id=%s" in src_video_generation)
check("child reuse stats log present", "CHILD_SHORT_REUSE_STATS content_id=%s" in src_remap)

print("\n-- Agent 5 render readiness --")
check("render pickup requires AudioFile", "from app.models import AudioFile, Content, VideoRender" in src_pickup_audio_done and "reason=audio_missing" in src_pickup_audio_done)
check("render pickup skips existing VideoRender", "VideoRender.content_id == content.id" in src_pickup_audio_done and "reason=render_exists" in src_pickup_audio_done)
check("parent render uses main format", 'render_fmt = "short" if is_short_episode else "main"' in src_process_language and 'format="main"' in src_process_language)
check("child render uses short format", 'format="short"' in src_run_short_render and "render_short(" in src_run_short_render and "_run_short_render(" in src_process_language)

print("\n-- Removed parent-cut short paths stay removed --")
check("run_audio_generation has no semantic_splits", "semantic_splits" not in src_audio)
check("run_audio_generation has no bookend generator", "generate_short_bookends" not in src_audio)
check("render orchestration does not call cut_shorts", "cut_shorts(" not in src_video_generation and "cut_shorts(" not in src_process_language)
check("child props do not include rehook or bridge paths", "rehook_paths" not in src_process_language and "bridge_paths" not in src_process_language)

if failures:
    print(f"\nSMOKE FAIL: {len(failures)}/{checks} failed")
    for failure in failures:
        print(f" - {failure}")
    raise SystemExit(1)

print(f"\nSMOKE PASS: {checks} checks")
