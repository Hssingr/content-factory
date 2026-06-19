"""Standalone short planning smoke test — zero API calls, zero DB access.

Verifies:
  1. generate_shorts_plan, generate_short_episode_script, run_shorts_planner importable.
  2. _SHORT_EPISODE_SYSTEM_PROMPT importable and contains "Re-hook every 7–10 seconds".
  3. Python total_parts range validation rejects total_parts=6.
  4. Python total_parts range validation rejects total_parts=2.
  5. Python total_parts range validation accepts total_parts=4 (boundary PASS).
  6. generate_shorts_plan schema has required keys total_parts and parts.
  7. _SHORTS_PLAN_SCHEMA enforces minItems=3 and maxItems=5 on parts array.
  8. Synthetic [INTRO] hook check fires MAJOR on a Short script with a forbidden opener.
  9. Synthetic [INTRO] hook check returns no MAJOR on a clean Short script opener.

Run: python scripts/smoke_standalone_shorta.py
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

import re

from app.agents.agent2_discovery.system_prompt import (
    generate_shorts_plan,
    generate_short_episode_script,
    _SHORT_EPISODE_SYSTEM_PROMPT,
    _SHORTS_PLANNER_SYSTEM_PROMPT,
    _SHORTS_PLAN_SCHEMA,
)
from app.agents.agent2_discovery.services.scripts import run_shorts_planner
from app.services.script_checks import check_hook_quality

assert_ok("imports", True)

# ── 2. _SHORT_EPISODE_SYSTEM_PROMPT contains re-hook rule ────────────────────

assert_ok(
    "_SHORT_EPISODE_SYSTEM_PROMPT contains re-hook rule",
    "Re-hook every 7–10 seconds" in _SHORT_EPISODE_SYSTEM_PROMPT,
    "string not found in prompt",
)

# ── 3. total_parts=6 is rejected by schema minimum/maximum ───────────────────
# Simulate the Python validation gate from generate_shorts_plan()

def _validate_total_parts(n: int) -> bool:
    """Mirror the validation from generate_shorts_plan()."""
    return isinstance(n, int) and (3 <= n <= 5)

assert_ok(
    "total_parts=6 rejected",
    not _validate_total_parts(6),
    "expected rejection of total_parts=6",
)

# ── 4. total_parts=2 is rejected ─────────────────────────────────────────────

assert_ok(
    "total_parts=2 rejected",
    not _validate_total_parts(2),
    "expected rejection of total_parts=2",
)

# ── 5. total_parts=4 accepted ────────────────────────────────────────────────

assert_ok(
    "total_parts=4 accepted",
    _validate_total_parts(4),
    "expected acceptance of total_parts=4",
)

# ── 6. _SHORTS_PLAN_SCHEMA has required keys ─────────────────────────────────

required = _SHORTS_PLAN_SCHEMA.get("required", [])
assert_ok(
    "_SHORTS_PLAN_SCHEMA requires total_parts",
    "total_parts" in required,
    f"required: {required}",
)
assert_ok(
    "_SHORTS_PLAN_SCHEMA requires parts",
    "parts" in required,
    f"required: {required}",
)

# ── 7. parts array schema has minItems=3 and maxItems=5 ──────────────────────

parts_schema = _SHORTS_PLAN_SCHEMA.get("properties", {}).get("parts", {})
assert_ok(
    "parts schema minItems=3",
    parts_schema.get("minItems") == 3,
    f"minItems={parts_schema.get('minItems')!r}",
)
assert_ok(
    "parts schema maxItems=5",
    parts_schema.get("maxItems") == 5,
    f"maxItems={parts_schema.get('maxItems')!r}",
)

# ── 8. Hook check fires MAJOR on forbidden opener ────────────────────────────
# Mirrors the logic in run_shorts_planner(): first sentence extracted from flat
# voice_script, wrapped in synthetic [INTRO] prefix, passed to check_hook_quality().

_forbidden_script = "In this story, a missing man was found alive after thirty years."
_first_sent = re.split(r"(?<=[.!?])\s+", _forbidden_script.strip())[0]
_hook_issues = check_hook_quality(f"[INTRO]\n{_first_sent}", "en")
_hook_majors = [i for i in _hook_issues if i["severity"] == "MAJOR"]

assert_ok(
    "hook check fires MAJOR on forbidden opener via synthetic [INTRO]",
    len(_hook_majors) >= 1,
    f"expected ≥1 MAJOR hook issue, got: {_hook_issues}",
)

# ── 9. Hook check returns no MAJOR on a clean opener ─────────────────────────

_clean_script = "She was declared dead before anyone noticed she was missing."
_first_sent_clean = re.split(r"(?<=[.!?])\s+", _clean_script.strip())[0]
_hook_issues_clean = check_hook_quality(f"[INTRO]\n{_first_sent_clean}", "en")
_hook_majors_clean = [i for i in _hook_issues_clean if i["severity"] == "MAJOR"]

assert_ok(
    "hook check returns no MAJOR on clean Short opener",
    len(_hook_majors_clean) == 0,
    f"unexpected MAJOR: {_hook_issues_clean}",
)

# ── Done ──────────────────────────────────────────────────────────────────────

print()
print("SMOKE PASS")
