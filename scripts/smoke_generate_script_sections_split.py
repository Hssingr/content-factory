"""Smoke test: Agent 2 generate_script_sections helper split.

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
system_prompt = importlib.import_module("app.agents.agent2_discovery.system_prompt")
scripts_file = ROOT / "app" / "agents" / "agent2_discovery" / "services" / "scripts.py"

public_src = inspect.getsource(scripts.generate_script_sections)
module_src = inspect.getsource(scripts)
section_prompt_src = inspect.getsource(system_prompt.generate_section)
helper_names = [
    "_build_section_generation_context",
    "_create_section_loop_state",
    "_log_blueprint_summary",
    "_append_generated_section",
    "_select_required_turns",
    "_generate_intro_section",
    "_credit_body_turn_coverage",
    "_should_stop_body_loop",
    "_run_body_section_loop",
    "_generate_outro_section",
    "_log_outro_overlap",
    "_assemble_sections_with_diagnostics",
    "_apply_length_correction",
    "_log_turn_coverage_alignment",
    "_group_narrative_retry_instructions",
    "_run_single_narrative_retry",
    "_clean_narrative_retry_text",
    "_log_post_retry_narrative_result",
    "_run_narrative_completeness_retry",
]
helper_src = "\n".join(inspect.getsource(getattr(scripts, name)) for name in helper_names)
section_src = public_src + "\n" + helper_src

print("\n-- Public entrypoint --")
check("generate_script_sections still exists", callable(scripts.generate_script_sections))
check(
    "generate_script_sections remains below 120 lines",
    function_line_count(scripts_file, "generate_script_sections") < 120,
)
check("generate_script_sections delegates to extracted helpers", all(name in public_src for name in [
    "_build_section_generation_context",
    "_generate_intro_section",
    "_run_body_section_loop",
    "_generate_outro_section",
    "_assemble_sections_with_diagnostics",
    "_run_narrative_completeness_retry",
]))

print("\n-- Helpers exist --")
for helper_name in helper_names:
    check(f"helper exists: {helper_name}", callable(getattr(scripts, helper_name, None)))

print("\n-- Section generation path --")
check("single-section retry helper still exists", callable(scripts._generate_section_with_retry))
check("intro generation uses retry helper", "_generate_section_with_retry(" in inspect.getsource(scripts._generate_intro_section))
check("body generation uses retry helper", "_generate_section_with_retry(" in inspect.getsource(scripts._run_body_section_loop))
check("outro generation uses retry helper", "_generate_section_with_retry(" in inspect.getsource(scripts._generate_outro_section))
check("narrative retry still calls prompt entrypoint directly", "generate_section(" in inspect.getsource(scripts._run_single_narrative_retry))

print("\n-- Turn coverage and loop policy --")
check("required/future turn selection remains", "future_turns" in inspect.getsource(scripts._select_required_turns))
check("turn matching remains in append helper", "_match_turns(" in inspect.getsource(scripts._append_generated_section))
check("over-compressed turn guard remains", "over-compressed major turns" in inspect.getsource(scripts._credit_body_turn_coverage))
check("hard cap remains enforced", "_MAX_BODY_SECTIONS" in inspect.getsource(scripts._should_stop_body_loop))
check("soft max extension remains", "soft_max_but_uncovered_turns" in inspect.getsource(scripts._should_stop_body_loop))
check("narrative coverage alignment remains", "TURN_COVERAGE_SOURCE" in inspect.getsource(scripts._log_turn_coverage_alignment))
check("narrative retry remains bounded by grouped section targets", "section_instructions" in inspect.getsource(scripts._run_narrative_completeness_retry))

print("\n-- Post assembly validation path --")
check("repetition diagnostics remain", "diagnose_section_repetition(" in section_src)
check("generic phrase scan remains", "detect_generic_documentary_phrases(" in section_src)
check("minimum length check remains", "check_minimum_length(" in section_src)
check(
    "global validation moved to run_script_quality_gate (Phase 10A-0) — no longer called here",
    "validate_script_globally(" not in section_src,
)
check("narrative completeness remains", "check_narrative_completeness(" in section_src)

print("\n-- Prompt ownership --")
check("section_generation Claude task remains in system_prompt.generate_section", 'task="section_generation"' in section_prompt_src)
check("split helpers do not embed section system prompt", "_SECTION_GENERATION_SYSTEM_PROMPT" not in section_src)
check("split helpers do not call Claude client directly", "call_claude_structured(" not in section_src)

print("\n-- Legacy parent-short regression guards --")
for forbidden in [
    "SCRIPTS_VALIDATED_AWAITING_PARENT",
    "ensure_child_short_audio_enqueued",
    "pickup_short_episodes_awaiting_parent",
    "semantic_splits",
    "generate_short_bookends",
    "cut_shorts",
    "shorts_breakpoints",
    "short_rehook_paths",
    "short_bridge_paths",
]:
    check(f"forbidden section path absent: {forbidden}", forbidden not in section_src)

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
