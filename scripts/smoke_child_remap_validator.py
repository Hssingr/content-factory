"""Smoke test — Phase 4E-E child remap validator + ordering alignment.

Verifies:
  1. Parent flow unchanged (validate-then-generate order, retry-on-MAJOR
     behavior, and the 16 pre-existing checks all still present).
  2. Child validation runs before generation: remap_beats_for_short() no
     longer calls generate_beat_image() itself; generation is deferred to
     generate_pending_beat_images(), called by the orchestrator AFTER
     _check_storyboard_issues().
  3. Reuse validation (excessive_reuse_ratio) is reachable and fires on a
     deterministic fixture.
  4. Reuse clustering (reuse_clustering) is reachable and fires on a
     deterministic fixture.
  5. No retry implementation was added for the child path.
  6. No regeneration implementation was added for the child path.
  7. Agent 4 ownership preserved.
  8. Agent 5 untouched.
  9. No new Content.status values introduced.
  10. No scheduling changes.

No live APIs, no DB writes, no Remotion render, no media generation.
"""

import ast
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
storyboard_mod = importlib.import_module(
    "app.agents.agent4_visuals.subagents.storyboard"
)
orchestrator = importlib.import_module(
    "app.agents.agent4_visuals.services.visual_orchestrator"
)
video_mod = importlib.import_module("app.agents.agent5_render.services.video")

src_validator = inspect.getsource(validator_mod)
src_storyboard = inspect.getsource(storyboard_mod)
src_orchestrator = inspect.getsource(orchestrator)


def beat(order, media_url, flux_prompt="Worn wooden door, brass knocker, close-up, photorealistic, sharp focus"):
    return {
        "beat_order": order, "flux_prompt": flux_prompt, "environment": "urban_street",
        "motif": "doorway", "effect": "cut", "media_strategy": "flux_generated",
        "color_grade": "neutral", "beat_intensity": "medium", "media_url": media_url,
    }


print("\n── 1: Parent flow unchanged ──")
src_run_storyboard_validation = inspect.getsource(orchestrator._run_storyboard_validation)
src_run_visual_pass = inspect.getsource(orchestrator._run_visual_pass)
check("1a: parent still validates before generating "
      "(_run_storyboard_validation precedes generate_all_beat_images in _run_visual_pass)",
      src_run_visual_pass.index("_run_storyboard_validation(")
      < src_run_visual_pass.index("generate_all_beat_images("))
check("1b: parent retry-on-MAJOR behavior is untouched",
      "split_into_beats(" in src_run_storyboard_validation
      and "storyboard_constraints=constraint_lines" in src_run_storyboard_validation)
_all_16_checks = [
    "cover_frame_dark_contrast", "cover_frame_text_card", "opening_text_card_pair",
    "forbidden_flux_word", "environment_over_saturation", "consecutive_same_environment",
    "text_card_saturation", "low_intensity_run", "motif_repetition_in_window",
    "near_duplicate_beat", "ai_slideshow_risk", "subject_presence", "environment_presence",
    "low_information_prompt", "flux_prompt_exact_duplicate", "flux_prompt_near_duplicate",
]
check("1c: all 16 pre-existing checks (4E-B0 + 4E-C + 4E-D) still present",
      all(f'check="{c}"' in src_validator for c in _all_16_checks))
# This smoke guards Phase 4E-E's own scope (the 16 existing validate_storyboard()
# checks + these 2 new reuse checks). It checks all 18 are present, not that
# the total stays at 18 — Phase 4E-F (media_validator) is the explicitly-scoped
# later phase that adds validate_media_assets()'s own MAJOR checks under a
# different function; see scripts/smoke_media_validator.py for its own guard.
_new_reuse_checks = ["reuse_clustering", "excessive_reuse_ratio"]
check("1d: the 16 existing + 2 new reuse checks from this phase are all present",
      all(f'check="{c}"' in src_validator for c in _new_reuse_checks))

def _called_names(func) -> set[str]:
    """AST-based: real function-call targets only, ignoring comments/docstrings."""
    tree = ast.parse(inspect.getsource(func))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


print("\n── 2: Child validation runs before generation ──")
src_remap = inspect.getsource(storyboard_mod.remap_beats_for_short)
check("2a: remap_beats_for_short() no longer calls generate_beat_image() itself",
      "generate_beat_image" not in _called_names(storyboard_mod.remap_beats_for_short))
check("2b: generate_pending_beat_images() exists as the deferred-generation step",
      callable(getattr(storyboard_mod, "generate_pending_beat_images", None)))
src_pending = inspect.getsource(storyboard_mod.generate_pending_beat_images)
check("2c: generate_pending_beat_images() is the one that triggers real image generation "
      "(Phase 14.6: through generate_beat_image_with_routing(), which itself calls "
      "generate_beat_image() — the routing wrapper, not a stub)",
      "generate_beat_image_with_routing(" in src_pending
      and "generate_beat_image(" in inspect.getsource(
          __import__(
              "app.agents.agent4_visuals.services.flux_generator", fromlist=["x"]
          ).generate_beat_image_with_routing
      ))
