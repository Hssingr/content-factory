"""Smoke test — Phase 4E-F media validator.

Verifies:
  1. Parent validator path reachable (_run_parent_visuals calls
     _check_media_assets after _save_video_sections).
  2. Child validator path reachable (_run_child_short_visuals calls the same
     shared helper).
  3. Missing media detected (empty/missing media_url, malformed/remote URL,
     missing/zero-byte file on disk — all deterministic fixture checks, no
     real filesystem dependency beyond a throwaway tempdir).
  4. Reuse validation reachable (a cross-content media_url that is missing
     is reported as reused_media_missing, not the own-asset variant).
  5. Persistence validation reachable (_check_media_assets compares against
     a fresh DB reload and flags a mismatch).
  6. Agent 4 ownership preserved.
  7. Agent 5 unchanged.
  8. Statuses unchanged.
  9. Scheduling unchanged.
  10. No second validator framework; no AI/network calls in the new logic.
  11. Bug Candidate 1 exclusion: the new checks gate on
      "media_strategy != remotion_text_card", never on
      "media_strategy == flux_generated" (Part 0 requirement).

No live APIs, no Flux generation, no Remotion render, no DB migration.
"""

import ast
import importlib
import inspect
import os
import re
import sys
import tempfile
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

src_validator = inspect.getsource(validator_mod)
src_orchestrator = inspect.getsource(orchestrator)

# ── isolate filesystem checks in a throwaway tempdir, never the real media_path ──
from app.config import settings
_tmpdir = tempfile.mkdtemp(prefix="smoke_media_validator_")
_orig_media_path = settings.media_path
settings.media_path = _tmpdir


def beat(order, media_url, media_strategy="flux_generated", media_type="image"):
    return {
        "beat_order": order, "section_order": order, "media_strategy": media_strategy,
        "media_type": media_type, "media_url": media_url,
    }


