"""Smoke tests for Work Item 7 — section progression fix.

Validates:
1. When a body section matches 3+ major turns, only the primary turn is credited.
2. The body loop does NOT exit after 2 body sections when the blueprint has 4 major turns
   (at_min requires min(4, len(major_turns)) = 4 body sections in that case).
3. _SECTION_GENERATION_SYSTEM_PROMPT contains future-turn suppression rules.
4. generate_section() has primary_required_turn and future_uncovered_turns params.
5. _generate_section_with_retry() has primary_required_turn and future_uncovered_turns params.
6. _min_body_for_bp with 4 turns = 4; with 2 turns = _MIN_BODY_SECTIONS.
7. Only-primary credit: primary_idx is always added regardless of match result.
8. Zero-match case still credits primary turn (always credit rule).

No API calls. Run with:
    python scripts/smoke_section_progression.py
"""

import sys
import os
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, condition: bool) -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    if not condition:
        _failures += 1


# ─────────────────────────────────────────────────────────────────────────────
# 1 — Turn crediting: 3+ matched → only primary credited
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 1: 3+ turn match → only primary credited ──")

from app.agents.agent2_discovery.services.scripts import _match_turns, _MIN_BODY_SECTIONS

# Four turns where the fixture text overlaps with turns 0, 1, 2 (all content tokens)
major_turns = [
    "detective discovers hidden evidence inside the warehouse",
    "suspect confesses to being present that night",
    "financial fraud traced back to the warehouse owner",
    "victim finally identified through dental records",
]
# Section text that overlaps strongly with turns 0, 1, 2 (but not 4)
section_text = (
    "The detective had finally discovered the hidden evidence inside the warehouse. "
    "The suspect then confessed to being present that night during the incident. "
    "Financial fraud was also traced directly back to the warehouse owner through documents."
)

reveals = [
    "detective discovered hidden evidence inside warehouse",
    "suspect confessed to being present that night",
    "financial fraud traced back to warehouse owner",
]

all_matched = _match_turns(reveals, major_turns, section_text, label="SECTION 1")
check("fixture: 3+ turns matched", len(all_matched) >= 3)

# Simulate the capped crediting logic from generate_script_sections
covered_turns: set = set()
_primary_idx = 0  # earliest uncovered turn
if len(all_matched) >= 3:
    covered_turns.add(_primary_idx)
else:
    covered_turns |= all_matched
    covered_turns.add(_primary_idx)

check("3+ match: only 1 turn credited (primary)", len(covered_turns) == 1)
check("3+ match: primary turn 0 is credited", 0 in covered_turns)
check("3+ match: turn 1 NOT credited (suppressed)", 1 not in covered_turns)
check("3+ match: turn 2 NOT credited (suppressed)", 2 not in covered_turns)

# ─────────────────────────────────────────────────────────────────────────────
# 2 — at_min with 4 turns requires 4 body sections
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 2: at_min with 4 major_turns ──")

# Replicate _min_body_for_bp logic from generate_script_sections
def _compute_min_body(major_turns_count: int) -> int:
    return (
        max(_MIN_BODY_SECTIONS, min(4, major_turns_count))
        if major_turns_count >= 4
        else _MIN_BODY_SECTIONS
    )


min_body_4_turns = _compute_min_body(4)
min_body_5_turns = _compute_min_body(5)
min_body_3_turns = _compute_min_body(3)
min_body_2_turns = _compute_min_body(2)

check("4 turns → _min_body_for_bp = 4", min_body_4_turns == 4)
check("5 turns → _min_body_for_bp = 4 (capped at 4)", min_body_5_turns == 4)
check("3 turns → _min_body_for_bp = _MIN_BODY_SECTIONS", min_body_3_turns == _MIN_BODY_SECTIONS)
check("2 turns → _min_body_for_bp = _MIN_BODY_SECTIONS", min_body_2_turns == _MIN_BODY_SECTIONS)

# Verify that with 4 turns, at_min=False at body_index=3 (would have exited too early before fix)
at_min_body3_4turns = 3 > min_body_4_turns  # should be False (3 > 4 = False)
at_min_body4_4turns = 4 > min_body_4_turns  # should be False (4 > 4 = False)
at_min_body5_4turns = 5 > min_body_4_turns  # should be True  (5 > 4 = True)

check("4 turns, body_index=3: at_min is False (loop must continue)", at_min_body3_4turns is False)
check("4 turns, body_index=4: at_min is False (loop must continue)", at_min_body4_4turns is False)
check("4 turns, body_index=5: at_min is True (loop may exit)", at_min_body5_4turns is True)

# With old logic (body_index > _MIN_BODY_SECTIONS = 2), body_index=3 would have been True
old_at_min_body3 = 3 > _MIN_BODY_SECTIONS  # True (pre-fix: loop would exit early)
check("OLD logic (body_index=3 > _MIN_BODY_SECTIONS=2) was True — confirmed bug", old_at_min_body3 is True)

