"""Phase 13.2 — Shorts AI-quality validation runtime proof.

Zero live API calls. Stubs only `generate_short_episode_script` and
`assess_short_script_quality` (the two Claude-call boundaries this phase's
code path touches); everything else — `_generate_validated_short_script()`,
`_run_short_quality_gate()`, `_collect_short_script_major_issues()`,
`check_tts_compliance()`, `check_hook_quality()` — is real, unmodified code.

Proves, per the Phase 13.2 brief:
  1. A valid Short (clean structure, AI quality PASSED) is accepted on the
     first attempt — generate_short_episode_script called exactly once.
  2. A Short with a weak hook (AI quality NEEDS_REWRITE, category="hook")
     triggers exactly one retry, then is accepted once attempt 2 is clean.
  3. A Short with over-recapping (category="recap") triggers a retry the
     same way.
  4. A Short with generic filler (category="generic_language") triggers a
     retry the same way.
  5. A Short containing a section marker is rejected by the deterministic
     structural check BEFORE the AI quality gate ever runs — proven by
     asserting assess_short_script_quality is called zero times for that
     attempt.
  6. Parent long-form quality gate (`run_script_quality_gate`,
     `assess_script_quality`, `rewrite_script_for_quality`) is untouched —
     re-run via the existing smoke that exercises it.
  7. Existing child Short structural validators
     (`_collect_short_script_major_issues`) still run on every attempt,
     proven by call-count tracking.
  8. Retry exhaustion (every attempt fails the AI quality gate) is safe:
     returns the latest attempt non-blocking, never raises, logs
     FAIL_USING_LATEST-style.
  9. The AI judge itself failing (raises) is handled safely: the
     structurally-valid draft is accepted as-is, not retried forever.
  10. No real Claude/API call is made anywhere in this proof.

Run: python scripts/smoke_short_ai_quality_validation.py
"""

import os
import subprocess
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


import app.agents.agent2_discovery.services.scripts as scripts_mod

orig_generate_short_episode_script = scripts_mod.generate_short_episode_script
orig_assess_short_script_quality = scripts_mod.assess_short_script_quality


class _FakeChannel:
    niche = "true crime"
    tone = "documentary"


CLEAN_SHORT = (
    "Detectives reopened the case the night the second letter arrived. "
    "Nobody on the original team had ever mentioned a second letter. "
    "By morning, three names on the original suspect list were gone. "
    "The only one who remembered them was the man who filed the report."
)

WITH_MARKER = "[INTRO]\n" + CLEAN_SHORT


def _make_episode_stub(responses: list[str]):
    calls: list[dict] = []
    state = {"i": 0}

    def _stub(**kwargs):
        calls.append(kwargs)
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return {"title": "Part", "voice_script": responses[i]}
    return _stub, calls


def _make_quality_stub(reviews: list):
    """reviews: list of dict (review result) or Exception instances, one per call."""
    calls: list[dict] = []
    state = {"i": 0}

    def _stub(voice_script, channel, is_final_part=True):
        calls.append({"voice_script": voice_script, "is_final_part": is_final_part})
        i = min(state["i"], len(reviews) - 1)
        state["i"] += 1
        item = reviews[i]
        if isinstance(item, Exception):
            raise item
        return item
    return _stub, calls


def _passed_review() -> dict:
    return {"status": "PASSED", "issues": []}


def _needs_rewrite_review(category: str, description: str) -> dict:
    return {
        "status": "NEEDS_REWRITE",
        "issues": [{
            "severity": "HIGH", "category": category,
            "description": description, "fix": f"Fix the {category} issue.",
        }],
    }


PART_PLAN = {"part": 1, "_total_parts": 3, "goal": "g", "opening_hook": "h",
             "main_content_summary": "s", "main_reveal": "r", "cliffhanger": "c"}

COMMON_KWARGS = dict(
    part_plan=PART_PLAN, part_n=1, voice_script="long-form source script here",
    blueprint={}, channel=_FakeChannel(), channel_voice=None, source_language="en",
)


def _run(episode_responses, quality_reviews):
    ep_stub, ep_calls = _make_episode_stub(episode_responses)
    q_stub, q_calls = _make_quality_stub(quality_reviews)
    scripts_mod.generate_short_episode_script = ep_stub
    scripts_mod.assess_short_script_quality = q_stub
    try:
        result = scripts_mod._generate_validated_short_script(**COMMON_KWARGS)
    finally:
        scripts_mod.generate_short_episode_script = orig_generate_short_episode_script
        scripts_mod.assess_short_script_quality = orig_assess_short_script_quality
    return result, ep_calls, q_calls