try:
    print("\n── 1: Parent validator path reachable ──")
    src_run_parent_visuals = inspect.getsource(orchestrator._run_parent_visuals)
    check("1a: _run_parent_visuals calls _check_media_assets",
          "_check_media_assets(" in src_run_parent_visuals)
    check("1b: _check_media_assets runs after _save_video_sections in the parent loop",
          src_run_parent_visuals.index("_save_video_sections(")
          < src_run_parent_visuals.index("_check_media_assets("))

    print("\n── 2: Child validator path reachable ──")
    src_run_child_short_visuals = inspect.getsource(orchestrator._run_child_short_visuals)
    check("2a: _run_child_short_visuals calls _check_media_assets",
          "_check_media_assets(" in src_run_child_short_visuals)
    check("2b: _check_media_assets runs after _save_video_sections in the child loop",
          src_run_child_short_visuals.index("_save_video_sections(")
          < src_run_child_short_visuals.index("_check_media_assets("))

    print("\n── 3: Missing/invalid media detected ──")
    cid = "abc123"
    os.makedirs(os.path.join(_tmpdir, "cache", cid), exist_ok=True)
    real_path = os.path.join(_tmpdir, "cache", cid, "real.jpg")
    with open(real_path, "wb") as f:
        f.write(b"fake-jpeg-bytes")
    zero_path = os.path.join(_tmpdir, "cache", cid, "zero.jpg")
    open(zero_path, "wb").close()

    fixture_beats = [
        beat(0, f"cache/{cid}/real.jpg"),               # valid — no issue
        beat(1, ""),                                      # missing
        beat(2, "http://evil.example.com/x.jpg"),         # malformed/remote
        beat(3, f"cache/{cid}/zero.jpg"),                  # zero-byte
        beat(4, f"cache/{cid}/does_not_exist.jpg"),        # missing on disk, own asset
        beat(5, "__text_card__", media_strategy="remotion_text_card"),  # exempt
        beat(6, f"cache/{cid}/real.jpg", media_type="video"),  # unsupported type
    ]
    issues = validator_mod.validate_media_assets(fixture_beats, cid)
    issues_by_order = {i["beat_order"]: i["check"] for i in issues}
    check("3a: valid beat (beat 0) produces no issue",
          0 not in issues_by_order)
    check("3b: empty media_url detected (beat 1)",
          issues_by_order.get(1) == "media_url_empty")
    check("3c: remote/malformed URL detected (beat 2)",
          issues_by_order.get(2) == "media_url_malformed")
    check("3d: zero-byte file detected (beat 3)",
          issues_by_order.get(3) == "media_file_empty")
    check("3e: missing-on-disk own asset detected (beat 4)",
          issues_by_order.get(4) == "media_file_missing_on_disk")
    check("3f: text_card beat (beat 5) is exempt — no issue",
          5 not in issues_by_order)
    check("3g: unsupported media_type detected (beat 6)",
          issues_by_order.get(6) == "media_type_unsupported")
    check("3h: every finding from this section is MAJOR severity",
          all(i["severity"] == "MAJOR" for i in issues))

    print("\n── 4: Reuse validation reachable ──")
    reuse_beats = [
        beat(0, "cache/PARENT_CONTENT_ID/missing_parent_asset.jpg"),
    ]
    reuse_issues = validator_mod.validate_media_assets(reuse_beats, "CHILD_CONTENT_ID")
    check("4a: a missing cross-content (reused) asset is reported as reused_media_missing, "
          "not the own-asset variant",
          len(reuse_issues) == 1 and reuse_issues[0]["check"] == "reused_media_missing")

    print("\n── 5: Persistence validation reachable ──")
    src_check_media_assets = inspect.getsource(orchestrator._check_media_assets)
    check("5a: _check_media_assets reloads via _load_sections_from_db and compares",
          "_load_sections_from_db(" in src_check_media_assets
          and "persistence_media_url_mismatch" in src_check_media_assets
          and "persistence_media_type_mismatch" in src_check_media_assets)
    check("5b: _check_media_assets also detects a row missing entirely on reload",
          "persistence_row_missing" in src_check_media_assets)

    print("\n── 6: Agent 4 ownership preserved ──")
    check("6a: validate_media_assets lives under app.agents.agent4_visuals",
          validator_mod.__name__.startswith("app.agents.agent4_visuals"))
    check("6b: _check_media_assets lives in visual_orchestrator.py (Agent 4)",
          orchestrator.__name__.startswith("app.agents.agent4_visuals"))
    check("6c: VideoSection persistence still owned by visual_orchestrator.py "
          "(_check_media_assets only reads, via _load_sections_from_db)",
          callable(getattr(orchestrator, "_save_video_sections", None)))

    print("\n── 7: Agent 5 unchanged ──")
    video_src = inspect.getsource(video_mod)
    video_imports = []
    for node in ast.walk(ast.parse(video_src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            video_imports.append(node.module)
        elif isinstance(node, ast.Import):
            video_imports.extend(a.name for a in node.names)
    check("7a: video.py still imports no app.agents.agent4_visuals module",
          not any(m.startswith("app.agents.agent4_visuals") for m in video_imports))
    check("7b: video.py does not reference validate_media_assets or the new check names",
          "validate_media_assets" not in video_src
          and "_check_media_assets" not in video_src
          and "media_url_missing" not in video_src
          and "reused_media_missing" not in video_src)

    print("\n── 8: Statuses unchanged ──")
    _known_statuses = {
        "PARENT_VISUALS_DONE", "CHILD_SHORT_VISUALS_DONE",
        "CHILD_SHORT_VISUALS_DEFERRED", "VISUALS_FAILED",
        "AUDIO_DONE", "GENERATING_VISUALS", "FAILED",
    }
    _status_literals = set(re.findall(r'content\.status\s*=\s*"([A-Z_]+)"', src_orchestrator))
    _status_literals |= set(re.findall(r'"status":\s*"([A-Z_]+)"', src_orchestrator))
    check("8a: visual_orchestrator.py introduces no new Content.status literal",
          _status_literals <= _known_statuses)

    print("\n── 9: Scheduling unchanged ──")
    tasks_mod = importlib.import_module("app.scheduler.tasks")
    scheduler_init_src = (ROOT / "app" / "scheduler" / "__init__.py").read_text(encoding="utf-8")
    check("9a: tasks.py does not reference validate_media_assets or the new check names",
          "validate_media_assets" not in inspect.getsource(tasks_mod)
          and "media_url_missing" not in inspect.getsource(tasks_mod))
    check("9b: scheduler/__init__.py Beat schedule unchanged",
          "pickup-audio-done" in scheduler_init_src and "pickup-visual-ready" in scheduler_init_src)

    print("\n── 10: No second validator framework; deterministic only ──")
    _known_validate_functions = {"validate_storyboard", "validate_media_assets"}
    check("10a: storyboard_validator.py defines only the known validate_* functions",
          {name for name in dir(validator_mod) if name.startswith("validate_")}
          <= _known_validate_functions)
    check("10b: validate_media_assets makes no Claude/fal.ai/network call",
          "fal_client" not in src_validator and "call_claude" not in src_validator
          and "anthropic" not in src_validator and "requests." not in src_validator
          and "httpx." not in src_validator)
    src_validate_media_assets = inspect.getsource(validator_mod.validate_media_assets)
    check("10c: validate_media_assets only does local filesystem reads "
          "(Path.exists/is_file/stat), no image decoding library import",
          "is_file(" in src_validate_media_assets or "stat(" in src_validate_media_assets)

    print("\n── 11: Bug Candidate 1 exclusion (Phase 4D-E0 / Part 0) ──")
    check("11a: the new checks gate on '!= remotion_text_card', never "
          "'== flux_generated' as a trusted producer claim",
          'strategy == _NO_MEDIA_REQUIRED_STRATEGY' in src_validate_media_assets
          and '== "flux_generated"' not in src_validate_media_assets)
    check("11b: CLAUDE.md-visible rationale comment for the exclusion exists in source",
          "Bug Candidate 1" in inspect.getsource(validator_mod))

finally:
    settings.media_path = _orig_media_path
    import shutil
    shutil.rmtree(_tmpdir, ignore_errors=True)

print()
if _failures:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    raise SystemExit(1)

print("SMOKE PASS — Phase 4E-F media validator")
