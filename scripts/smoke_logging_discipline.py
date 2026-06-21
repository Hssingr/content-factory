"""Static smoke for Phase 3E-B logging discipline."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def source(path: str) -> str:
    return (ROOT / path).read_text()


def parse(path: str) -> ast.Module:
    text = source(path)
    return ast.parse(text, filename=path)


def logger_calls(path: str) -> list[tuple[str, str]]:
    tree = parse(path)
    calls: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {"debug", "info", "warning", "error"}:
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "logger":
            continue
        msg = ""
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            msg = node.args[0].value
        calls.append((func.attr, msg))
    return calls


def assert_marker_level(path: str, marker: str, level: str) -> None:
    matches = [(lvl, msg) for lvl, msg in logger_calls(path) if marker in msg]
    check(bool(matches), f"missing logger marker {marker!r} in {path}")
    check(any(lvl == level for lvl, _ in matches), f"{marker!r} in {path} is not logged at {level}")


def assert_no_info_marker(path: str, marker: str) -> None:
    matches = [(lvl, msg) for lvl, msg in logger_calls(path) if marker in msg]
    check(bool(matches), f"missing diagnostic marker {marker!r} in {path}")
    check(all(lvl != "info" for lvl, _ in matches), f"{marker!r} still has logger.info in {path}")


def assert_no_forbidden_imports(path: str) -> None:
    tree = parse(path)
    forbidden = (
        "app.agents.agent3_audio",
        "app.agents.agent4_visuals",
        "app.agents.agent5_render",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            check(not module.startswith(forbidden), f"forbidden import in {path}: {module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                check(not alias.name.startswith(forbidden), f"forbidden import in {path}: {alias.name}")


changed_python = [
    "app/agents/agent2_discovery/services/scripts.py",
    "app/agents/agent2_discovery/services/discovery.py",
    "app/agents/agent2_discovery/services/scoring.py",
    "app/agents/agent2_discovery/services/validation.py",
    "app/agents/agent2_discovery/services/script_workflow.py",
    "app/agents/agent2_discovery/system_prompt.py",
    "app/agents/agent3_audio/services/audio.py",
    "app/agents/agent3_audio/services/storage.py",
    "app/agents/agent3_audio/services/tts.py",
    "app/agents/agent4_visuals/services/flux_generator.py",
    "app/agents/agent4_visuals/services/visual_orchestrator.py",
    "app/agents/agent4_visuals/subagents/storyboard.py",
    "app/agents/agent5_render/services/renderer.py",
    "app/agents/agent5_render/services/subtitles.py",
    "app/agents/agent5_render/services/video.py",
    "app/services/claude_client.py",
    "app/services/script_checks.py",
]
for changed in changed_python:
    parse(changed)

for marker in [
    "SCRIPT_TRACE",
    "FINAL_TTS_BACKSTOP",
    "QUALITY_GATE_INPUT",
    "QUALITY_GATE_BREAKDOWN",
    "TURN_MATCH",
    "SECTION_INPUT",
    "SECTION_OUTPUT",
    "SECTION_CLEANUP",
    "LOOP_DECISION",
    "TURN_COVERAGE_SOURCE",
    "TURN_COVERAGE_FINAL",
    "NARRATIVE_RETRY",
    "REPETITION",
    "OUTRO_OVERLAP",
]:
    assert_no_info_marker("app/agents/agent2_discovery/services/scripts.py", marker)

assert_no_info_marker("app/agents/agent3_audio/services/audio.py", "Processing lang=")
assert_no_info_marker("app/agents/agent3_audio/services/storage.py", "Audio saved:")
assert_no_info_marker("app/agents/agent3_audio/services/tts.py", "TTS prepare:")
assert_no_info_marker("app/agents/agent4_visuals/services/flux_generator.py", "Flux beat=%d content=%s")
assert_no_info_marker("app/agents/agent4_visuals/subagents/storyboard.py", "STORYBOARD_ESTIMATE script_words")
assert_no_info_marker("app/agents/agent5_render/services/renderer.py", "Chunked render enabled")
assert_no_info_marker("app/agents/agent5_render/services/subtitles.py", "Karaoke subtitles:")
assert_no_info_marker("app/agents/agent5_render/services/video.py", "Agent5 [PRE_RENDER]")
assert_no_info_marker("app/services/claude_client.py", "call_claude start:")
assert_no_info_marker("app/services/claude_client.py", "call_claude_structured start:")

assert_marker_level("app/agents/agent3_audio/services/audio.py", "PARENT_AUDIO_START", "info")
assert_marker_level("app/agents/agent3_audio/services/audio.py", "PARENT_AUDIO_DONE", "info")
assert_marker_level("app/agents/agent3_audio/services/audio.py", "CHILD_SHORT_AUDIO_START", "info")
assert_marker_level("app/agents/agent3_audio/services/audio.py", "CHILD_SHORT_AUDIO_DONE", "info")
assert_marker_level("app/agents/agent4_visuals/services/visual_orchestrator.py", "PARENT_VISUALS_START", "info")
assert_marker_level("app/agents/agent4_visuals/services/visual_orchestrator.py", "PARENT_VISUALS_DONE", "info")
assert_marker_level("app/agents/agent4_visuals/services/visual_orchestrator.py", "CHILD_SHORT_VISUALS_DEFERRED", "warning")
assert_marker_level("app/agents/agent4_visuals/services/visual_orchestrator.py", "CHILD_SHORT_VISUALS_START", "info")
assert_marker_level("app/agents/agent4_visuals/services/visual_orchestrator.py", "CHILD_SHORT_VISUALS_DONE", "info")
assert_marker_level("app/agents/agent5_render/services/video.py", "CHILD_SHORT_RENDER_START", "info")
assert_marker_level("app/agents/agent5_render/services/video.py", "CHILD_SHORT_RENDER_DONE", "info")
assert_marker_level("app/scheduler/tasks.py", "AUDIO_PICKUP", "info")

assert_no_forbidden_imports("app/agents/agent2_discovery/services/scripts.py")

live_text = "\n".join(source(path) for path in changed_python)
for legacy in [
    "SCRIPTS_VALIDATED_AWAITING_PARENT",
    "shorts_breakpoints",
    "short_rehook_paths",
    "short_bridge_paths",
    "generate_short_bookends",
]:
    check(legacy not in live_text, f"legacy marker returned: {legacy}")

print(f"SMOKE PASS: {CHECKS} checks")
