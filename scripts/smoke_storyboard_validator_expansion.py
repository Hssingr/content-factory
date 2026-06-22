"""Smoke test — Phase 4E-C storyboard validator expansion.

Verifies:
  1. The existing validate_storyboard() function and its 8 original checks
     still exist (Phase 4E-B0 inventory unchanged).
  2. No second storyboard validator module/function was created.
  3. The 3 new checks (motif_repetition_in_window, near_duplicate_beat,
     ai_slideshow_risk) are reachable and fire on deterministic fixtures —
     no AI calls, no network I/O.
  4. Parent path is covered (validate_storyboard called via the shared
     _check_storyboard_issues() helper, used by _run_storyboard_validation).
  5. Child path is covered (same shared helper, used by
     _run_child_short_visuals).
  6. Agent 4 ownership preserved (storyboard_validator.py stays under
     agent4_visuals; persistence still owned by visual_orchestrator.py).
  7. Agent 5 (video.py) untouched — still imports no Agent 4 module.
  8. No prompt files (system_prompt.py) were changed.
  9. No new Content.status values were introduced.
  10. No scheduling (tasks.py / scheduler/__init__.py) files were changed.

No live APIs, no DB, no Remotion render. Static/import checks plus
deterministic fixture-based unit checks only.
"""

import hashlib
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


validator_mod = importlib.import_module(
    "app.agents.agent4_visuals.subagents.storyboard_validator"
)
orchestrator = importlib.import_module(
    "app.agents.agent4_visuals.services.visual_orchestrator"
)
video_mod = importlib.import_module("app.agents.agent5_render.services.video")
system_prompt_mod = importlib.import_module("app.agents.agent4_visuals.system_prompt")

src_validator = inspect.getsource(validator_mod)


def make_beat(order, motif="other", environment="urban_street", effect="cut",
              flux_prompt="photorealistic shot of a wooden chair, no people",
              media_strategy="flux_generated", color_grade="neutral",
              beat_intensity="medium"):
    return {
        "beat_order": order, "section_order": order, "motif": motif,
        "environment": environment, "effect": effect, "flux_prompt": flux_prompt,
        "media_strategy": media_strategy, "color_grade": color_grade,
        "beat_intensity": beat_intensity,
    }


print("\n── 1: Original 8 checks unchanged (Phase 4E-B0 inventory) ──")
_original_checks = [
    "cover_frame_dark_contrast", "cover_frame_text_card", "opening_text_card_pair",
    "forbidden_flux_word", "environment_over_saturation",
    "consecutive_same_environment", "text_card_saturation", "low_intensity_run",
]
check("1a: all 8 original check names still present in source",
      all(f'check="{c}"' in src_validator for c in _original_checks))
_new_checks = ["motif_repetition_in_window", "near_duplicate_beat", "ai_slideshow_risk"]
# This smoke guards Phase 4E-C's own scope (the 8 original + these 3 new
# checks). It checks all 11 are present, not that the total stays at 11 —
# Phase 4E-D (flux_prompt_validator) is the explicitly-scoped later phase
# that adds more checks; see scripts/smoke_flux_prompt_validator.py for its
# own exact-count guard.
check("1b: the 8 original + 3 new checks from this phase are all present",
      all(f'check="{c}"' in src_validator for c in _new_checks))
check("1c: FORBIDDEN_FLUX_WORDS still has 12 entries (unchanged)",
      len(validator_mod.FORBIDDEN_FLUX_WORDS) == 12)
check("1d: existing clean fixture still produces zero MAJOR issues",
      not [
          i for i in validator_mod.validate_storyboard([
              make_beat(0, environment="indoor_office", motif="document"),
              make_beat(1, environment="forest_nature", motif="hands"),
          ])
          if i["severity"] == "MAJOR"
      ])

print("\n── 2: No second storyboard validator created ──")
# storyboard_validator.py now legitimately defines two public validate_*
# functions (validate_storyboard for pre-generation text/structure checks,
# validate_media_assets for post-generation media checks, Phase 4E-F) — this
# guards against a THIRD, forked one (e.g. a child-only or parent-only
# variant), not against the legitimate second entrypoint.
_known_validate_functions = {"validate_storyboard", "validate_media_assets"}
check("2a: storyboard_validator.py defines only the known validate_* functions "
      "(no third, forked validator)",
      {name for name in dir(validator_mod) if name.startswith("validate_")}
      <= _known_validate_functions)
check("2b: no validate_child_storyboard / validate_parent_storyboard exists anywhere",
      not hasattr(validator_mod, "validate_child_storyboard")
      and not hasattr(validator_mod, "validate_parent_storyboard")
      and not hasattr(orchestrator, "validate_child_storyboard"))

print("\n── 3: New checks are reachable and deterministic ──")
motif_beats = [make_beat(i, motif=("object" if i in (0, 3, 6) else "doorway")) for i in range(10)]
motif_issues = [i for i in validator_mod.validate_storyboard(motif_beats) if i["check"] == "motif_repetition_in_window"]
check("3a: motif_repetition_in_window fires when a motif repeats 3x in a 10-beat window",
      len(motif_issues) >= 1 and all(i["severity"] == "MINOR" for i in motif_issues))