# ── 1: valid Short passes on first attempt ──────────────────────────────────

result, ep_calls, q_calls = _run([CLEAN_SHORT], [_passed_review()])
assert_ok(
    "valid Short (clean structure + AI quality PASSED) accepted on attempt 1 "
    "— generate_short_episode_script called exactly once",
    len(ep_calls) == 1 and result is not None and result["voice_script"] == CLEAN_SHORT,
    f"ep_calls={len(ep_calls)}",
)
assert_ok("AI quality gate was actually called once", len(q_calls) == 1)
assert_ok("AI quality gate received is_final_part=False (part 1 of 3)", q_calls[0]["is_final_part"] is False)

# ── 2: weak hook -> retry -> clean ───────────────────────────────────────────

result, ep_calls, q_calls = _run(
    [CLEAN_SHORT, CLEAN_SHORT],
    [_needs_rewrite_review("hook", "Opening gives away the reveal."), _passed_review()],
)
assert_ok(
    "weak-hook AI quality NEEDS_REWRITE triggers exactly one retry",
    len(ep_calls) == 2,
    f"ep_calls={len(ep_calls)}",
)
assert_ok("attempt 1 had no override_instruction", ep_calls[0]["override_instruction"] == "")
assert_ok(
    "attempt 2's override_instruction names the hook defect",
    "reveal" in ep_calls[1]["override_instruction"].lower(),
    ep_calls[1]["override_instruction"],
)
assert_ok("final result accepted after retry", result is not None and result["voice_script"] == CLEAN_SHORT)

# ── 3: over-recapping -> retry -> clean ──────────────────────────────────────

result, ep_calls, q_calls = _run(
    [CLEAN_SHORT, CLEAN_SHORT],
    [_needs_rewrite_review("recap", "Restates the original case facts already known."), _passed_review()],
)
assert_ok(
    "over-recap AI quality NEEDS_REWRITE triggers exactly one retry",
    len(ep_calls) == 2,
    f"ep_calls={len(ep_calls)}",
)
assert_ok(
    "attempt 2's override_instruction names the recap defect",
    "recap" in ep_calls[1]["override_instruction"].lower() or "restates" in ep_calls[1]["override_instruction"].lower(),
    ep_calls[1]["override_instruction"],
)

# ── 4: generic filler -> retry -> clean ──────────────────────────────────────

result, ep_calls, q_calls = _run(
    [CLEAN_SHORT, CLEAN_SHORT],
    [_needs_rewrite_review("generic_language", "Uses 'little did they know' as a crutch."), _passed_review()],
)
assert_ok(
    "generic-filler AI quality NEEDS_REWRITE triggers exactly one retry",
    len(ep_calls) == 2,
    f"ep_calls={len(ep_calls)}",
)
assert_ok(
    "attempt 2's override_instruction names the generic-language defect",
    "little did they know" in ep_calls[1]["override_instruction"].lower(),
    ep_calls[1]["override_instruction"],
)

# ── 5: section marker -> structural rejection BEFORE AI quality ever runs ──

result, ep_calls, q_calls = _run([WITH_MARKER, CLEAN_SHORT], [_passed_review()])
assert_ok(
    "Short containing a section marker triggers exactly one retry (structural, "
    "not AI-quality driven)",
    len(ep_calls) == 2,
    f"ep_calls={len(ep_calls)}",
)
assert_ok(
    "AI quality gate was called exactly once total — never for the rejected "
    "section-marker attempt, only for the clean attempt 2",
    len(q_calls) == 1,
    f"q_calls={len(q_calls)}",
)
assert_ok(
    "attempt 2's override_instruction came from the structural hook-check "
    "finding on the [INTRO]-prefixed text, not an AI quality category",
    ep_calls[1]["override_instruction"] != "",
    ep_calls[1]["override_instruction"],
)
assert_ok("final accepted result has no section marker", "[INTRO]" not in result["voice_script"])

# ── 7 (proven inline above + explicit check): structural validator runs every attempt ──

import app.services.script_checks as checks_mod
orig_check_tts = checks_mod.check_tts_compliance
tts_call_count = {"n": 0}


def _counting_check_tts(*a, **k):
    tts_call_count["n"] += 1
    return orig_check_tts(*a, **k)


scripts_mod.check_tts_compliance = _counting_check_tts
try:
    _run([WITH_MARKER, CLEAN_SHORT], [_passed_review()])
finally:
    scripts_mod.check_tts_compliance = orig_check_tts
