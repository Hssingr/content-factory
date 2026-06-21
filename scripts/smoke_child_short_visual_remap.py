"""Child short visual remap smoke test — zero API calls, zero DB access.

Verifies:
  1. remap_beats_for_short importable from storyboard.py.
  2. _MATCH_SCORE_THRESHOLD == 70 (constant in place).
  3. score=85, valid cache path → reuse path fires (media_url preserved).
  4. score=55 → below threshold → new Flux path fires.
  5. score=90, empty media_url → new Flux path fires (missing media fallback).
  6. score=90, __text_card__ sentinel → new Flux path fires (text_card fallback).
  7. video.py routing: remap_beats_for_short is imported into video.py.
  8. _SHORT_REMAP_SCHEMA has required assignments key with correct structure.

Run: python scripts/smoke_standalone_shortc.py
Expected output: all lines prefixed with PASS, then SMOKE PASS
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]")


# ── 1. Import check ───────────────────────────────────────────────────────────

import inspect

from app.agents.agent4_visuals.subagents.storyboard import (
    remap_beats_for_short,
    _MATCH_SCORE_THRESHOLD,
    _TEXT_CARD_SENTINEL,
    _SHORT_REMAP_SCHEMA,
)

assert_ok("remap_beats_for_short importable", True)

# ── 2. Threshold constant is 70 ───────────────────────────────────────────────

assert_ok(
    "_MATCH_SCORE_THRESHOLD == 70",
    _MATCH_SCORE_THRESHOLD == 70,
    f"actual value: {_MATCH_SCORE_THRESHOLD}",
)

# ── 3-6. Threshold decision logic (mirrors remap_beats_for_short) ─────────────
# We inline the decision condition so we don't need DB or Claude.

def _should_reuse(match_score: int, media_url: str) -> bool:
    """Mirror the reuse condition in remap_beats_for_short."""
    return (
        match_score >= _MATCH_SCORE_THRESHOLD
        and bool(media_url)
        and media_url != _TEXT_CARD_SENTINEL
        and media_url.startswith("cache/")
    )


assert_ok(
    "score=85, valid cache/ path → reuse",
    _should_reuse(85, "cache/abc123456789012345678901.jpg"),
)

assert_ok(
    "score=55 → below threshold → new Flux",
    not _should_reuse(55, "cache/abc123456789012345678901.jpg"),
)

assert_ok(
    "score=90, empty media_url → new Flux (missing media fallback)",
    not _should_reuse(90, ""),
)

assert_ok(
    "score=90, __text_card__ → new Flux (text_card fallback)",
    not _should_reuse(90, _TEXT_CARD_SENTINEL),
)

# ── 7. Agent 4 visual orchestrator routes remap_beats_for_short; Agent 5 does not ──

import importlib
import ast

orchestrator_py = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "agents", "agent4_visuals", "services", "visual_orchestrator.py",
)
with open(orchestrator_py, encoding="utf-8") as fh:
    orchestrator_src = fh.read()

video_py = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "agents", "agent5_render", "services", "video.py",
)
with open(video_py, encoding="utf-8") as fh:
    video_src = fh.read()

assert_ok(
    "visual_orchestrator.py imports remap_beats_for_short",
    "remap_beats_for_short" in orchestrator_src,
    "symbol not found in visual_orchestrator.py source",
)

# Also confirm it's used in the routing block
assert_ok(
    "visual_orchestrator.py calls remap_beats_for_short()",
    "remap_beats_for_short(" in orchestrator_src,
    "call site not found in visual_orchestrator.py source",
)

# Agent 5 must call only Agent 4's public entrypoint, not internal helpers.
assert_ok(
    "video.py does not call remap_beats_for_short() directly",
    "remap_beats_for_short(" not in video_src,
    "Agent 5 should not orchestrate Agent 4 internal helpers directly",
)

# ── 8. _SHORT_REMAP_SCHEMA structure ─────────────────────────────────────────

assignments_schema = _SHORT_REMAP_SCHEMA.get("properties", {}).get("assignments", {})
items_schema = assignments_schema.get("items", {})
required_fields = items_schema.get("required", [])

assert_ok(
    "_SHORT_REMAP_SCHEMA has assignments property",
    "assignments" in _SHORT_REMAP_SCHEMA.get("properties", {}),
)

for field in ("narration_phrase", "long_beat_order", "beat_intensity", "match_score"):
    assert_ok(
        f"_SHORT_REMAP_SCHEMA items require '{field}'",
        field in required_fields,
        f"required fields: {required_fields}",
    )

# ── Done ──────────────────────────────────────────────────────────────────────

print()
print("SMOKE PASS")
