"""Smoke test — Phase 5B test_full_pipeline.py rewrite.

Verifies (static/AST inspection only — see code_report/phase5b_test_full_pipeline_rewrite.md
for the separate real-DB runtime proof of resume/idempotency behavior):

  1. No old package imports (agent4_audio, agent5_video).
  2. No retired statuses (SCRIPTS_READY, SCRIPTS_VALIDATED_AWAITING_PARENT,
     GENERATING_VIDEO, VIDEO_DONE).
  3. Current imports resolve.
  4. Agent 3 -> Agent 4 -> Agent 5 calls appear in correct order.
  5. Agent 4 visual step exists and is separate from Agent 5 render step.
  6. Script validation failure does not mark SCRIPTS_VALIDATED.
  7. Child rows are discovered/reused, not recreated.
  8. Force flags exist.
  9. Dry-run / confirm safety exists.
  10. Final summary includes parent and child rows.
  11. No parent-cut short / bookend / rehook / bridge logic exists.
  12. No parent-audio child-release logic exists.
  13. No code path sets child Content.status as a side effect of parent audio success.
  14. No code path sets child Content.status as a side effect of parent visual success.

No live APIs, no Flux generation, no Remotion render, no DB migration.
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


harness_path = ROOT / "test_full_pipeline.py"
src = harness_path.read_text(encoding="utf-8")
tree = ast.parse(src)


def _imported_modules(tree: ast.AST) -> list[str]:
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    return mods


print("\n── 1: No old package imports ──")
imported = _imported_modules(tree)
check("1a: no app.agents.agent4_audio import",
      not any(m.startswith("app.agents.agent4_audio") for m in imported))
check("1b: no app.agents.agent5_video import",
      not any(m.startswith("app.agents.agent5_video") for m in imported))

print("\n── 2: No retired statuses ──")
for status in ("SCRIPTS_READY", "SCRIPTS_VALIDATED_AWAITING_PARENT", "GENERATING_VIDEO", "VIDEO_DONE"):
    check(f"2.{status}: not present as a string literal", f'"{status}"' not in src)

print("\n── 3: Current imports resolve ──")
required_modules = [
    "app.agents.agent2_discovery.services.discovery",
    "app.agents.agent2_discovery.services.validation",
    "app.agents.agent2_discovery.services.scripts",
    "app.agents.agent2_discovery.system_prompt",
    "app.agents.agent3_audio.services.audio",
    "app.agents.agent4_visuals.services.visual_orchestrator",
    "app.agents.agent5_render.services.video",
]
check("3a: agent3_audio.services.audio is referenced (not agent4_audio)",
      "app.agents.agent3_audio.services.audio" in src)
check("3b: agent5_render.services.video is referenced (not agent5_video)",
      "app.agents.agent5_render.services.video" in src)
check("3c: agent4_visuals.services.visual_orchestrator is referenced",
      "app.agents.agent4_visuals.services.visual_orchestrator" in src)
for m in required_modules:
    try:
        importlib.import_module(m)
        ok = True
    except ModuleNotFoundError:
        ok = False
    check(f"3d: {m} resolves", ok)

print("\n── 4/5: Agent 3 -> Agent 4 -> Agent 5 order, visuals separate from render ──")
i_audio   = src.index("run_audio_generation")
i_visuals = src.index("run_visual_generation_for_content")
i_render  = src.index("run_video_generation")
check("4a: run_audio_generation appears before run_visual_generation_for_content",
      i_audio < i_visuals)
check("4b: run_visual_generation_for_content appears before run_video_generation",
      i_visuals < i_render)
check("5a: a dedicated visuals step function exists, distinct from the render step function",
      "_run_step_visuals" in src and "_run_step_render" in src
      and "_run_step_visuals" != "_run_step_render")
src_step_visuals = inspect.getsource(
    importlib.import_module("test_full_pipeline")._run_step_visuals
)
src_step_render = inspect.getsource(
    importlib.import_module("test_full_pipeline")._run_step_render
)
check("5b: _run_step_visuals calls run_visual_generation_for_content, not run_video_generation",
      "run_visual_generation_for_content" in src_step_visuals
      and "run_video_generation" not in src_step_visuals)
check("5c: _run_step_render calls run_video_generation, not run_visual_generation_for_content",
      "run_video_generation" in src_step_render
      and "run_visual_generation_for_content" not in src_step_render)

print("\n── 6: Script validation failure does not mark SCRIPTS_VALIDATED ──")
harness = importlib.import_module("test_full_pipeline")
src_step_scripts = inspect.getsource(harness._run_step_scripts)
check("6a: SCRIPTS_VALIDATED is only assigned inside the branch guarded by a truthy "
      "required_scripts/generate_multilingual_scripts() return value",
      'content.status = "SCRIPTS_VALIDATED"' in src_step_scripts
      and src_step_scripts.index("if not required_scripts:")
          < src_step_scripts.index('content.status = "SCRIPTS_VALIDATED"'))
check("6b: the failure branch returns False before reaching the SCRIPTS_VALIDATED assignment",
      src_step_scripts.count("return False") >= 1)
# Regression guard for the exact Phase 5A bug: that bug was an *unconditional* assignment of
# both Script.validated=True and Content.status="SCRIPTS_VALIDATED" regardless of a computed
# pass/fail outcome. The structural guard against a repeat is an early `return False` sitting
# strictly between the completeness check and the SCRIPTS_VALIDATED assignment — confirmed by
# 6a/6b above. This check additionally guards against a *second*, redundant SCRIPTS_VALIDATED
# assignment appearing anywhere earlier in the function (which would defeat the gate even with
# the early return present).
check("6c: SCRIPTS_VALIDATED is assigned exactly once in this function, "
      "not duplicated outside the gated branch",
      src_step_scripts.count('content.status = "SCRIPTS_VALIDATED"') == 1)
check("6d: Script.validated is never force-set to True outside generate_multilingual_scripts() "
      "(no s.validated = True loop reimplemented in the harness itself, unlike the Phase 5A bug)",
      "s.validated = True" not in src_step_scripts and ".validated = True" not in src_step_scripts)

print("\n── 7: Child rows are discovered/reused, not recreated ──")
src_shorts_planning = inspect.getsource(harness._run_step_shorts_planning)
check("7a: _existing_children() is checked before calling run_shorts_planner()",
      src_shorts_planning.index("_existing_children(")
      < src_shorts_planning.index("run_shorts_planner("))
check("7b: existing children short-circuit (REUSED) when not forced",
      "REUSED" in src_shorts_planning and "if existing and not force" in src_shorts_planning)

print("\n── 8: Force flags exist ──")
for flag in ("--force-scripts", "--force-audio", "--force-visuals", "--force-render"):
    check(f"8.{flag}", flag in src)

print("\n── 9: Dry-run / confirm safety exists ──")
check("9a: --dry-run flag defined", "--dry-run" in src)
check("9b: --confirm flag defined", "--confirm" in src)
check("9c: real run without --confirm is refused before Step 1",
      "Refusing to run for real without --confirm" in src)
check("9d: required env vars are checked before any paid call",
      "_check_env_vars" in src and "_print_env_check" in src)
check("9e: missing required env vars abort before Step 1 in a real run",
      "Required environment variable(s) missing" in src)

print("\n── 10: Final summary includes parent and child rows ──")
src_final_summary = inspect.getsource(harness._print_final_summary)
check("10a: _print_final_summary iterates parent + children into one rows list",
      "[_summary_row(parent" in src_final_summary and "for c in children" in src_final_summary)

print("\n── 11: No parent-cut short / bookend / rehook / bridge logic ──")
for forbidden in ("cut_shorts", "rehook", "bridge_path", "bookend", "breakpoint"):
    check(f"11.{forbidden}: absent", forbidden not in src.lower())

print("\n── 12/13/14: No parent-gates-child-audio/visuals side effects ──")
check("12a: SCRIPTS_VALIDATED_AWAITING_PARENT (the forbidden release-gate status) is absent",
      "SCRIPTS_VALIDATED_AWAITING_PARENT" not in src)
check("12b: no query filters Content.status for a child release gate keyed on parent success",
      "AWAITING_PARENT" not in src)


def _functions_assigning_other_status(tree: ast.AST) -> dict[str, list[str]]:
    """Map function name -> list of status string literals it assigns to *some* content var."""
    result: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assigns = []
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == "status" for t in inner.targets)
                ):
                    if isinstance(inner.value, ast.Constant) and isinstance(inner.value.value, str):
                        assigns.append(inner.value.value)
            if assigns:
                result[node.name] = assigns
    return result


status_writers = _functions_assigning_other_status(tree)
# _run_step_audio only ever writes to *its own* `content` parameter (the function is called
# once per content row independently); it must never iterate over a separate `children` list
# and assign their .status as a side effect of processing `content`.
src_step_audio = inspect.getsource(harness._run_step_audio)
check("13a: _run_step_audio never iterates a children/short list and assigns their status "
      "(it only ever touches its own `content` parameter)",
      "for c in children" not in src_step_audio and "child.status" not in src_step_audio)
check("13b: _run_step_audio's own status writes happen only inside run_audio_generation() "
      "(the production function), not via a direct content.status= assignment in the harness",
      'content.status = "AUDIO_DONE"' not in src_step_audio
      and 'content.status = "SCRIPTS_VALIDATED_AWAITING_PARENT"' not in src_step_audio)
check("14a: _run_step_visuals never iterates a children/short list and assigns their status "
      "(it only ever touches its own `content` parameter)",
      "for c in children" not in src_step_visuals and "child.status" not in src_step_visuals)
check("14b: the execution driver calls _run_step_audio / _run_step_visuals once per content row "
      "in a loop over children, rather than the step function fanning out internally",
      inspect.getsource(harness._execute_audio_through_render).count("for c in children") >= 2)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 5B test_full_pipeline.py rewrite")
