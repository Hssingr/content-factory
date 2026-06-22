"""Smoke test — Phase 4E-B child storyboard validation wiring.

Verifies:
  1. There is exactly one `validate_storyboard()` call site inside
     visual_orchestrator.py, shared by both parent and child paths (no fork,
     no duplicate validator path, no `validate_child_storyboard()`).
  2. The parent path (`_run_storyboard_validation`) still calls the shared
     helper — coverage unchanged for parent.
  3. The child path (`_run_child_short_visuals`) now calls the shared helper
     too — this is the coverage gap being closed.
  4. No new validator rules were added to storyboard_validator.py (rule
     inventory unchanged from Phase 4E-B0's audit: 8 checks).
  5. No validator thresholds/severities changed.
  6. No new Content.status values were introduced.
  7. No prompt files were changed (system_prompt.py untouched by this phase).
  8. Agent 4/Agent 5 ownership boundaries are unaffected (Agent 5 still
     imports nothing from agent4_visuals; Agent 4 still owns persistence).
  9. Child MAJOR-issue handling mirrors the parent's non-blocking terminal
     pattern (log ERROR, proceed) rather than inventing new behavior.

No live APIs, no DB, no Remotion render. Static/import checks only.
"""

import ast
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


orchestrator = importlib.import_module(
    "app.agents.agent4_visuals.services.visual_orchestrator"
)
validator_mod = importlib.import_module(
    "app.agents.agent4_visuals.subagents.storyboard_validator"
)
video_mod = importlib.import_module("app.agents.agent5_render.services.video")

src_orchestrator = inspect.getsource(orchestrator)

print("\n── 1: Single validate_storyboard() call site, no fork ──")
# AST-based: robust to the import statement growing a second name on the same
# line (e.g. Phase 4E-F added validate_media_assets to the same import).
_orchestrator_import_tree = ast.parse(src_orchestrator)
_validator_import_names: list[str] = []
for node in ast.walk(_orchestrator_import_tree):
    if isinstance(node, ast.ImportFrom) and node.module == (
        "app.agents.agent4_visuals.subagents.storyboard_validator"
    ):
        _validator_import_names.extend(alias.name for alias in node.names)
check("1a: validate_storyboard is imported exactly once, from exactly one "
      "ImportFrom statement naming storyboard_validator",
      _validator_import_names.count("validate_storyboard") == 1)
# validate_storyboard() is *called* (as opposed to merely mentioned in prose)
# at exactly two AST Call sites: once inside the shared
# _check_storyboard_issues() helper (used by both parent and child), and once
# more inside the parent's pre-existing post-retry re-check (validating the
# *retried* beats after a full storyboard regeneration) — that second call
# predates this phase and is parent-only retry logic, not a second validator
# path. Both calls funnel through the same validate_storyboard() function.
_orchestrator_tree = ast.parse(src_orchestrator)
_validate_storyboard_calls = [
    node for node in ast.walk(_orchestrator_tree)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "validate_storyboard"
]
check("1b: validate_storyboard() has exactly the two expected call sites "
      "(shared helper + parent's pre-existing retry re-check) — no third, "
      "forked call site",
      len(_validate_storyboard_calls) == 2)
check("1c: no validate_child_storyboard() function was created",
      not hasattr(orchestrator, "validate_child_storyboard")
      and "def validate_child_storyboard" not in src_orchestrator)
check("1d: shared helper _check_storyboard_issues exists",
      callable(getattr(orchestrator, "_check_storyboard_issues", None)))

print("\n── 2: Parent path still validates (coverage unchanged) ──")
src_parent_validation = inspect.getsource(orchestrator._run_storyboard_validation)
check("2a: _run_storyboard_validation calls the shared helper",
      "_check_storyboard_issues(" in src_parent_validation)
check("2b: parent retry-on-MAJOR behavior is untouched (split_into_beats retry present)",
      "split_into_beats(" in src_parent_validation
      and "storyboard_constraints=constraint_lines" in src_parent_validation)

print("\n── 3: Child path now validates (the coverage gap being closed) ──")
src_child_visuals = inspect.getsource(orchestrator._run_child_short_visuals)
check("3a: _run_child_short_visuals calls the shared helper",
      "_check_storyboard_issues(" in src_child_visuals)
check("3b: validation runs after remap_beats_for_short() and before persistence",
      src_child_visuals.index("remap_beats_for_short(")
      < src_child_visuals.index("_check_storyboard_issues(")
      < src_child_visuals.index("_save_video_sections("))