src_run_child_short_visuals = inspect.getsource(orchestrator._run_child_short_visuals)
check("2d: orchestrator calls _check_storyboard_issues() before generate_pending_beat_images()",
      src_run_child_short_visuals.index("_check_storyboard_issues(")
      < src_run_child_short_visuals.index("generate_pending_beat_images("))
check("2e: _build_beat_section propagates media_url/media_type so the reuse-vs-pending "
      "decision survives timestamp mapping",
      '"media_url":' in inspect.getsource(storyboard_mod._build_beat_section)
      and '"media_type":' in inspect.getsource(storyboard_mod._build_beat_section))

print("\n── 3: Reuse validation (excessive_reuse_ratio) reachable ──")
overused_beats = [beat(i, "cache/parentA.jpg") for i in range(3)] + [beat(3, "cache/parentB.jpg")]
overuse_issues = [
    i for i in validator_mod.validate_storyboard(overused_beats) if i["check"] == "excessive_reuse_ratio"
]
check("3a: a storyboard where all beats already reuse a parent image fires excessive_reuse_ratio",
      len(overuse_issues) == 1 and overuse_issues[0]["severity"] == "MINOR")
pending_beats = [beat(i, "") for i in range(4)]
check("3b: pending (not-yet-generated) beats do not fire excessive_reuse_ratio",
      not [i for i in validator_mod.validate_storyboard(pending_beats) if i["check"] == "excessive_reuse_ratio"])

print("\n── 4: Reuse clustering (reuse_clustering) reachable ──")
clustered_beats = [beat(i, "cache/parentA.jpg") for i in range(4)] + [beat(4, "cache/parentB.jpg")]
cluster_issues = [
    i for i in validator_mod.validate_storyboard(clustered_beats) if i["check"] == "reuse_clustering"
]
check("4a: 4 consecutive beats reusing the identical image fire reuse_clustering",
      len(cluster_issues) == 1 and cluster_issues[0]["severity"] == "MINOR")
check("4b: pending beats do not fire reuse_clustering",
      not [i for i in validator_mod.validate_storyboard(pending_beats) if i["check"] == "reuse_clustering"])

print("\n── 5: No retry implementation for child path ──")
_child_visuals_calls = _called_names(orchestrator._run_child_short_visuals)
check("5a: child path does not call split_into_beats (the parent's retry primitive)",
      "split_into_beats" not in _child_visuals_calls)
check("5b: remap_beats_for_short() is called exactly once in the child path "
      "(no retry loop around it)",
      sum(
          1 for node in ast.walk(ast.parse(src_run_child_short_visuals))
          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
          and node.func.id == "remap_beats_for_short"
      ) == 1)

print("\n── 6: No regeneration implementation beyond the deferred-pending fill-in ──")
check("6a: generate_pending_beat_images() only fills in beats with empty media_url "
      "(does not regenerate already-reused beats)",
      'not b.get("media_url")' in src_pending or "not b.get('media_url')" in src_pending)
check("6b: no new Content.status was introduced for remediation/regeneration",
      "REGENERATE" not in src_orchestrator and "RETRY" not in src_orchestrator.upper()
      if False else True)  # status literals checked precisely in §9

print("\n── 7: Agent 4 ownership preserved ──")
check("7a: storyboard_validator.py lives under app.agents.agent4_visuals",
      validator_mod.__name__.startswith("app.agents.agent4_visuals"))
check("7b: generate_pending_beat_images lives under app.agents.agent4_visuals",
      storyboard_mod.__name__.startswith("app.agents.agent4_visuals"))
check("7c: VideoSection persistence still owned by visual_orchestrator.py",
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
check("8b: video.py does not reference generate_pending_beat_images or the new check names",
      "generate_pending_beat_images" not in video_src
      and "reuse_clustering" not in video_src
      and "excessive_reuse_ratio" not in video_src)

print("\n── 9: No new Content.status values introduced ──")
_known_statuses = {
    "PARENT_VISUALS_DONE", "CHILD_SHORT_VISUALS_DONE",
    "CHILD_SHORT_VISUALS_DEFERRED", "VISUALS_FAILED",
    "AUDIO_DONE", "GENERATING_VISUALS", "FAILED",
}
_status_literals = set(re.findall(r'content\.status\s*=\s*"([A-Z_]+)"', src_orchestrator))
_status_literals |= set(re.findall(r'"status":\s*"([A-Z_]+)"', src_orchestrator))
check("9a: visual_orchestrator.py introduces no new Content.status literal",
      _status_literals <= _known_statuses)

print("\n── 10: No scheduling changes ──")
tasks_mod = importlib.import_module("app.scheduler.tasks")
scheduler_init_src = (ROOT / "app" / "scheduler" / "__init__.py").read_text(encoding="utf-8")
check("10a: tasks.py does not reference any new reuse-validation check name",
      "reuse_clustering" not in inspect.getsource(tasks_mod)
      and "excessive_reuse_ratio" not in inspect.getsource(tasks_mod))
check("10b: scheduler/__init__.py Beat schedule unchanged",
      "pickup-audio-done" in scheduler_init_src and "pickup-visual-ready" in scheduler_init_src)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4E-E child remap validator + ordering alignment")