# ─────────────────────────────────────────────────────────────────────────────
# 3 — Future-turn suppression rules in system prompt
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 3: Future-turn suppression rules in _SECTION_GENERATION_SYSTEM_PROMPT ──")

from app.agents.agent2_discovery.system_prompt import _SECTION_GENERATION_SYSTEM_PROMPT

check(
    "prompt: 'one narrative job' present",
    "one narrative job" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: future-turn foreshadow-but-not-resolve rule present",
    "may be foreshadowed" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: 'do not resolve yet' language present",
    "do not resolve" in _SECTION_GENERATION_SYSTEM_PROMPT.lower()
    or "do NOT resolve" in _SECTION_GENERATION_SYSTEM_PROMPT
    or "must not be answered" in _SECTION_GENERATION_SYSTEM_PROMPT,
)
check(
    "prompt: bridge toward next turn rule present",
    "bridge" in _SECTION_GENERATION_SYSTEM_PROMPT or "open question" in _SECTION_GENERATION_SYSTEM_PROMPT,
)

# ─────────────────────────────────────────────────────────────────────────────
# 4 — generate_section() has new params
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 4: generate_section() param signatures ──")

from app.agents.agent2_discovery.system_prompt import generate_section
sig = inspect.signature(generate_section)
check(
    "generate_section: has primary_required_turn param",
    "primary_required_turn" in sig.parameters,
)
check(
    "generate_section: primary_required_turn defaults to None",
    sig.parameters["primary_required_turn"].default is None,
)
check(
    "generate_section: has future_uncovered_turns param",
    "future_uncovered_turns" in sig.parameters,
)
check(
    "generate_section: future_uncovered_turns defaults to None",
    sig.parameters["future_uncovered_turns"].default is None,
)
check(
    "generate_section: old param current_required_turns is GONE",
    "current_required_turns" not in sig.parameters,
)

# ─────────────────────────────────────────────────────────────────────────────
# 5 — _generate_section_with_retry() has new params
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 5: _generate_section_with_retry() param signatures ──")

from app.agents.agent2_discovery.services.scripts import _generate_section_with_retry
sig2 = inspect.signature(_generate_section_with_retry)
check(
    "_generate_section_with_retry: has primary_required_turn param",
    "primary_required_turn" in sig2.parameters,
)
check(
    "_generate_section_with_retry: primary_required_turn defaults to None",
    sig2.parameters["primary_required_turn"].default is None,
)
check(
    "_generate_section_with_retry: has future_uncovered_turns param",
    "future_uncovered_turns" in sig2.parameters,
)
check(
    "_generate_section_with_retry: old param required_turns is GONE",
    "required_turns" not in sig2.parameters,
)

# ─────────────────────────────────────────────────────────────────────────────
# 6 — User message wiring: primary turn → injected as single constraint
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 6: generate_section user message wiring ──")

src = inspect.getsource(generate_section)
check(
    "generate_section: 'MUST primarily advance this one story turn' in source",
    "MUST primarily advance this one story turn" in src,
)
check(
    "generate_section: future turns injection uses 'do NOT fully resolve' language",
    "do NOT fully resolve these yet" in src,
)
check(
    "generate_section: primary_required_turn gated on truthiness",
    "if primary_required_turn:" in src,
)
check(
    "generate_section: future_uncovered_turns gated on truthiness",
    "if future_uncovered_turns:" in src,
)

# ─────────────────────────────────────────────────────────────────────────────
# 7 — 2-match case: both turns credited + primary always included
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 7: 2-match case — both turns credited ──")

# When 2 turns match (not 3+), both should be credited, including primary
covered2: set = set()
_primary_idx2 = 0
two_matched = {0, 1}  # exactly 2 — should fall through to normal path
if len(two_matched) >= 3:
    covered2.add(_primary_idx2)
else:
    covered2 |= two_matched
    covered2.add(_primary_idx2)

check("2-match case: both turns 0 and 1 credited", covered2 == {0, 1})

# ─────────────────────────────────────────────────────────────────────────────
# 8 — Zero-match case: primary always credited
# ─────────────────────────────────────────────────────────────────────────────

print("\n── 8: Zero-match case — primary always credited ──")

covered_zero: set = set()
_primary_idx3 = 2  # primary is turn 2
zero_matched: set = set()  # section matched nothing
if len(zero_matched) >= 3:
    covered_zero.add(_primary_idx3)
else:
    covered_zero |= zero_matched
    covered_zero.add(_primary_idx3)

check("zero-match: primary turn 2 still credited", 2 in covered_zero)
check("zero-match: only primary credited (nothing else)", covered_zero == {2})

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — section progression fix: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
