"""Section transition smoke test — zero API calls, zero DB access.

Verifies:
  1. check_section_transition() imported from script_checks.
  2. Recap case (≥3 overlapping content tokens) → 1 MINOR issue returned.
  3. Clean case (no overlap) → [] returned.
  4. Empty prior summary → [] returned (guard clause).
  5. _GLOBAL_VALIDATION_SYSTEM_PROMPT contains per-item annotations.
  6. generate_script_sections() docstring no longer references "outer correction loop".
  7. _generate_section_with_retry accepts prior_summary_text parameter.

Run: python scripts/smoke_section_transitions.py
Expected output: all lines prefixed with PASS, then SMOKE PASS
"""

import sys
import os
import inspect

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

from app.services.script_checks import check_section_transition
from app.agents.agent2_discovery.system_prompt import _GLOBAL_VALIDATION_SYSTEM_PROMPT
from app.agents.agent2_discovery.services.scripts import (
    generate_script_sections,
    _generate_section_with_retry,
)

assert_ok("imports", True)

# ── 2. Recap case — should detect overlap ─────────────────────────────────────

PRIOR_SUMMARY = (
    "The banker Gerald Holt disappeared three weeks before the investigation began. "
    "Police discovered he had been placing funds into offshore accounts."
)

CURRENT_RECAP = (
    "Gerald Holt, the banker who disappeared three weeks earlier, had been placing "
    "funds into offshore accounts before he vanished. But what investigators found next "
    "would change everything."
)

issues = check_section_transition(CURRENT_RECAP, PRIOR_SUMMARY)
assert_ok(
    "recap case returns MINOR issue",
    len(issues) == 1 and issues[0]["severity"] == "MINOR" and issues[0]["category"] == "section_transition",
    f"got {issues}",
)

# ── 3. Clean case — no overlap ────────────────────────────────────────────────

PRIOR_SUMMARY_2 = (
    "The detective arrived at the bank branch just before closing time on a Tuesday."
)

CURRENT_CLEAN = (
    "Interpol had already flagged the account for suspicious wire transfers totalling "
    "four million dollars. The trail led straight to a shell company in Cyprus."
)

clean_issues = check_section_transition(CURRENT_CLEAN, PRIOR_SUMMARY_2)
assert_ok(
    "clean case returns []",
    clean_issues == [],
    f"got {clean_issues}",
)

# ── 4. Empty prior summary → guard clause returns [] ─────────────────────────

empty_issues = check_section_transition(CURRENT_CLEAN, "")
assert_ok(
    "empty prior summary returns []",
    empty_issues == [],
    f"got {empty_issues}",
)

# ── 5. _GLOBAL_VALIDATION_SYSTEM_PROMPT per-item annotations ─────────────────

assert_ok(
    "_GLOBAL_VALIDATION_SYSTEM_PROMPT has per-item annotations",
    "(already checked per section)" in _GLOBAL_VALIDATION_SYSTEM_PROMPT,
    "annotation string not found in prompt",
)

# ── 6. generate_script_sections docstring no longer says "outer correction loop" ─

src = inspect.getsource(generate_script_sections)
assert_ok(
    "generate_script_sections docstring is stale-free",
    "outer correction loop" not in src,
    "stale 'outer correction loop' reference still present",
)

# ── 7. _generate_section_with_retry accepts prior_summary_text ───────────────

sig = inspect.signature(_generate_section_with_retry)
assert_ok(
    "_generate_section_with_retry has prior_summary_text param",
    "prior_summary_text" in sig.parameters,
    f"parameters: {list(sig.parameters.keys())}",
)

# ── Done ──────────────────────────────────────────────────────────────────────

print()
print("SMOKE PASS")
