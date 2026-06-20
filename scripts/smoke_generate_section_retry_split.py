"""Smoke test: Agent 2 single-section retry helper split.

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

retry_src = inspect.getsource(scripts._generate_section_with_retry)
module_src = inspect.getsource(scripts)
section_prompt_src = inspect.getsource(system_prompt.generate_section)
helper_names = [
    "_log_section_retry_input",
    "_call_section_generation",
    "_log_section_generation_output",
    "_clean_generated_section",
    "_collect_section_retry_issues",
    "_log_section_cleanup",
    "_log_section_transition_issues",
    "_finalize_section_after_retry_limit",
    "_build_section_retry_instruction",
]
helper_src = "\n".join(inspect.getsource(getattr(scripts, name)) for name in helper_names)
combined_src = retry_src + "\n" + helper_src

print("\n-- Public internal helper --")
check("_generate_section_with_retry still exists", callable(scripts._generate_section_with_retry))
check(
    "_generate_section_with_retry is below 90 lines",
    function_line_count(scripts_file, "_generate_section_with_retry") < 90,
)
check("_generate_section_with_retry delegates to extracted helpers", all(name in retry_src for name in [
    "_log_section_retry_input",
    "_call_section_generation",
    "_log_section_generation_output",
    "_clean_generated_section",
    "_collect_section_retry_issues",
    "_finalize_section_after_retry_limit",
    "_build_section_retry_instruction",
]))

print("\n-- Helpers exist --")
for helper_name in helper_names:
    check(f"helper exists: {helper_name}", callable(getattr(scripts, helper_name, None)))

print("\n-- Retry behavior preserved --")
check("retry count expression unchanged", "range(1, _MAX_SECTION_RETRIES + 2)" in retry_src)
check("generation failure returns None only after retry limit", "if attempt > _MAX_SECTION_RETRIES:" in retry_src and "return None" in retry_src)
check("failed generation continues before retry limit", "continue" in retry_src)
check("successful no-major path returns result", 'if not issue_group["majors"]:' in retry_src and "return result" in retry_src)
check("final retry path uses final cleanup helper", "_finalize_section_after_retry_limit(" in retry_src)
check("retry override rebuilt before next attempt", "_build_section_retry_instruction(" in retry_src and "override =" in retry_src)

print("\n-- Claude generation path preserved --")
call_src = inspect.getsource(scripts._call_section_generation)
check("section generation still calls prompt entrypoint", "generate_section(" in call_src)
for marker in [
    "label=label",
    "story=story",
    "blueprint=blueprint",
    "prior_sections_summary=prior_sections_summary",
    "visual_intent_accumulator=visual_intent_accumulator",
    "channel=channel",
    "script_format=script_format",
    "tts_model=tts_model",
    "tts_provider=tts_provider",
    "audio_tags_enabled=audio_tags_enabled",
    "override_instruction=override",
    "primary_required_turn=primary_required_turn",
    "future_uncovered_turns=future_uncovered_turns",
]:
    check(f"generation argument preserved: {marker}", marker in call_src)
check("section_generation Claude task remains in system_prompt.generate_section", 'task="section_generation"' in section_prompt_src)
check("retry helpers do not call Claude client directly", "call_claude_structured(" not in combined_src)
check("retry helpers do not embed section system prompt", "_SECTION_GENERATION_SYSTEM_PROMPT" not in combined_src)

print("\n-- Deterministic checks preserved --")
check("TTS cleanup remains normalize then split", "normalize_tts_chars(script_text)" in combined_src and "split_long_sentences(cleaned)" in combined_src)
check("TTS compliance check remains", 'check_tts_compliance(script_text, \"source\")' in combined_src)
check("hook check remains conditional", 'check_hook_quality(script_text, \"source\") if check_hook else []' in combined_src)
check("transition check remains conditional", "check_section_transition(script_text, prior_summary_text)" in combined_src)
check("MAJOR issue filter remains", 'issue["severity"] == "MAJOR"' in combined_src)
check("final cleanup rechecks deterministic issues", "final_majors" in inspect.getsource(scripts._finalize_section_after_retry_limit))

print("\n-- Logging preserved --")
for marker in [
    "SECTION_INPUT label=%s attempt=%d prior_count=%d avoid_count=%d override=%s",
    "SECTION_OUTPUT label=%s attempt=%d words=%d sents=%d max_sent=%d suggests_outro=%s",
    "SECTION_OUTPUT label=%s first_sent=%r",
    "SECTION_CLEANUP label=%s attempt=%d backstop=%s words=%d→%d",
    "Section %s transition check [MINOR]: %s",
    "Section %s retry %d — issues: %s",
    "final deterministic cleanup resolved all MAJOR issues",
]:
    check(f"log marker preserved: {marker}", marker in combined_src)

print("\n-- Retry override preserved --")
build_src = inspect.getsource(scripts._build_section_retry_instruction)
check("override uses first three major descriptions", 'majors[:3]' in build_src and 'issue["description"]' in build_src)
check("override appends first transition issue", 'transition_issues[0]["description"]' in build_src)
check("override prefix unchanged", "Fix these issues from the previous attempt:" in build_src)

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
    check(f"forbidden retry path absent: {forbidden}", forbidden not in combined_src)

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