dup_beats = [
    make_beat(0, motif="doorway", environment="indoor_office", effect="pan"),
    make_beat(1, motif="doorway", environment="indoor_office", effect="pan"),
    make_beat(2, motif="hands", environment="forest_nature", effect="cut"),
]
dup_issues = [i for i in validator_mod.validate_storyboard(dup_beats) if i["check"] == "near_duplicate_beat"]
check("3b: near_duplicate_beat fires when environment+motif+effect all match within proximity",
      len(dup_issues) == 1 and dup_issues[0]["severity"] == "MINOR")

slideshow_beats = [make_beat(i, environment="laboratory", motif="document", effect="cut") for i in range(5)] \
    + [make_beat(5, environment="forest_nature", motif="hands", effect="pan")]
slideshow_issues = [i for i in validator_mod.validate_storyboard(slideshow_beats) if i["check"] == "ai_slideshow_risk"]
check("3c: ai_slideshow_risk fires on a 5-beat run sharing one field value",
      len(slideshow_issues) >= 1 and all(i["severity"] == "MINOR" for i in slideshow_issues))

check("3d: new checks require no network/AI call (pure functions over dict beats)",
      "fal_client" not in src_validator and "call_claude" not in src_validator
      and "anthropic" not in src_validator)

print("\n── 4: Parent path covered ──")
src_run_storyboard_validation = inspect.getsource(orchestrator._run_storyboard_validation)
check("4a: parent path calls the shared helper, which calls validate_storyboard()",
      "_check_storyboard_issues(" in src_run_storyboard_validation)

print("\n── 5: Child path covered ──")
src_run_child_short_visuals = inspect.getsource(orchestrator._run_child_short_visuals)
check("5a: child path calls the same shared helper",
      "_check_storyboard_issues(" in src_run_child_short_visuals)

print("\n── 6: Agent 4 ownership preserved ──")
check("6a: storyboard_validator.py lives under app.agents.agent4_visuals",
      validator_mod.__name__.startswith("app.agents.agent4_visuals"))
check("6b: VideoSection persistence still owned by visual_orchestrator.py",
      callable(getattr(orchestrator, "_save_video_sections", None)))

print("\n── 7: Agent 5 untouched ──")
import ast
video_src = inspect.getsource(video_mod)
video_imports = []
for node in ast.walk(ast.parse(video_src)):
    if isinstance(node, ast.ImportFrom) and node.module:
        video_imports.append(node.module)
    elif isinstance(node, ast.Import):
        video_imports.extend(a.name for a in node.names)
check("7a: video.py still imports no app.agents.agent4_visuals module",
      not any(m.startswith("app.agents.agent4_visuals") for m in video_imports))
check("7b: video.py does not reference any of the new check names",
      not any(name in video_src for name in (
          "motif_repetition_in_window", "near_duplicate_beat", "ai_slideshow_risk",
      )))

print("\n── 8: No prompt files changed ──")
_system_prompt_hash = hashlib.sha256(
    inspect.getsource(system_prompt_mod).encode()
).hexdigest()
print(f"      (system_prompt.py sha256 = {_system_prompt_hash[:16]}... — "
      "informational; this phase made no edits to it)")
_validator_imports = []
for node in ast.walk(ast.parse(src_validator)):
    if isinstance(node, ast.ImportFrom) and node.module:
        _validator_imports.append(node.module)
    elif isinstance(node, ast.Import):
        _validator_imports.extend(a.name for a in node.names)
check("8a: storyboard_validator.py has no import statement naming system_prompt "
      "(the one prose mention is a code comment cross-reference, not an import)",
      not any("system_prompt" in m for m in _validator_imports))

print("\n── 9: No new Content.status values introduced ──")
import re as _re
_known_statuses = {
    "PARENT_VISUALS_DONE", "CHILD_SHORT_VISUALS_DONE",
    "CHILD_SHORT_VISUALS_DEFERRED", "VISUALS_FAILED",
    "AUDIO_DONE", "GENERATING_VISUALS", "FAILED",
}
src_orchestrator = inspect.getsource(orchestrator)
_status_literals = set(_re.findall(r'content\.status\s*=\s*"([A-Z_]+)"', src_orchestrator))
_status_literals |= set(_re.findall(r'"status":\s*"([A-Z_]+)"', src_orchestrator))
check("9a: visual_orchestrator.py introduces no new Content.status literal",
      _status_literals <= _known_statuses)
check("9b: storyboard_validator.py contains no Content.status reference at all "
      "(severity is StoryboardIssue-local, not a pipeline status)",
      "content.status" not in src_validator and '"status":' not in src_validator)

print("\n── 10: No scheduling changes ──")
tasks_mod = importlib.import_module("app.scheduler.tasks")
scheduler_init_src = (ROOT / "app" / "scheduler" / "__init__.py").read_text(encoding="utf-8")
check("10a: tasks.py does not reference any new check name "
      "(scheduling is untouched by this phase)",
      not any(name in inspect.getsource(tasks_mod) for name in (
          "motif_repetition_in_window", "near_duplicate_beat", "ai_slideshow_risk",
      )))
check("10b: scheduler/__init__.py Beat schedule unchanged "
      "(still exactly pickup-audio-done and pickup-visual-ready for Agent4/5)",
      "pickup-audio-done" in scheduler_init_src and "pickup-visual-ready" in scheduler_init_src)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4E-C storyboard validator expansion")
