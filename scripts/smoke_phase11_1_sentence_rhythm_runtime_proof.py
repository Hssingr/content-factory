"""Phase 11.1 — sentence-rhythm-variance checker runtime proof.

Zero live API calls — stubs only `generate_section` (the Claude call inside
`_call_section_generation()`). Everything else — `_generate_section_with_retry()`,
`_collect_section_retry_issues()`, `_build_section_retry_instruction()`,
`check_sentence_rhythm_variance()`, `check_tts_compliance()` — is real,
unmodified code.

Proves, per the Phase 11.1 brief:
  1. A stubbed section response with 4 consecutive 12-18-word sentences
     produces a MINOR finding from check_sentence_rhythm_variance().
  2. A stubbed section response with proper short/long alternation produces
     zero findings from the new checker.
  3. A rhythm-only MINOR (no MAJOR present) does NOT by itself trigger a
     retry — generate_section() is called exactly once.
  4. When a MAJOR from another check (TTS digit-run) IS present alongside a
     rhythm MINOR, the rhythm finding folds into the next retry's
     override_instruction via the exact same mechanism
     check_section_transition()'s MINOR findings already use
     (_build_section_retry_instruction) — not a duplicate/parallel path.

Run: python scripts/smoke_phase11_1_sentence_rhythm_runtime_proof.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]" + (f" — {detail}" if detail else ""))


from app.services.script_checks import check_sentence_rhythm_variance

# ── 1 & 2: unit-level proof directly against the new checker ────────────────

FLAT_RHYTHM_TEXT = (
    "The old mine had been sealed for decades before anyone thought to ask why it mattered. "
    "Workers who once spent their whole lives underground simply stopped talking about it entirely. "
    "Records from that era were either lost or quietly destroyed by people who knew better. "
    "Nobody who still lived in town wanted to explain what had actually happened down there."
)
ALTERNATING_TEXT = (
    "He filed it away. "
    "The sound returned almost every night for nearly a week before anyone else noticed it. "
    "Then his sister vanished too. "
    "Nobody explained why a search party was never sent out to look for her properly."
)
MAJOR_PLUS_RHYTHM_TEXT = (
    "The old mine had been sealed in 1847 before anyone thought to ask why it mattered. "
    "Workers who once spent their whole lives underground simply stopped talking about it entirely. "
    "Records from that era were either lost or quietly destroyed by people who knew better. "
    "Nobody who still lived in town wanted to explain what had actually happened down there."
)
FIXED_TEXT = (
    "He filed it away. "
    "Workers who once spent their whole lives underground simply stopped talking about it entirely. "
    "Then his sister vanished too. "
    "Nobody who still lived in town wanted to explain what had actually happened down there."
)

flat_issues = check_sentence_rhythm_variance(FLAT_RHYTHM_TEXT)
assert_ok(
    "4 consecutive long (12-18 word) sentences -> 1 MINOR rhythm finding",
    len(flat_issues) == 1 and flat_issues[0]["severity"] == "MINOR"
    and flat_issues[0]["category"] == "sentence_rhythm_variance",
    f"{flat_issues}",
)
assert_ok(
    "finding correctly identifies the 'long' band and a run of 4",
    "4 consecutive" in flat_issues[0]["description"] and "long" in flat_issues[0]["description"],
    flat_issues[0]["description"],
)

alt_issues = check_sentence_rhythm_variance(ALTERNATING_TEXT)
assert_ok(
    "proper short/long alternation -> zero findings",
    alt_issues == [],
    f"{alt_issues}",
)

# ── 3 & 4: real _generate_section_with_retry() chain, generate_section() stubbed ──

import app.agents.agent2_discovery.services.scripts as scripts_mod

orig_generate_section = scripts_mod.generate_section
calls: list[dict] = []


class _FakeStory:
    title = "t"
    url = "https://example.invalid"
    language = "en"
    body = "source body"


class _FakeChannel:
    niche = "history"
    tone = "documentary"


def _make_stub(responses: list[str]):
    """Return a stub generate_section() that yields one text per call, in order."""
    state = {"i": 0}

    def _stub(**kwargs):
        calls.append(kwargs)
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return {
            "script_text": responses[i],
            "summary": "s",
            "reveals": [],
            "open_questions": [],
            "suggests_outro": False,
            "visual_intent": {"section_goal": "g", "primary_visual_focus": "f", "avoid_repeating": []},
        }
    return _stub


# Scenario A: rhythm-only MINOR, no MAJOR anywhere -> must NOT retry on its own.
calls.clear()
scripts_mod.generate_section = _make_stub([FLAT_RHYTHM_TEXT])
try:
    result = scripts_mod._generate_section_with_retry(
        label="[SECTION 1]", story=_FakeStory(), blueprint={}, prior_sections_summary=[],
        visual_intent_accumulator={"avoid_repeating": []}, channel=_FakeChannel(),
        script_format="youtube_long", tts_model="sonic-2", tts_provider="cartesia",
        audio_tags_enabled=False, check_hook=False, prior_summary_text="",
    )
finally:
    scripts_mod.generate_section = orig_generate_section

assert_ok(
    "rhythm-only MINOR (no MAJOR) does not trigger a retry — generate_section called exactly once",
    len(calls) == 1,
    f"called {len(calls)} time(s)",
)
assert_ok("section accepted despite the rhythm MINOR (advisory only)", result is not None and result["script_text"] == FLAT_RHYTHM_TEXT)

# Scenario B: a real MAJOR (digit-run) + the same flat rhythm pattern on attempt 1;
# attempt 2 fixes the MAJOR but keeps good rhythm — confirm the override_instruction
# attempt 2 actually received contains the rhythm finding's description, via the
# exact same _build_section_retry_instruction() mechanism transition_issues uses.
calls.clear()
scripts_mod.generate_section = _make_stub([MAJOR_PLUS_RHYTHM_TEXT, FIXED_TEXT])
try:
    result = scripts_mod._generate_section_with_retry(
        label="[SECTION 1]", story=_FakeStory(), blueprint={}, prior_sections_summary=[],
        visual_intent_accumulator={"avoid_repeating": []}, channel=_FakeChannel(),
        script_format="youtube_long", tts_model="sonic-2", tts_provider="cartesia",
        audio_tags_enabled=False, check_hook=False, prior_summary_text="",
    )
finally:
    scripts_mod.generate_section = orig_generate_section

assert_ok("MAJOR present -> a retry happened (generate_section called twice)", len(calls) == 2, f"called {len(calls)} time(s)")
assert_ok("attempt 1 had no override (first attempt)", calls[0]["override_instruction"] == "")
attempt_2_override = calls[1]["override_instruction"]
assert_ok(
    "attempt 2's override_instruction contains the rhythm MINOR's description "
    "(same fold-in mechanism check_section_transition's MINOR findings already use)",
    "consecutive sentences are all 'long' length" in attempt_2_override,
    attempt_2_override,
)
assert_ok(
    "attempt 2's override_instruction also contains the MAJOR (digit-run) description",
    "digit" in attempt_2_override.lower(),
    attempt_2_override,
)
assert_ok(
    "section ultimately accepted once attempt 2's text is clean",
    result is not None and result["script_text"] == FIXED_TEXT,
)

print()
print("SMOKE PASS")
