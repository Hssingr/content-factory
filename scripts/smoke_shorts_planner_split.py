"""Smoke test: Agent 2 shorts planner helper split.

Pure source/import checks only. No live APIs, DB writes, Remotion, or migrations.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


def function_line_count(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.end_lineno - node.lineno + 1
    raise AssertionError(f"{name} not found in {path}")


scripts = importlib.import_module("app.agents.agent2_discovery.services.scripts")
scripts_file = ROOT / "app" / "agents" / "agent2_discovery" / "services" / "scripts.py"

planner_src = inspect.getsource(scripts.run_shorts_planner)
module_src = inspect.getsource(scripts)
helper_names = [
    "_load_shorts_planner_source",
    "_load_short_source_voice",
    "_generate_shorts_plan_with_retry",
    "_child_shorts_already_exist",
    "_create_child_short_content",
    "_generate_validated_short_script",
    "_collect_short_script_major_issues",
    "_remove_failed_short_content",
    "_persist_child_short_script",
]
helper_src = "\n".join(inspect.getsource(getattr(scripts, name)) for name in helper_names)

print("\n-- Public entrypoint --")
check("run_shorts_planner still exists", callable(scripts.run_shorts_planner))
check("run_shorts_planner remains below 100 lines", function_line_count(scripts_file, "run_shorts_planner") < 100)
check("run_shorts_planner delegates to helpers", all(name in planner_src for name in [
    "_load_shorts_planner_source",
    "_generate_shorts_plan_with_retry",
    "_create_child_short_content",
    "_generate_validated_short_script",
    "_persist_child_short_script",
]))

print("\n-- Helpers exist --")
for helper_name in helper_names:
    check(f"helper exists: {helper_name}", callable(getattr(scripts, helper_name, None)))

print("\n-- Child content creation path --")
create_src = inspect.getsource(scripts._create_child_short_content)
check("child Content row is created", "Content(" in create_src)
check("child row is marked short episode", "is_short_episode=True" in create_src)
check("child row keeps parent_content_id", "parent_content_id=long_content_id" in create_src)
check("child row starts GENERATING_SCRIPTS", 'status="GENERATING_SCRIPTS"' in create_src)

print("\n-- Short script validation path --")
validation_src = inspect.getsource(scripts._generate_validated_short_script) + inspect.getsource(scripts._collect_short_script_major_issues)
check("short script generation still calls prompt entrypoint", "generate_short_episode_script(" in validation_src)
check("TTS compliance check remains", "check_tts_compliance(" in validation_src)
check("hook quality check remains", "check_hook_quality(" in validation_src)
check("word cap remains enforced", "_MAX_SHORT_WORDS" in validation_src and "script_too_long" in validation_src)
check("correction rounds remain bounded", "_MAX_SHORT_CORRECTION_ROUNDS + 2" in validation_src)

print("\n-- V2 parent dependency regression guards --")
for forbidden in [
    "SCRIPTS_VALIDATED_AWAITING_PARENT",
    "ensure_child_short_audio_enqueued",
    "pickup_short_episodes_awaiting_parent",
    "AUDIO_DONE",
    "semantic_splits",
    "generate_short_bookends",
    "breakpoints",
    "rehook",
    "bridge",
]:
    check(f"forbidden path absent: {forbidden}", forbidden not in planner_src + helper_src)

print("\n-- Import direction --")
check("Agent 2 scripts service does not import scheduler", "app.scheduler" not in module_src)
check("Agent 2 scripts service does not import Agent 3", "app.agents.agent3_" not in module_src)
check("Agent 2 scripts service does not import Agent 4", "app.agents.agent4_" not in module_src)
check("Agent 2 scripts service does not import Agent 5", "app.agents.agent5_" not in module_src)

if failures:
    print(f"\nSMOKE FAIL: {len(failures)}/{checks} failed")
    for failure in failures:
        print(f" - {failure}")
    raise SystemExit(1)

print(f"\nSMOKE PASS: {checks} checks")
