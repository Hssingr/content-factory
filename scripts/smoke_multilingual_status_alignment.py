"""Smoke test: multilingual script status alignment.

Pure source/import checks only. No live APIs, DB writes, Remotion, or migrations.
"""

from __future__ import annotations

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


def app_sources() -> str:
    parts: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


scripts = importlib.import_module("app.agents.agent2_discovery.services.scripts")
workflow = importlib.import_module("app.agents.agent2_discovery.services.script_workflow")
tasks = importlib.import_module("app.scheduler.tasks")

src_app = app_sources()
src_multilingual = inspect.getsource(scripts.generate_multilingual_scripts)
src_required_langs = inspect.getsource(scripts._required_script_languages)
src_mark_validated = inspect.getsource(scripts._mark_script_validated)
src_workflow = inspect.getsource(workflow.run_script_workflow)
src_planner = inspect.getsource(scripts.run_shorts_planner)
src_child_persist = inspect.getsource(scripts._persist_child_short_script)
src_source_loader = inspect.getsource(scripts._load_shorts_planner_source)
src_pickup = inspect.getsource(tasks.pickup_scripts_validated)
claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

print("\n-- SCRIPTS_READY removed from live app code --")
check("no live app code writes SCRIPTS_READY", 'status = "SCRIPTS_READY"' not in src_app and 'status="SCRIPTS_READY"' not in src_app)
check("no live app code reads SCRIPTS_READY", '== "SCRIPTS_READY"' not in src_app and 'Content.status == "SCRIPTS_READY"' not in src_app)

print("\n-- Parent completion contract --")
check("multilingual helper requires a validated source script", "Script.validated.is_(True)" in src_multilingual)
check("multilingual helper computes required language set", "_required_script_languages(" in src_multilingual and "ChannelLanguage" in src_required_langs)
check("multilingual helper validates required scripts", "_mark_script_validated(" in src_multilingual and "script.validated = True" in src_mark_validated)
check("multilingual helper fails incomplete required script sets", "Multilingual script set incomplete" in src_multilingual and 'content.status = "FAILED"' in src_multilingual)
check("workflow stops before SCRIPTS_VALIDATED when script set incomplete", "if not required_scripts" in src_workflow and "script set incomplete" in src_workflow)
check("parent SCRIPTS_VALIDATED transition happens after multilingual generation", src_workflow.index("generate_multilingual_scripts(") < src_workflow.index('content.status = "SCRIPTS_VALIDATED"'))
check("run_shorts_planner starts after parent SCRIPTS_VALIDATED", src_workflow.index('content.status = "SCRIPTS_VALIDATED"') < src_workflow.index("run_shorts_planner("))

print("\n-- Child short completion contract --")
check("child source short script is persisted before child multilingual generation", src_child_persist.index("db.add(short_script)") < src_child_persist.index("generate_multilingual_scripts("))
check("child short calls multilingual generation before SCRIPTS_VALIDATED", src_child_persist.index("generate_multilingual_scripts(") < src_child_persist.index('short_content.status = "SCRIPTS_VALIDATED"'))
check("child short stops before SCRIPTS_VALIDATED when script set incomplete", "if not required_scripts" in src_child_persist and "script set incomplete" in src_child_persist)
check("child short passes channel context into multilingual generation", "channel=channel" in src_planner and "audio_tags_enabled=config.audio_tags_enabled" in src_planner)

print("\n-- Dependency preservation --")
check("short planner still requires validated parent source script", "Script.validated.is_(True)" in src_source_loader and "no validated source script" in src_source_loader)
check("Agent 3 pickup still only uses SCRIPTS_VALIDATED", 'Content.status == "SCRIPTS_VALIDATED"' in src_pickup)

print("\n-- CLAUDE.md contract --")
for marker in [
    "required source-language script exists",
    "all required multilingual scripts exist",
    "SCRIPTS_READY",
    "Agent 3 pickup must see only fully completed script sets",
]:
    check(f"CLAUDE.md documents: {marker}", marker in claude)

if failures:
    print(f"\nSMOKE FAIL: {len(failures)}/{checks} failed")
    for failure in failures:
        print(f" - {failure}")
    raise SystemExit(1)

print(f"\nSMOKE PASS: {checks} checks")
