"""Smoke test: Agent 2 script workflow boundary split.

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


tasks = importlib.import_module("app.scheduler.tasks")
workflow = importlib.import_module("app.agents.agent2_discovery.services.script_workflow")

task_src = inspect.getsource(tasks.run_agent2_scripts_for_content)
workflow_src = inspect.getsource(workflow)
tasks_file = ROOT / "app" / "scheduler" / "tasks.py"
workflow_file = ROOT / "app" / "agents" / "agent2_discovery" / "services" / "script_workflow.py"

print("\n-- Scheduler delegation --")
check("scheduler task delegates to Agent 2 workflow service", "run_script_workflow(content, db)" in task_src)
check("scheduler task remains under 80 lines", function_line_count(tasks_file, "run_agent2_scripts_for_content") < 80)
check("scheduler keeps task retry behavior", "self.retry(exc=exc)" in task_src and "MaxRetriesExceededError" in task_src)
check("scheduler keeps status guard", 'content.status != "APPROVED"' in task_src)

print("\n-- Workflow service ownership --")
check("workflow service exists", hasattr(workflow, "run_script_workflow"))
check("workflow service owns blueprint generation", "generate_story_blueprint(" in workflow_src)
check("workflow service owns section generation", "generate_script_sections(" in workflow_src)
check("workflow service owns quality gate", "run_script_quality_gate(" in workflow_src)
check("workflow service owns multilingual generation", "generate_multilingual_scripts(" in workflow_src)
check("workflow service owns shorts planner", "run_shorts_planner(" in workflow_src)
check("workflow service public method remains below 80 lines", function_line_count(workflow_file, "run_script_workflow") < 80)

print("\n-- Behavior path preserved --")
for marker in [
    'content.status = "GENERATING_SCRIPTS"',
    'content.status = "SCRIPTS_VALIDATED"',
    "tasks_post_quality_gate",
    "tasks_entering_multilingual",
    "Script hook (first 300 chars)",
    "run_shorts_planner failed",
]:
    check(f"preserves marker: {marker}", marker in workflow_src)

print("\n-- Import direction --")
check("workflow service does not import scheduler", "app.scheduler" not in workflow_src)
check("scheduler imports workflow service", "services.script_workflow import run_script_workflow" in task_src)

if failures:
    print(f"\nSMOKE FAIL: {len(failures)}/{checks} failed")
    for failure in failures:
        print(f" - {failure}")
    raise SystemExit(1)

print(f"\nSMOKE PASS: {checks} checks")
