"""Phase 13.3 — Parent/child repetition detector runtime proof.

Zero live API calls. Stubs only `generate_short_episode_script` and
`assess_short_script_quality` (the two Claude-call boundaries in this code
path); `detect_parent_child_overlap()`, `_find_overlap_spans()`,
`_normalize_overlap_tokens()`, `_generate_validated_short_script()`, and
`_collect_short_script_major_issues()` are all real, unmodified code.

Proves, per the Phase 13.3 brief:
  1. A child Short with low/no overlap passes.
  2. A child Short with ~21-24% exact 6-gram reuse from the parent fails.
  3. The specific overlapping excerpts are included in the next attempt's
     override_instruction.
  4. A retry with a rewritten, non-overlapping Short passes.
  5. The AI quality gate is not called for a draft rejected by overlap.
  6. Structural validation still runs, and runs before overlap validation.
  7. A missing/empty parent script skips the overlap check safely (logged,
     not a crash, not treated as pass or fail).
  8. Parent long-form generation/quality-gate behavior is unchanged.
  9. The Phase 13.2 Short AI-quality smoke still passes in full.
  10. The Phase 12.4 multilingual child Short smoke still passes in full.

Run: python scripts/smoke_parent_child_repetition_detector.py
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

# ── 1, 2, 7: detect_parent_child_overlap() unit-level proof ─────────────────

PARENT_SCRIPT = (
    "The records had been sealed for thirty years before anyone thought to ask why. "
    "Detectives reopened the case the night the second letter arrived at the precinct. "
    "Nobody on the original team had ever mentioned a second letter to the family. "
    "By the time investigators returned, three names on the original suspect list "
    "were already gone, and the only witness who remembered them had moved away. "
    "Local records from that era painted a very different picture of what happened "
    "in the days before the disappearance was first reported to the authorities."
)

# A child Short built mostly from original wording but with two genuine verbatim
# runs lifted from the parent — this is what the real 21-24% defect looked like:
# not one giant copy-paste block, but a couple of distinct verbatim spans summing
# to roughly a quarter of the Short's own word count (measured at 24.6% below).
HIGH_OVERLAP_SHORT = (
    "The records had been sealed for thirty years. "
    "A new witness finally agreed to speak on camera after years of silence. "
    "Her account contradicted nearly everything investigators had assumed for decades. "
    "Detectives reopened the case the night the. "
    "phone finally rang at the old farmhouse near the county line. "
    "That single decision changed the direction of the entire inquiry for good."
)

LOW_OVERLAP_SHORT = (
    "A new lead surfaced just before the anniversary of the disappearance. "
    "It came from a neighbor who had stayed quiet for years out of fear. "
    "Her account contradicted everything the original report had claimed. "
    "Investigators are now reopening files nobody expected to see again."
)

overlap = scripts_mod.detect_parent_child_overlap(
    child_voice_script=LOW_OVERLAP_SHORT, parent_voice_script=PARENT_SCRIPT,
    part_n=1, correction_round=1,
)
assert_ok(
    "low/no-overlap Short passes (ratio below threshold, zero issues)",
    overlap is not None and overlap["issues"] == [] and overlap["overlap_ratio"] < 0.05,
    f"ratio={overlap['overlap_ratio']:.3f}",
)

overlap = scripts_mod.detect_parent_child_overlap(
    child_voice_script=HIGH_OVERLAP_SHORT, parent_voice_script=PARENT_SCRIPT,
    part_n=1, correction_round=1,
)
assert_ok(
    "21-24%-style real-defect overlap fails (ratio in the reported defect range, "
    "MAJOR issue returned)",
    overlap is not None and 0.20 <= overlap["overlap_ratio"] <= 0.30 and len(overlap["issues"]) == 1,
    f"ratio={overlap['overlap_ratio']:.3f}, issues={overlap['issues']}",
)
assert_ok(
    "the failing issue is severity=MAJOR, category=parent_child_overlap",
    overlap["issues"][0]["severity"] == "MAJOR"
    and overlap["issues"][0]["category"] == "parent_child_overlap",
)
assert_ok(
    "concrete overlapping excerpts are populated and appear in the issue description",
    len(overlap["excerpts"]) >= 1
    and overlap["excerpts"][0] in overlap["issues"][0]["description"],
    overlap["excerpts"],
)
assert_ok(
    "case/punctuation differences do not affect the result (normalized tokens)",
    scripts_mod.detect_parent_child_overlap(
        child_voice_script=HIGH_OVERLAP_SHORT.upper().replace(".", "!"),
        parent_voice_script=PARENT_SCRIPT, part_n=1, correction_round=1,
    )["overlap_ratio"] == overlap["overlap_ratio"],
)

# Shared names/common short phrases alone (well under the 6-word window) must not fail.
SHARED_NAME_ONLY_SHORT = (
    "A detective named Sarah Connors took over the case after the holidays. "
    "She had never worked a missing-persons file quite like this one before. "
    "Connors started by re-reading every interview from the original investigation. "
    "What she found in the margins changed the direction of the entire case."
)
overlap = scripts_mod.detect_parent_child_overlap(
    child_voice_script=SHARED_NAME_ONLY_SHORT, parent_voice_script=PARENT_SCRIPT,
    part_n=1, correction_round=1,
)
assert_ok(
    "a Short sharing only short common phrases/names with the parent (no 6-word "
    "verbatim run) passes",
    overlap["issues"] == [],
    f"ratio={overlap['overlap_ratio']:.3f}",
)

# Missing/empty parent script -> skip safely, not a crash, not pass/fail.
result_missing = scripts_mod.detect_parent_child_overlap(
    child_voice_script=LOW_OVERLAP_SHORT, parent_voice_script="", part_n=1, correction_round=1,
)
assert_ok("empty parent_voice_script -> detector returns None (skipped, not crashed)", result_missing is None)
result_missing_none = scripts_mod.detect_parent_child_overlap(
    child_voice_script=LOW_OVERLAP_SHORT, parent_voice_script=None, part_n=1, correction_round=1,
)
assert_ok("parent_voice_script=None -> detector returns None (skipped, not crashed)", result_missing_none is None)

# ── 3, 4, 5, 6: full _generate_validated_short_script() retry loop ─────────

orig_generate_short_episode_script = scripts_mod.generate_short_episode_script
orig_assess_short_script_quality = scripts_mod.assess_short_script_quality


class _FakeChannel:
    niche = "true crime"
    tone = "documentary"


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
    calls: list[dict] = []
    state = {"i": 0}

    def _stub(voice_script, channel, is_final_part=True):
        calls.append({"voice_script": voice_script})
        i = min(state["i"], len(reviews) - 1)
        state["i"] += 1
        return reviews[i]
    return _stub, calls


def _passed_review() -> dict:
    return {"status": "PASSED", "issues": []}


PART_PLAN = {"part": 1, "_total_parts": 3, "goal": "g", "opening_hook": "h",
             "main_content_summary": "s", "main_reveal": "r", "cliffhanger": "c"}


def _run(episode_responses, quality_reviews):
    ep_stub, ep_calls = _make_episode_stub(episode_responses)
    q_stub, q_calls = _make_quality_stub(quality_reviews)
    scripts_mod.generate_short_episode_script = ep_stub
    scripts_mod.assess_short_script_quality = q_stub
    try:
        result = scripts_mod._generate_validated_short_script(
            part_plan=PART_PLAN, part_n=1, voice_script=PARENT_SCRIPT,
            blueprint={}, channel=_FakeChannel(), channel_voice=None, source_language="en",
        )
    finally:
        scripts_mod.generate_short_episode_script = orig_generate_short_episode_script
        scripts_mod.assess_short_script_quality = orig_assess_short_script_quality
    return result, ep_calls, q_calls


# Attempt 1 has a real section marker (structural) AND would also overlap heavily —
# proves structural runs first / overlap never evaluated for that attempt, since
# the function `continue`s on the structural MAJOR before reaching the overlap check.
WITH_MARKER_AND_OVERLAP = "[INTRO]\n" + HIGH_OVERLAP_SHORT

result, ep_calls, q_calls = _run(
    [WITH_MARKER_AND_OVERLAP, HIGH_OVERLAP_SHORT, LOW_OVERLAP_SHORT],
    [_passed_review()],
)
assert_ok(
    "structural defect (section marker) on attempt 1 triggers a retry without "
    "ever needing the overlap result for that attempt",
    len(ep_calls) >= 2,
    f"ep_calls={len(ep_calls)}",
)
assert_ok(
    "attempt 2's override_instruction came from the structural check, not overlap "
    "(attempt 1 was rejected for the marker before overlap ever ran)",
    "section" in ep_calls[1]["override_instruction"].lower()
    or "bracketed" in ep_calls[1]["override_instruction"].lower(),
    ep_calls[1]["override_instruction"],
)

# Now isolate the overlap gate itself: attempt 1 is structurally clean but has the
# real-defect-range overlap; attempt 2 is a clean, non-overlapping rewrite.
result, ep_calls, q_calls = _run([HIGH_OVERLAP_SHORT, LOW_OVERLAP_SHORT], [_passed_review()])
assert_ok(
    "structurally-clean but high-overlap Short triggers exactly one retry",
    len(ep_calls) == 2,
    f"ep_calls={len(ep_calls)}",
)
assert_ok(
    "AI quality gate was NOT called for the overlap-rejected attempt 1 — only "
    "once, for the accepted attempt 2",
    len(q_calls) == 1,
    f"q_calls={len(q_calls)}",
)
assert_ok(
    "attempt 2's override_instruction contains a concrete overlapping excerpt "
    "from the parent script, not a generic instruction",
    any(
        excerpt_word in ep_calls[1]["override_instruction"].lower()
        for excerpt_word in ("sealed for thirty years", "reopened the case the night the")
    ),
    ep_calls[1]["override_instruction"],
)
assert_ok(
    "retry with the rewritten non-overlapping Short is accepted",
    result is not None and result["voice_script"] == LOW_OVERLAP_SHORT,
)

# Retry exhaustion: every attempt keeps overlapping -> latest version used, logged,
# never raises.
result, ep_calls, q_calls = _run(
    [HIGH_OVERLAP_SHORT, HIGH_OVERLAP_SHORT, HIGH_OVERLAP_SHORT],
    [_passed_review()],
)
assert_ok(
    "persistent overlap exhausts retries (_MAX_SHORT_CORRECTION_ROUNDS=2 -> 3 "
    "total attempts) and still returns the latest attempt, non-blocking",
    len(ep_calls) == 3 and result is not None and result["voice_script"] == HIGH_OVERLAP_SHORT,
    f"ep_calls={len(ep_calls)}",
)
assert_ok(
    "the AI quality gate was never reached on any of the 3 overlap-failing attempts",
    len(q_calls) == 0,
    f"q_calls={len(q_calls)}",
)

# Low-overlap Short with a parent script available end to end: passes immediately,
# AI quality gate runs exactly once.
result, ep_calls, q_calls = _run([LOW_OVERLAP_SHORT], [_passed_review()])
assert_ok(
    "low-overlap Short passes on attempt 1 with both overlap and AI quality gates clean",
    len(ep_calls) == 1 and len(q_calls) == 1 and result is not None,
)

# ── 8: parent long-form behavior unchanged — re-run its own smokes ─────────

print()
print("── Re-running parent long-form + Phase 13.2 + Phase 12.4 smokes (regression check) ──")
existing_smokes = [
    "scripts/smoke_script_quality_gate_split.py",
    "scripts/smoke_generate_script_sections_split.py",
    "scripts/smoke_generate_section_retry_split.py",
    "scripts/smoke_story_blueprint_sections.py",
    "scripts/smoke_standalone_shorts_planner.py",
    "scripts/smoke_shorts_planner_split.py",
    "scripts/smoke_short_ai_quality_validation.py",
    "scripts/smoke_multilingual_child_short_adaptation.py",
]
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for smoke in existing_smokes:
    proc = subprocess.run(
        [sys.executable, smoke], cwd=repo_root, capture_output=True, text=True, timeout=180,
    )
    ok = proc.returncode == 0 and "SMOKE PASS" in proc.stdout
    assert_ok(f"existing smoke still passes: {smoke}", ok, proc.stdout[-300:] if not ok else "")

print()
print("SMOKE PASS")
