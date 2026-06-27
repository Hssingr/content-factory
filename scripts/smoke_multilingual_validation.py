"""Phase 13.4 — Multilingual validation gap runtime proof.

Zero live API calls. Stubs only `generate_native_script` (the single
Claude-call boundary `_generate_validated_translated_parent_script()`
touches); `_collect_translated_parent_script_issues()`,
`check_completeness()`, `check_tts_compliance()`, `check_length_coherence()`,
and `generate_multilingual_scripts()`'s control flow are all real,
unmodified code.

Proves, per the Phase 13.4 brief:
  1. A valid translated parent script passes.
  2. A parent translation missing a section fails validation.
  3. A parent translation with malformed section markers fails.
  4. A valid translated child Short passes (Phase 12.4 path, untouched).
  5. A child translation exceeding length tolerance fails (Phase 12.4 path).
  6. A child translation containing section markers fails (Phase 12.4 path).
  7. Retry produces a corrected parent translation.
  8. Retry exhaustion remains non-blocking and logged.
  9. The existing Phase 12.4 multilingual smoke still passes in full.
  10. The existing Phase 13.2 and 13.3 smokes still pass in full.

Run: python scripts/smoke_multilingual_validation.py
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

# ── Fixtures ──────────────────────────────────────────────────────────────────

SOURCE_PARENT = (
    "[INTRO]\n"
    "Children hear a grinding noise from the woods every night for a week.\n"
    "[SECTION 1]\n"
    "Detectives reopened the case the night the second letter arrived at the precinct. "
    "Nobody on the original team had ever mentioned a second letter to the family.\n"
    "[SECTION 2]\n"
    "Investigators returned to find three names gone from the original suspect list. "
    "The only witness who remembered them had quietly moved away long before.\n"
    "[OUTRO]\n"
    "The truth, once buried, finally surfaced thirty years too late."
)

VALID_PARENT_TRANSLATION = (
    "[INTRO]\n"
    "Des enfants entendent un grincement venant des bois chaque nuit pendant une semaine.\n"
    "[SECTION 1]\n"
    "Les enquêteurs ont rouvert l'affaire la nuit où la deuxième lettre est arrivée. "
    "Personne dans l'équipe d'origine n'avait jamais mentionné cette deuxième lettre.\n"
    "[SECTION 2]\n"
    "Les enquêteurs ont découvert que trois noms avaient disparu de la liste des suspects. "
    "Le seul témoin qui se souvenait d'eux avait discrètement déménagé bien avant.\n"
    "[OUTRO]\n"
    "La vérité, une fois enfouie, a finalement refait surface trente ans trop tard."
)

MISSING_SECTION_TRANSLATION = (
    "[INTRO]\n"
    "Des enfants entendent un grincement venant des bois chaque nuit pendant une semaine.\n"
    "[SECTION 1]\n"
    "Les enquêteurs ont rouvert l'affaire la nuit où la deuxième lettre est arrivée. "
    "Personne dans l'équipe d'origine n'avait jamais mentionné cette deuxième lettre.\n"
    "[OUTRO]\n"
    "La vérité, une fois enfouie, a finalement refait surface trente ans trop tard."
)

MALFORMED_MARKER_TRANSLATION = VALID_PARENT_TRANSLATION.replace("[SECTION 1]", "[SECTON 1]")

# ── 1, 2, 3: _collect_translated_parent_script_issues() unit-level proof ──────

issues = scripts_mod._collect_translated_parent_script_issues(
    VALID_PARENT_TRANSLATION, "fr", SOURCE_PARENT,
)
assert_ok("valid translated parent script passes (zero issues)", issues == [], f"{issues}")

issues = scripts_mod._collect_translated_parent_script_issues(
    MISSING_SECTION_TRANSLATION, "fr", SOURCE_PARENT,
)
assert_ok(
    "parent translation missing a section fails validation (section_loss MAJOR)",
    any(i["category"] == "section_loss" for i in issues),
    f"{[i['category'] for i in issues]}",
)
assert_ok(
    "section_loss issue names both the source and translated section counts",
    "2" in issues[0]["description"] and "1" in issues[0]["description"],
    issues[0]["description"],
)

issues = scripts_mod._collect_translated_parent_script_issues(
    MALFORMED_MARKER_TRANSLATION, "fr", SOURCE_PARENT,
)
assert_ok(
    "parent translation with a malformed section marker fails validation "
    "(caught via reused check_completeness() + the new section-count check)",
    len(issues) >= 1 and any(i["category"] in ("completeness", "section_loss") for i in issues),
    f"{[i['category'] for i in issues]}",
)

# Length-parity check, isolated:
too_short = "[INTRO]\n" + ("Court. " * 5) + "\n[SECTION 1]\nTrès court.\n[OUTRO]\nFin."
issues = scripts_mod._collect_translated_parent_script_issues(too_short, "fr", SOURCE_PARENT)
assert_ok(
    "parent translation far shorter than the source fails length_parity",
    any(i["category"] == "length_parity" for i in issues),
    f"{[i['category'] for i in issues]}",
)

# ── 7, 8: full _generate_validated_translated_parent_script() retry loop ────

orig_generate_native_script = scripts_mod.generate_native_script


class _FakeChannel:
    niche = "true crime"
    tone = "documentary"


def _make_stub(responses: list[str]):
    calls: list[dict] = []
    state = {"i": 0}

    def _stub(**kwargs):
        calls.append(kwargs)
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return {"voice_script": responses[i]}
    return _stub, calls


def _run_parent(responses):
    stub, calls = _make_stub(responses)
    scripts_mod.generate_native_script = stub
    try:
        result = scripts_mod._generate_validated_translated_parent_script(
            source_voice_script=SOURCE_PARENT, target_language="fr", channel=_FakeChannel(),
            script_format="youtube_long", audio_tags_enabled=False,
            tts_model="sonic-2", tts_provider="cartesia", hook_context=None,
            content_id="fake-content-id",
        )
    finally:
        scripts_mod.generate_native_script = orig_generate_native_script
    return result, calls


result, calls = _run_parent([MISSING_SECTION_TRANSLATION, VALID_PARENT_TRANSLATION])
assert_ok(
    "retry produces a corrected parent translation: attempt 1 (missing section) "
    "triggers exactly one retry, attempt 2 (valid) is accepted",
    len(calls) == 2 and result is not None and result["voice_script"] == VALID_PARENT_TRANSLATION,
    f"calls={len(calls)}",
)
assert_ok("attempt 1 had no override_instruction", calls[0]["override_instruction"] == "")
assert_ok(
    "attempt 2's override_instruction names the section_loss defect",
    "section" in calls[1]["override_instruction"].lower(),
    calls[1]["override_instruction"],
)
assert_ok("attempt 2 was generated with content_kind='parent_long_form'", calls[1]["content_kind"] == "parent_long_form")

result, calls = _run_parent([MISSING_SECTION_TRANSLATION, MISSING_SECTION_TRANSLATION, MISSING_SECTION_TRANSLATION])
assert_ok(
    "retry exhaustion (every attempt still missing a section) is non-blocking — "
    "_MAX_PARENT_TRANSLATION_CORRECTION_ROUNDS=2 -> 3 total attempts, latest "
    "attempt returned, never raises",
    len(calls) == 3 and result is not None and result["voice_script"] == MISSING_SECTION_TRANSLATION,
    f"calls={len(calls)}",
)

def _raising_stub(**kwargs):
    raise ValueError("simulated Claude failure")


scripts_mod.generate_native_script = _raising_stub
try:
    result = scripts_mod._generate_validated_translated_parent_script(
        source_voice_script=SOURCE_PARENT, target_language="fr", channel=_FakeChannel(),
        script_format="youtube_long", audio_tags_enabled=False,
        tts_model="sonic-2", tts_provider="cartesia", hook_context=None,
        content_id="fake-content-id",
    )
finally:
    scripts_mod.generate_native_script = orig_generate_native_script
assert_ok("generate_native_script() raising returns None (distinct from a validation failure)", result is None)

# ── Confirm generate_multilingual_scripts()'s parent branch actually calls the
#    new validated wrapper, not the old unchecked direct call ─────────────────

import inspect
src = inspect.getsource(scripts_mod.generate_multilingual_scripts)
assert_ok(
    "generate_multilingual_scripts()'s parent (else) branch calls "
    "_generate_validated_translated_parent_script(), not generate_native_script() directly",
    "_generate_validated_translated_parent_script(" in src,
    "source inspection",
)
assert_ok(
    "the old unchecked direct call statement ('= generate_native_script(') is "
    "gone from the function body — only a docstring mention of the underlying "
    "primitive remains, plus the two validated-wrapper call sites",
    "= generate_native_script(" not in src,
    "source inspection",
)

# ── 4, 5, 6: Phase 12.4 child Short translation path — untouched, still active ──

CHILD_SOURCE = (
    "He heard the sound again at midnight. Nobody else in the house woke up. "
    "By morning the door was open and nothing inside had been touched."
)
valid_child = (
    "Il a entendu le bruit a nouveau a minuit. Personne d'autre dans la maison ne s'est reveille. "
    "Au matin la porte etait ouverte et rien a l'interieur n'avait ete touche."
)
issues = scripts_mod._collect_translated_short_script_issues(
    valid_child, "fr", len(CHILD_SOURCE.split()),
)
assert_ok("valid translated child Short passes (Phase 12.4 path, untouched)", issues == [], f"{issues}")

too_long_child = " ".join(["mot"] * 300)
issues = scripts_mod._collect_translated_short_script_issues(
    too_long_child, "fr", len(CHILD_SOURCE.split()),
)
assert_ok(
    "child translation exceeding length tolerance fails (Phase 12.4 path, untouched)",
    any(i["category"] == "translated_short_too_long" for i in issues),
    f"{[i['category'] for i in issues]}",
)

with_marker_child = "[INTRO]\n" + valid_child
issues = scripts_mod._collect_translated_short_script_issues(
    with_marker_child, "fr", len(CHILD_SOURCE.split()),
)
assert_ok(
    "child translation containing section markers fails (Phase 12.4 path, untouched)",
    any(i["category"] == "section_markers_in_short_translation" for i in issues),
    f"{[i['category'] for i in issues]}",
)

# ── Validation matrix sanity check: confirm the documented checks are the real ones ──

assert_ok(
    "check_length_coherence is now wired into generate_multilingual_scripts() "
    "as a cross-language diagnostic",
    "check_length_coherence(" in inspect.getsource(scripts_mod.generate_multilingual_scripts),
)

print()
print("── Re-running Phase 12.4 / 13.2 / 13.3 smokes (regression check) ──────────")
existing_smokes = [
    "scripts/smoke_multilingual_child_short_adaptation.py",
    "scripts/smoke_short_ai_quality_validation.py",
    "scripts/smoke_parent_child_repetition_detector.py",
    "scripts/smoke_story_blueprint_sections.py",
    "scripts/smoke_script_quality_gate_split.py",
    "scripts/smoke_generate_script_sections_split.py",
    "scripts/smoke_standalone_shorts_planner.py",
    "scripts/smoke_shorts_planner_split.py",
]
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for smoke in existing_smokes:
    proc = subprocess.run(
        [sys.executable, smoke], cwd=repo_root, capture_output=True, text=True, timeout=240,
    )
    ok = proc.returncode == 0 and "SMOKE PASS" in proc.stdout
    assert_ok(f"existing smoke still passes: {smoke}", ok, proc.stdout[-300:] if not ok else "")

print()
print("SMOKE PASS")
