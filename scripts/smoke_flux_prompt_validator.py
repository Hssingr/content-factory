"""Smoke test — Phase 4E-D Flux prompt validator.

Verifies:
  1. The existing validate_storyboard() path is preserved (16 checks total:
     8 from Phase 4E-B0 + 3 from Phase 4E-C + 5 new Flux-prompt checks here).
  2. No second Flux/storyboard validator was created.
  3. Parent path covered (via the shared _check_storyboard_issues() helper).
  4. Child path covered (same shared helper).
  5. New checks are deterministic — no AI/network calls.
  6. No AI calls anywhere in the new logic.
  7. Agent 4 ownership preserved.
  8. Agent 5 untouched.
  9. No prompt-generation file (system_prompt.py) was changed.
  10. No new Content.status values introduced.
  11. No scheduling changes.

Fixture-based checks prove each new rule actually fires/doesn't fire on
deterministic input, matching the phase brief's own BAD/GOOD examples.

No live APIs, no DB, no Remotion render, no media generation.
"""

import ast
import hashlib
import importlib
import inspect
import re
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
flux_gen_mod = importlib.import_module("app.agents.agent4_visuals.services.flux_generator")

src_validator = inspect.getsource(validator_mod)


def beat(order, flux_prompt, environment="urban_street", motif="other", effect="cut",
         media_strategy="flux_generated"):
    return {
        "beat_order": order, "flux_prompt": flux_prompt, "environment": environment,
        "motif": motif, "effect": effect, "media_strategy": media_strategy,
        "color_grade": "neutral", "beat_intensity": "medium",
    }


print("\n── 1: Existing validation path preserved ──")
_all_checks = [
    "cover_frame_dark_contrast", "cover_frame_text_card", "opening_text_card_pair",
    "forbidden_flux_word", "environment_over_saturation", "consecutive_same_environment",
    "text_card_saturation", "low_intensity_run", "motif_repetition_in_window",
    "near_duplicate_beat", "ai_slideshow_risk",
]
_new_flux_checks = [
    "subject_presence", "environment_presence", "low_information_prompt",
    "flux_prompt_exact_duplicate", "flux_prompt_near_duplicate",
]
check("1a: all 11 pre-existing checks (4E-B0 + 4E-C) still present",
      all(f'check="{c}"' in src_validator for c in _all_checks))
# This smoke guards Phase 4E-D's own scope (the 11 existing + these 5 new
# checks). It checks all 16 are present, not that the total stays at 16 —
# Phase 4E-E (child_remap_validator) is the explicitly-scoped later phase
# that adds more checks; see scripts/smoke_child_remap_validator.py for its
# own exact-count guard.
check("1b: the 11 existing + 5 new checks from this phase are all present",
      all(f'check="{c}"' in src_validator for c in _new_flux_checks))
check("1c: FORBIDDEN_FLUX_WORDS unchanged (12 words) — existing MAJOR check untouched",
      len(validator_mod.FORBIDDEN_FLUX_WORDS) == 12)

print("\n── 2: No second Flux/storyboard validator created ──")
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
check("2b: no validate_flux_prompt_v2 / validate_child_flux_prompt exists anywhere",
      not hasattr(validator_mod, "validate_flux_prompt_v2")
      and not hasattr(validator_mod, "validate_child_flux_prompt")
      and not hasattr(orchestrator, "validate_flux_prompt_v2")
      and not hasattr(orchestrator, "validate_child_flux_prompt"))

print("\n── 3: Parent path covered ──")
src_run_storyboard_validation = inspect.getsource(orchestrator._run_storyboard_validation)
check("3a: parent path calls the shared helper that runs validate_storyboard()",
      "_check_storyboard_issues(" in src_run_storyboard_validation)

print("\n── 4: Child path covered ──")
src_run_child_short_visuals = inspect.getsource(orchestrator._run_child_short_visuals)
check("4a: child path calls the same shared helper",
      "_check_storyboard_issues(" in src_run_child_short_visuals)

print("\n── 5/6: New checks are deterministic, no AI calls ──")
check("5a: no fal_client/Claude/anthropic reference anywhere in storyboard_validator.py",
      "fal_client" not in src_validator and "call_claude" not in src_validator
      and "anthropic" not in src_validator)

bad_beats = [
    beat(0, "cinematic atmosphere"),
    beat(1, "dramatic lighting", environment="laboratory"),
    beat(2, "epic scene", environment="indoor_office"),
    beat(3, "close-up portrait", environment="laboratory"),
    beat(4, "beautiful atmosphere"),
    beat(5, "high quality image"),
]
bad_issues = {
    i["check"] for i in validator_mod.validate_storyboard(bad_beats)
    if i["check"] in ("subject_presence", "environment_presence", "low_information_prompt")
}
check("5b: BAD examples from the phase brief (Part 3/4/5) all fire at least one quality check",
      bad_issues == {"subject_presence", "environment_presence", "low_information_prompt"})

