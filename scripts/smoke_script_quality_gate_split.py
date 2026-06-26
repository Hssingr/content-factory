"""Smoke test: Agent 2 script quality gate helper split.

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

gate_src = inspect.getsource(scripts.run_script_quality_gate)
module_src = inspect.getsource(scripts)
helper_names = [
    "_apply_final_tts_backstop",
    "_run_global_script_validation",
    "_log_quality_gate_input",
    "_collect_quality_gate_issues",
    "_log_quality_gate_review",
    "_has_tts_only_high_issues",
    "_apply_tts_only_quality_cleanup",
    "_apply_post_rewrite_cleanup",
    "_apply_final_quality_cleanup",
]
helper_src = "\n".join(inspect.getsource(getattr(scripts, name)) for name in helper_names)
combined_src = gate_src + "\n" + helper_src

print("\n-- Public entrypoint --")
check("run_script_quality_gate still exists", callable(scripts.run_script_quality_gate))
check("run_script_quality_gate remains below 100 lines", function_line_count(scripts_file, "run_script_quality_gate") < 100)
check("run_script_quality_gate delegates to helpers", all(name in gate_src for name in [
    "_apply_final_tts_backstop",
    "_run_global_script_validation",
    "_log_quality_gate_input",
    "_collect_quality_gate_issues",
    "_log_quality_gate_review",
    "_apply_tts_only_quality_cleanup",
    "_apply_post_rewrite_cleanup",
    "_apply_final_quality_cleanup",
]))

print("\n-- Helpers exist --")
for helper_name in helper_names:
    check(f"helper exists: {helper_name}", callable(getattr(scripts, helper_name, None)))

print("\n-- Validation path preserved --")
check("Claude assessment remains", "assess_script_quality(" in combined_src)
check("TTS deterministic check remains", "check_tts_compliance(" in combined_src)
check("hook deterministic check remains", "check_hook_quality(" in combined_src)
check("deterministic MAJOR issues convert to HIGH", '"severity": "HIGH"' in combined_src and '"fix": issue["suggestion"]' in combined_src)
check("pass condition preserved", 'issue_group["status"] == "PASSED"' in gate_src and 'not issue_group["converted_det"]' in gate_src)
check("pass condition also requires no global-validation issues (Phase 10A-0)", 'not issue_group["global"]' in gate_src)

print("\n-- Global validation wiring (Phase 10A-0) --")
check("global validation moved here from generate_script_sections", "validate_script_globally(" in combined_src)
check("global validation runs once, fed only into attempt 1", "global_issues if attempt == 1 else []" in gate_src)
check("global validation result persisted to ContentValidation", "script_validation_status" in helper_src and "script_issues_log" in helper_src)
check("global-validation issues tagged for the rewrite merge", '"category": "global_narrative"' in helper_src)

print("\n-- Correction and retry path preserved --")
check("rewrite path exists", "rewrite_script_for_quality(" in gate_src)
check("rewrite count increments", "rewrite_calls += 1" in gate_src)
check("retry count unchanged", "range(1, _MAX_QUALITY_REWRITES + 1)" in gate_src)
check("TTS-only rewrite skip remains", "QUALITY_REWRITE_SKIPPED reason=TTS_ONLY" in combined_src)
check("post-rewrite cleanup remains", "_apply_post_rewrite_cleanup(current, attempt)" in gate_src)
check("final cleanup remains", "_apply_final_quality_cleanup(current)" in gate_src)

print("\n-- Logs and cost telemetry preserved --")
for marker in [
    "FINAL_TTS_BACKSTOP",
    "QUALITY_GATE_INPUT",
    "Script Quality Gate: claude=%s det_major=%d issues=%d (high=%d) attempt=%d",
    "QUALITY_GATE_BREAKDOWN",
    "QUALITY_REWRITE_SCHEMA_OK",
    "QUALITY_REWRITE_JSON_FAIL",
    "quality_gate_max_retries_return",
    "_emit_script_cost_estimate",
]:
    check(f"preserves marker: {marker}", marker in combined_src)

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
