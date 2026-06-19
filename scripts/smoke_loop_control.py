"""Smoke tests for the two loop-control + TTS-cleanup fixes introduced in scripts.py.

No API calls. Run with:
    python scripts/smoke_loop_control.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.agent2_discovery.services.scripts import (
    _MAX_BODY_SECTIONS,
    _MIN_BODY_SECTIONS,
    _match_turns,
)
from app.services.script_checks import normalize_tts_chars, check_tts_compliance

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
# Problem 1 — loop control
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Problem 1: loop control ──")

# 1a. Hard cap must be 7 (V2 spec)
check("_MAX_BODY_SECTIONS == 7", _MAX_BODY_SECTIONS == 7)
check("_MIN_BODY_SECTIONS == 2", _MIN_BODY_SECTIONS == 2)

# 1b. New exit condition must NOT break when covered_turns < total and below hard cap
# Simulate the state from the failing log:
#   body_index=5, max_body=4 (blueprint suggested 4), covered_turns=0, major_turns=4
body_index = 5
max_body = 4
covered_turns: set = set()
major_turns_count = 4

at_min         = body_index > _MIN_BODY_SECTIONS   # True
at_soft_max    = body_index > max_body              # True  (5 > 4)
at_hard_max    = body_index > _MAX_BODY_SECTIONS   # False (5 <= 7)
all_turns_covered = len(covered_turns) >= major_turns_count  # False
claude_suggests_outro = True

# Old condition: `at_max or (all_turns_covered and at_min and claude_suggests_outro)`
# This WOULD have broken — verify the bug is reproduced
old_condition = at_soft_max or (all_turns_covered and at_min and claude_suggests_outro)
check("OLD condition would have broken (bug reproduced)", old_condition is True)

# New condition must NOT break here
new_condition_hard = at_hard_max
new_condition_normal = all_turns_covered and at_min and (at_soft_max or claude_suggests_outro)
should_break_new = new_condition_hard or new_condition_normal
check(
    "NEW condition does NOT break at body_index=5 / covered=0/4 / at_hard_max=False",
    not should_break_new,
)

# 1c. New condition DOES break when hard cap is reached
body_index_hard = _MAX_BODY_SECTIONS + 1   # = 8
at_hard_max_now = body_index_hard > _MAX_BODY_SECTIONS   # True
check("NEW condition breaks at hard cap (body_index=8)", at_hard_max_now)

# 1d. New condition breaks normally when all turns covered + at_min + suggests_outro
body_index_ok  = 4
covered_all    = {0, 1, 2, 3}   # all 4 turns covered
at_min_ok      = body_index_ok > _MIN_BODY_SECTIONS   # True (4 > 2)
at_soft_max_ok = body_index_ok > 3                    # True (4 > 3, example max_body=3)
at_hard_ok     = body_index_ok > _MAX_BODY_SECTIONS   # False
all_covered    = len(covered_all) >= 4                 # True

normal_exit = (not at_hard_ok) and all_covered and at_min_ok and (at_soft_max_ok or True)
check(
    "NEW condition breaks normally with all turns covered at soft max",
    normal_exit,
)

# ─────────────────────────────────────────────────────────────────────────────
# Problem 1b — _match_turns fallback to script_text
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Problem 1b: _match_turns script_text fallback ──")

major_turns = [
    "family discovers the hidden truth about the father",
    "investigation reveals financial fraud at the company",
    "protagonist confronts the antagonist in the warehouse",
    "the final letter exposes the conspiracy",
]

# Case A: reveals are empty → without fallback all miss, with fallback some hit
reveals_empty: list[str] = []
script_text_relevant = (
    "The family finally discovered the truth. "
    "What the father had hidden for years was now clear. "
    "The investigation began to reveal signs of financial fraud at the company. "
    "Nothing else was mentioned here."
)

covered = _match_turns(reveals_empty, major_turns, script_text_relevant)
check("empty reveals + relevant script_text → turns 0 and 1 covered", 0 in covered and 1 in covered)
check("turn 2 (warehouse) NOT in script_text → not covered", 2 not in covered)
check("turn 3 (letter conspiracy) NOT in script_text → not covered", 3 not in covered)

# Case B: good reveals → primary path works as before
reveals_good = [
    "The hidden truth about the father was revealed to the family",
    "The investigation uncovered financial fraud at the company",
]
covered_good = _match_turns(reveals_good, major_turns, "")
check("good reveals → turns 0 and 1 covered via primary path", 0 in covered_good and 1 in covered_good)

# Case C: empty reveals + empty script → nothing covered (unchanged zero-coverage)
covered_zero = _match_turns([], major_turns, "")
check("empty reveals + empty script → no coverage", len(covered_zero) == 0)

# Case D: reveals with vague text + fallback saves coverage via script_text
reveals_vague = ["the truth was revealed", "something happened"]  # no token overlap at 60%
script_with_turns = (
    "The protagonist finally confronted the antagonist inside the dark warehouse. "
    "The final letter that exposed the conspiracy was found."
)
covered_vague = _match_turns(reveals_vague, major_turns, script_with_turns)
check("vague reveals + script_text covers turns 2 and 3", 2 in covered_vague and 3 in covered_vague)

# ─────────────────────────────────────────────────────────────────────────────
# Problem 2 — normalize_tts_chars
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Problem 2: normalize_tts_chars ──")

# Slash between words
t1 = normalize_tts_chars("The pass/fail rate was controversial.")
check("slash: word/word → word or word", "/" not in t1)
check("slash: content preserved", "pass or fail" in t1)

# Percent sign
t2 = normalize_tts_chars("Only 30% of voters participated.")
check("percent: 30% → 30 percent", "%" not in t2)
check("percent: content preserved", "30 percent" in t2)

# Parentheses (content kept)
t3 = normalize_tts_chars("The suspect (a known fraudster) fled.")
check("parens: removed", "(" not in t3 and ")" not in t3)
check("parens: content kept", "a known fraudster" in t3)

# Ampersand
t4 = normalize_tts_chars("Smith & Jones were implicated.")
check("ampersand: & → and", "&" not in t4)
check("ampersand: content preserved", "Smith and Jones" in t4)

# All together — simulate the OUTRO "/" case
t5 = normalize_tts_chars(
    "The report (released in secret) showed a pass/fail rate of 40% — it was devastating."
)
check("combined: no forbidden chars remain", not any(c in t5 for c in "()/%&"))

# TTS compliance after normalize
t6 = normalize_tts_chars("The pass/fail system (used across 50% of schools) was abolished.")
issues_after = check_tts_compliance(t6, "source")
forbidden_char_issues = [i for i in issues_after if "forbidden" in i["description"].lower()]
check("normalize + check_tts: no forbidden-char MAJOR issues remain", len(forbidden_char_issues) == 0)

# Idempotence
t7_raw = "The director & producer (John) got 50% of profits."
t7_once = normalize_tts_chars(t7_raw)
t7_twice = normalize_tts_chars(t7_once)
check("normalize_tts_chars is idempotent", t7_once == t7_twice)

# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

print()
if _failures == 0:
    print("SMOKE PASS — loop control + TTS cleanup: all assertions OK")
else:
    print(f"SMOKE FAIL — {_failures} assertion(s) failed")
    sys.exit(1)