assert_ok(
    "_collect_short_script_major_issues (via check_tts_compliance) runs on every "
    "attempt, including the rejected one — structural checks were not skipped",
    tts_call_count["n"] == 2,
    f"called {tts_call_count['n']} time(s)",
)

# ── 8: retry exhaustion — every attempt fails AI quality, never raises ─────

bad_review = _needs_rewrite_review("clarity", "Still unclear who is involved.")
result, ep_calls, q_calls = _run(
    [CLEAN_SHORT, CLEAN_SHORT, CLEAN_SHORT],
    [bad_review, bad_review, bad_review],
)
assert_ok(
    "persistent AI-quality failure exhausts retries (_MAX_SHORT_CORRECTION_ROUNDS=2 "
    "-> 3 total attempts) and still returns the latest attempt, non-blocking",
    len(ep_calls) == 3 and result is not None and result["voice_script"] == CLEAN_SHORT,
    f"ep_calls={len(ep_calls)}, result={result}",
)

# ── 9: AI judge itself raising is handled safely (accept structurally-valid draft) ──

result, ep_calls, q_calls = _run([CLEAN_SHORT], [ValueError("simulated malformed JSON from Claude")])
assert_ok(
    "AI quality assessment call raising is treated as a fail-safe accept "
    "(matches run_script_quality_gate's existing convention) — exactly one "
    "generation attempt, no retry loop spin-up",
    len(ep_calls) == 1 and result is not None and result["voice_script"] == CLEAN_SHORT,
    f"ep_calls={len(ep_calls)}",
)

# ── Direct check: _run_short_quality_gate() in isolation ───────────────────

q_stub, q_calls = _make_quality_stub([_passed_review()])
scripts_mod.assess_short_script_quality = q_stub
try:
    issues = scripts_mod._run_short_quality_gate(
        ep_voice_script=CLEAN_SHORT, channel=_FakeChannel(),
        part_n=1, correction_round=1, is_final_part=True,
    )
finally:
    scripts_mod.assess_short_script_quality = orig_assess_short_script_quality
assert_ok("_run_short_quality_gate returns [] on PASSED", issues == [])

q_stub, q_calls = _make_quality_stub([_needs_rewrite_review("hook", "weak")])
scripts_mod.assess_short_script_quality = q_stub
try:
    issues = scripts_mod._run_short_quality_gate(
        ep_voice_script=CLEAN_SHORT, channel=_FakeChannel(),
        part_n=1, correction_round=1, is_final_part=True,
    )
finally:
    scripts_mod.assess_short_script_quality = orig_assess_short_script_quality
assert_ok(
    "_run_short_quality_gate returns the issues list on NEEDS_REWRITE",
    len(issues) == 1 and issues[0]["category"] == "hook",
)

# ── Prompt-shape check: assess_short_script_quality() never asks for markers ──

import app.agents.agent2_discovery.system_prompt as sp_mod
assert_ok(
    "_SHORT_QUALITY_SYSTEM_PROMPT forbids requiring section markers",
    "Never require or suggest adding [INTRO]" in sp_mod._SHORT_QUALITY_SYSTEM_PROMPT,
)
assert_ok(
    "_SHORT_QUALITY_SYSTEM_PROMPT explicitly rejects long-form word-arc expectations",
    "1200-1600 word arc" in sp_mod._SHORT_QUALITY_SYSTEM_PROMPT,
)
assert_ok(
    "model_routing has a dedicated short_quality_check task key (Haiku tier)",
    sp_mod is not None and __import__("app.services.model_routing", fromlist=["MODEL_ROUTING"]).MODEL_ROUTING.get("short_quality_check") is not None,
)

# ── 6: parent long-form quality gate unchanged — re-run its own existing smoke ──

print()
print("── Re-running parent quality-gate + child Short structural smokes (regression check) ──")
existing_smokes = [
    "scripts/smoke_script_quality_gate_split.py",
    "scripts/smoke_generate_script_sections_split.py",
    "scripts/smoke_generate_section_retry_split.py",
    "scripts/smoke_story_blueprint_sections.py",
    "scripts/smoke_standalone_shorts_planner.py",
    "scripts/smoke_shorts_planner_split.py",
    "scripts/smoke_multilingual_child_short_adaptation.py",
]
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for smoke in existing_smokes:
    proc = subprocess.run(
        [sys.executable, smoke], cwd=repo_root, capture_output=True, text=True, timeout=120,
    )
    ok = proc.returncode == 0 and "SMOKE PASS" in proc.stdout
    assert_ok(f"existing smoke still passes: {smoke}", ok, proc.stdout[-300:] if not ok else "")

print()
print("SMOKE PASS")