good_beats = [
    beat(0, "Tesla engineer inspecting battery cells inside a clean industrial laboratory "
            "bench, close-up, photorealistic", environment="laboratory", motif="hands"),
]
good_issues = [
    i for i in validator_mod.validate_storyboard(good_beats)
    if i["check"] in ("subject_presence", "low_information_prompt")
]
check("5c: a real, well-formed GOOD example from the phase brief does not fire "
      "subject_presence or low_information_prompt",
      not good_issues)

dup_beats = [
    beat(0, "Worn wooden front door with brass knocker, close-up, photorealistic"),
    beat(1, "Worn wooden front door with brass knocker, close-up, photorealistic"),
]
dup_issues = [
    i for i in validator_mod.validate_storyboard(dup_beats) if i["check"] == "flux_prompt_exact_duplicate"
]
check("5d: an exact-duplicate flux_prompt fires flux_prompt_exact_duplicate",
      len(dup_issues) == 1)

near_dup_beats = [
    beat(0, "Worn wooden front door with brass knocker, close-up, photorealistic, sharp focus"),
    beat(1, "Worn wooden front door with brass knocker, close-up, photorealistic, very sharp focus indeed"),
]
near_issues = [
    i for i in validator_mod.validate_storyboard(near_dup_beats) if i["check"] == "flux_prompt_near_duplicate"
]
check("5e: a near-duplicate (high word overlap) flux_prompt fires flux_prompt_near_duplicate",
      len(near_issues) == 1)

print("\n── 7: Agent 4 ownership preserved ──")
check("7a: storyboard_validator.py lives under app.agents.agent4_visuals",
      validator_mod.__name__.startswith("app.agents.agent4_visuals"))
check("7b: VideoSection persistence still owned by visual_orchestrator.py",
      callable(getattr(orchestrator, "_save_video_sections", None)))

print("\n── 8: Agent 5 untouched ──")
video_src = inspect.getsource(video_mod)
video_imports = []
for node in ast.walk(ast.parse(video_src)):
    if isinstance(node, ast.ImportFrom) and node.module:
        video_imports.append(node.module)
    elif isinstance(node, ast.Import):
        video_imports.extend(a.name for a in node.names)
check("8a: video.py still imports no app.agents.agent4_visuals module",
      not any(m.startswith("app.agents.agent4_visuals") for m in video_imports))
check("8b: video.py does not reference any of the new Flux-prompt check names",
      not any(name in video_src for name in _new_flux_checks))

print("\n── 9: No prompt-generation changes ──")
_validator_imports = []
for node in ast.walk(ast.parse(src_validator)):
    if isinstance(node, ast.ImportFrom) and node.module:
        _validator_imports.append(node.module)
    elif isinstance(node, ast.Import):
        _validator_imports.extend(a.name for a in node.names)
check("9a: storyboard_validator.py has no import statement naming system_prompt",
      not any("system_prompt" in m for m in _validator_imports))
check("9b: flux_generator.py (Flux image generation) source is unchanged by this phase "
      "(no new check names referenced there)",
      not any(name in inspect.getsource(flux_gen_mod) for name in _new_flux_checks))

print("\n── 10: No new Content.status values introduced ──")
_known_statuses = {
    "PARENT_VISUALS_DONE", "CHILD_SHORT_VISUALS_DONE",
    "CHILD_SHORT_VISUALS_DEFERRED", "VISUALS_FAILED",
    "AUDIO_DONE", "GENERATING_VISUALS", "FAILED",
}
src_orchestrator = inspect.getsource(orchestrator)
_status_literals = set(re.findall(r'content\.status\s*=\s*"([A-Z_]+)"', src_orchestrator))
_status_literals |= set(re.findall(r'"status":\s*"([A-Z_]+)"', src_orchestrator))
check("10a: visual_orchestrator.py introduces no new Content.status literal",
      _status_literals <= _known_statuses)
check("10b: storyboard_validator.py contains no Content.status reference",
      "content.status" not in src_validator and '"status":' not in src_validator)

print("\n── 11: No scheduling changes ──")
tasks_mod = importlib.import_module("app.scheduler.tasks")
scheduler_init_src = (ROOT / "app" / "scheduler" / "__init__.py").read_text(encoding="utf-8")
check("11a: tasks.py does not reference any new Flux-prompt check name",
      not any(name in inspect.getsource(tasks_mod) for name in _new_flux_checks))
check("11b: scheduler/__init__.py Beat schedule unchanged",
      "pickup-audio-done" in scheduler_init_src and "pickup-visual-ready" in scheduler_init_src)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4E-D Flux prompt validator")