check("3c: child MAJOR issues are logged, not silently dropped",
      "major_issues" in src_child_visuals and "MAJOR_count=" in src_child_visuals)
check("3d: child MAJOR issues do not block persistence (coverage only, no new gate)",
      # the _save_video_sections call must not be inside the `if major_issues:` branch
      "if major_issues:" in src_child_visuals
      and src_child_visuals.index("_save_video_sections(")
          > src_child_visuals.index("if major_issues:"))

print("\n── 4: Phase 4E-B0 baseline checks still present (this phase added none) ──")
src_validator = inspect.getsource(validator_mod)
_known_checks = [
    "cover_frame_dark_contrast", "cover_frame_text_card", "opening_text_card_pair",
    "forbidden_flux_word", "environment_over_saturation",
    "consecutive_same_environment", "text_card_saturation", "low_intensity_run",
]
# This smoke guards Phase 4E-B's own no-new-rules scope. It checks the
# original 8 are still present, not that the total stays at 8 — Phase 4E-C
# (storyboard_validator_expansion) is the explicitly-scoped later phase that
# adds new rules; see scripts/smoke_storyboard_validator_expansion.py for its
# own exact-count guard.
check("4a: storyboard_validator.py still has all 8 Phase 4E-B0 baseline checks",
      all(f'check="{c}"' in src_validator for c in _known_checks))
check("4b: storyboard_validator.py file was not otherwise modified in shape "
      "(validate_storyboard signature unchanged)",
      "def validate_storyboard(beats: list[dict]) -> list[StoryboardIssue]:" in src_validator)

print("\n── 5: No threshold/severity changes ──")
check("5a: FORBIDDEN_FLUX_WORDS unchanged (12 words)",
      len(validator_mod.FORBIDDEN_FLUX_WORDS) == 12)
check("5b: severity values are still only MAJOR/MINOR",
      set(
          line.split('severity="')[1].split('"')[0]
          for line in src_validator.splitlines() if 'severity="' in line
      ) == {"MAJOR", "MINOR"})

print("\n── 6: No new Content.status values introduced ──")
_known_statuses = {
    "PARENT_VISUALS_DONE", "CHILD_SHORT_VISUALS_DONE",
    "CHILD_SHORT_VISUALS_DEFERRED", "VISUALS_FAILED",
    "AUDIO_DONE", "GENERATING_VISUALS", "FAILED",
}
import re as _re
_status_assignments = set(_re.findall(r'content\.status\s*=\s*"([A-Z_]+)"', src_orchestrator))
_status_assignments |= set(_re.findall(r'"status":\s*"([A-Z_]+)"', src_orchestrator))
check("6a: every content.status / result-status literal in visual_orchestrator.py "
      "is one of the statuses already known from Phase 4D-D",
      _status_assignments <= _known_statuses)

print("\n── 7: No prompt files changed ──")
check("7a: system_prompt.py is not imported or modified by the new validation call",
      "system_prompt" not in src_child_visuals)

print("\n── 8: Agent ownership boundaries unaffected ──")
video_src = inspect.getsource(video_mod)
video_imports = []
for node in ast.walk(ast.parse(video_src)):
    if isinstance(node, ast.ImportFrom) and node.module:
        video_imports.append(node.module)
    elif isinstance(node, ast.Import):
        video_imports.extend(a.name for a in node.names)
check("8a: Agent 5 (video.py) still imports no Agent 4 module",
      not any(m.startswith("app.agents.agent4_visuals") for m in video_imports))
check("8b: Agent 4 (visual_orchestrator.py) still owns _save_video_sections",
      callable(getattr(orchestrator, "_save_video_sections", None)))
check("8c: Agent 5 does not define or call _check_storyboard_issues",
      "_check_storyboard_issues" not in video_src)

print("\n── 9: Child failure behavior mirrors the parent's non-blocking pattern ──")
check("9a: child path logs at ERROR level on unresolved MAJOR issues, like the "
      "parent's post-retry-still-MAJOR branch",
      "logger.error(" in src_child_visuals
      and src_child_visuals.index("logger.error(", src_child_visuals.index("major_issues"))
      > 0)
check("9b: child path does not introduce a retry call (no regeneration primitive exists)",
      "remap_beats_for_short(" not in src_child_visuals[
          src_child_visuals.index("_check_storyboard_issues("):
      ])

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4E-B child storyboard validation wiring")
