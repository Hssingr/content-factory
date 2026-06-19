"""Story blueprint and section-generation smoke test — zero API calls, zero DB access.

Verifies:
  1. All story blueprint and section-generation symbols import without error.
  2. check_narrative_completeness() returns [] on a passing fixture.
  3. check_narrative_completeness() returns issues on a failing fixture.
  4. _match_turns() correctly computes 60% token overlap.
  5. _payoff_reached() returns True/False correctly.
  6. assemble_script() produces correct [LABEL]\\n\\ntext format.
  7. _get_content_tokens() filters correctly (>3 chars only).

Run: python scripts/smoke_story_blueprint_sections.py
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

from app.agents.agent2_discovery.services.scripts import (
    check_narrative_completeness,
    _match_turns,
    _payoff_reached,
    assemble_script,
    generate_script_sections,
    _get_content_tokens,
)
from app.agents.agent2_discovery.system_prompt import (
    generate_story_blueprint,
    generate_section,
    validate_script_globally,
    _STORY_BLUEPRINT_SYSTEM_PROMPT,
    _SECTION_GENERATION_SYSTEM_PROMPT,
    _GLOBAL_VALIDATION_SYSTEM_PROMPT,
    PROMPT_VERSION,
)

assert_ok(
    "imports and PROMPT_VERSION=4.0",
    PROMPT_VERSION == "4.0",
    f"got {PROMPT_VERSION!r}",
)

# ── Shared blueprint fixture ───────────────────────────────────────────────────

_BLUEPRINT = {
    "hook": "A retired detective found seventy thousand dollars in an abandoned suitcase.",
    "central_question": "Who left the money and why?",
    "major_turns": [
        "The suitcase belonged to a missing banker",
        "The banker had been under police surveillance for fraud",
    ],
    "final_payoff": "The banker staged his own disappearance to escape prosecution.",
    "comment_trigger": "Would you have reported the money or kept it?",
    "suggested_section_count": 3,
    "suggested_title": "The Detective Who Found $70,000 in an Abandoned Suitcase",
}

# ── 2. check_narrative_completeness — PASS fixture ───────────────────────────

_PASS_SCRIPT = """\
[INTRO]
A retired detective found seventy thousand dollars inside an abandoned suitcase on his morning walk.

[SECTION 1]
The suitcase belonged to missing banker Gerald Holt who had disappeared three weeks earlier.
Police had placed Gerald Holt under active surveillance for fraud before he vanished.

[SECTION 2]
Investigators discovered evidence showing the banker staged his own disappearance to escape prosecution.
The money was traced to an offshore account linked to the scheme.

[OUTRO]
The banker staged his own disappearance to escape prosecution, and the money was never recovered.
Would you have reported the money or kept it?
"""

nc_pass = check_narrative_completeness(_PASS_SCRIPT, _BLUEPRINT)
assert_ok(
    "check_narrative_completeness PASS fixture",
    nc_pass == [],
    f"expected [], got {nc_pass}",
)

# ── 3. check_narrative_completeness — FAIL fixture ───────────────────────────

_FAIL_SCRIPT = """\
[INTRO]
In this video we explore a fascinating story about a detective.

[SECTION 1]
A detective found something interesting one day. It was quite surprising.

[OUTRO]
This was an interesting story. Thanks for watching.
"""

nc_fail = check_narrative_completeness(_FAIL_SCRIPT, _BLUEPRINT)
assert_ok(
    "check_narrative_completeness FAIL fixture",
    len(nc_fail) > 0,
    f"expected >=1 issue, got {nc_fail}",
)

# ── 4. _match_turns() ─────────────────────────────────────────────────────────

major_turns = [
    "The suitcase belonged to a missing banker",
    "The banker had been under police surveillance for fraud",
]

reveals_match = [
    "The suitcase was traced back to missing banker Gerald Holt",
    "Gerald Holt had been placed under surveillance by police for fraud",
]
matched = _match_turns(reveals_match, major_turns)
assert_ok(
    "_match_turns high-overlap returns {0,1}",
    matched == {0, 1},
    f"got {matched}",
)

reveals_no_match = ["The dog ran across the field", "Sunlight filtered through the trees"]
no_matched = _match_turns(reveals_no_match, major_turns)
assert_ok(
    "_match_turns low-overlap returns empty set",
    no_matched == set(),
    f"got {no_matched}",
)

# ── 5. _payoff_reached() ─────────────────────────────────────────────────────

section_with_payoff = {
    "script_text": "The banker staged his own disappearance to escape prosecution.",
    "reveals": ["He staged his disappearance to avoid prosecution"],
}
assert_ok(
    "_payoff_reached True when payoff tokens overlap",
    _payoff_reached(section_with_payoff, _BLUEPRINT) is True,
)

section_without_payoff = {
    "script_text": "The detective examined the suitcase carefully.",
    "reveals": ["The suitcase was found on a park bench"],
}
assert_ok(
    "_payoff_reached False when payoff tokens absent",
    _payoff_reached(section_without_payoff, _BLUEPRINT) is False,
)

# ── 6. assemble_script() ─────────────────────────────────────────────────────

sections = [
    {"label": "INTRO",     "script_text": "First sentence of intro."},
    {"label": "SECTION 1", "script_text": "Body of section one."},
    {"label": "SECTION 2", "script_text": "Body of section two."},
    {"label": "OUTRO",     "script_text": "Final resolution."},
]
voice_script, video_script = assemble_script(sections)

assert_ok(
    "assemble_script markers and voice==video",
    all(m in voice_script for m in ("[INTRO]", "[SECTION 1]", "[SECTION 2]", "[OUTRO]"))
    and voice_script == video_script
    and "First sentence of intro." in voice_script
    and "Final resolution." in voice_script,
    f"voice_script excerpt: {voice_script[:120]!r}",
)

# ── 7. _get_content_tokens() ─────────────────────────────────────────────────

tokens = _get_content_tokens("The quick brown fox jumped over the lazy dogs")
assert_ok(
    "_get_content_tokens filters <=3-char words",
    "quick" in tokens
    and "brown" in tokens
    and "jumped" in tokens
    and "the" not in tokens
    and "fox" not in tokens,
    f"tokens={tokens}",
)

# ── Done ──────────────────────────────────────────────────────────────────────

print()
print("SMOKE PASS")
