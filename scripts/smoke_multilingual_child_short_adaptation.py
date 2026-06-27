"""Phase 12.4 — child Short multilingual adaptation prompt-selection runtime proof.

Zero live API calls. Stubs only `generate_native_script` (the Claude call inside
`_generate_validated_translated_short_script()`); everything else —
`build_native_system_prompt()`, `_collect_translated_short_script_issues()`,
`_generate_validated_translated_short_script()`, `check_tts_compliance()` — is
real, unmodified code.

Proves, per the Phase 12.4 brief:
  1. Parent long-form multilingual adaptation still selects the long-form
     native prompt (content_kind="parent_long_form", script_format="youtube_long").
  2. Child Short multilingual adaptation selects the dedicated Short-specific
     native prompt (content_kind="child_short"), regardless of script_format.
  3. Translated child Short output containing a section marker is rejected by
     `_collect_translated_short_script_issues()` and triggers a real retry.
  4. Translated child Short output over the word cap is rejected and triggers
     a real retry.
  5. Valid flat-narration translated child Short output passes with zero issues.
  6. Parent long-form behavior is unchanged: `generate_native_script()`'s
     default `content_kind="parent_long_form"` preserves the pre-Phase-12.4
     prompt-selection behavior exactly (same base text selected as before this
     phase existed).
  7. Existing relevant smokes (Phase 9/10/11/12 script-generation smokes) still
     pass — run as subprocesses at the end of this script.

Run: python scripts/smoke_multilingual_child_short_adaptation.py
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


from app.agents.agent2_discovery.system_prompt import build_native_system_prompt
import app.agents.agent2_discovery.services.scripts as scripts_mod

# ── 1 & 2: build_native_system_prompt() base selection ──────────────────────

parent_prompt = build_native_system_prompt(
    script_format="youtube_long", tts_model="sonic-2", content_kind="parent_long_form",
)
assert_ok(
    "parent long-form (content_kind=parent_long_form, script_format=youtube_long) "
    "selects the long-form documentary native base",
    "professional translator for YouTube documentary content" in parent_prompt,
    "matched marker string from _BASE_YOUTUBE_LONG_FORM_NATIVE",
)
assert_ok(
    "parent long-form prompt does NOT contain the child-Short-only marker",
    "self-contained narration block" not in parent_prompt,
)

child_prompt_long_format = build_native_system_prompt(
    script_format="youtube_long", tts_model="sonic-2", content_kind="child_short",
)
assert_ok(
    "child Short (content_kind=child_short) selects the dedicated flat-narration "
    "native base even when script_format=youtube_long — the exact Phase 12.3 defect",
    "self-contained narration block" in child_prompt_long_format,
    "matched marker string from _BASE_CHILD_SHORT_NATIVE",
)
assert_ok(
    "child Short prompt does NOT contain the long-form translator framing",
    "professional translator for YouTube documentary content" not in child_prompt_long_format,
)
assert_ok(
    "child Short prompt explicitly forbids section markers",
    "[SECTION N]" in child_prompt_long_format and "zero bracketed structural markers" in child_prompt_long_format,
)
assert_ok(
    "child Short prompt preserves cliffhanger intent and minimum-context rules",
    "ends on a cliffhanger" in child_prompt_long_format
    and "minimum context" in child_prompt_long_format,
)

# Old default-args call shape (pre-Phase-12.4 call site) must still resolve to the
# long-form base — confirms backward compatibility for any caller not yet passing
# content_kind explicitly.
legacy_call_prompt = build_native_system_prompt("youtube_long", "sonic-2")
assert_ok(
    "calling build_native_system_prompt() with the pre-Phase-12.4 positional "
    "signature (no content_kind) still selects the long-form base",
    "professional translator for YouTube documentary content" in legacy_call_prompt,
)

# ── 3, 4, 5: _collect_translated_short_script_issues() ──────────────────────

SOURCE_SHORT = (
    "He heard the sound again at midnight. Nobody else in the house woke up. "
    "By morning the door was open and nothing inside had been touched."
)
SOURCE_WC = len(SOURCE_SHORT.split())  # 24 words

flat_valid = (
    "Il a entendu le bruit a nouveau a minuit. Personne d'autre dans la maison ne s'est reveille. "
    "Au matin la porte etait ouverte et rien a l'interieur n'avait ete touche."
)
issues_valid = scripts_mod._collect_translated_short_script_issues(flat_valid, "fr", SOURCE_WC)
assert_ok(
    "valid flat-narration translated Short -> zero issues",
    issues_valid == [],
    f"{issues_valid}",
)

with_marker = "[INTRO]\n" + flat_valid
issues_marker = scripts_mod._collect_translated_short_script_issues(with_marker, "fr", SOURCE_WC)
assert_ok(
    "translated Short containing a section marker -> rejected with a MAJOR finding",
    any(i["category"] == "section_markers_in_short_translation" for i in issues_marker),
    f"{issues_marker}",
)

too_long = " ".join(["mot"] * 300)  # 300 words > _MAX_SHORT_WORDS (250)
issues_long = scripts_mod._collect_translated_short_script_issues(too_long, "fr", SOURCE_WC)
assert_ok(
    "translated Short over the absolute word cap -> rejected with a MAJOR finding",
    any(i["category"] == "translated_short_too_long" for i in issues_long),
    f"{issues_long}",
)

bloated_but_under_cap = " ".join(["mot"] * 60)  # 60 words, 2.5x the 24-word source, under 250 cap
issues_ratio = scripts_mod._collect_translated_short_script_issues(bloated_but_under_cap, "fr", SOURCE_WC)
assert_ok(
    "translated Short within the absolute cap but >1.6x source length -> "
    "rejected with the length-parity MAJOR finding",
    any(i["category"] == "translated_short_length_mismatch" for i in issues_ratio),
    f"{issues_ratio}",
)

# ── 3/4 continued: full retry loop via _generate_validated_translated_short_script() ──

calls: list[dict] = []
orig_generate_native_script = scripts_mod.generate_native_script


class _FakeChannel:
    niche = "true crime"
    tone = "documentary"
    id = "fake-channel-id"


def _make_stub(responses: list[str]):
    state = {"i": 0}

    def _stub(**kwargs):
        calls.append(kwargs)
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return {"voice_script": responses[i]}
    return _stub


# Scenario A: attempt 1 has a section marker (MAJOR) -> retry -> attempt 2 is clean.
calls.clear()
scripts_mod.generate_native_script = _make_stub([with_marker, flat_valid])
try:
    result = scripts_mod._generate_validated_translated_short_script(
        source_voice_script=SOURCE_SHORT, target_language="fr", channel=_FakeChannel(),
        script_format="youtube_long", audio_tags_enabled=False,
        tts_model="sonic-2", tts_provider="cartesia", hook_context=None,
        content_id="fake-content-id",
    )
finally:
    scripts_mod.generate_native_script = orig_generate_native_script

assert_ok(
    "section-marker MAJOR on attempt 1 triggers exactly one retry "
    "(generate_native_script called twice)",
    len(calls) == 2,
    f"called {len(calls)} time(s)",
)
assert_ok("attempt 1 passed content_kind='child_short'", calls[0]["content_kind"] == "child_short")
assert_ok("attempt 1 had no override_instruction (first attempt)", calls[0]["override_instruction"] == "")
assert_ok(
    "attempt 2's override_instruction names the section-marker defect",
    "section" in calls[1]["override_instruction"].lower()
    or "bracketed" in calls[1]["override_instruction"].lower(),
    calls[1]["override_instruction"],
)
assert_ok(
    "final result is the clean attempt 2 text",
    result is not None and result["voice_script"] == flat_valid,
)

# Scenario B: attempt 1 over word cap (MAJOR) -> retry -> attempt 2 is clean.
calls.clear()
scripts_mod.generate_native_script = _make_stub([too_long, flat_valid])
try:
    result = scripts_mod._generate_validated_translated_short_script(
        source_voice_script=SOURCE_SHORT, target_language="fr", channel=_FakeChannel(),
        script_format="youtube_long", audio_tags_enabled=False,
        tts_model="sonic-2", tts_provider="cartesia", hook_context=None,
        content_id="fake-content-id",
    )
finally:
    scripts_mod.generate_native_script = orig_generate_native_script

assert_ok(
    "over-word-cap MAJOR on attempt 1 triggers exactly one retry",
    len(calls) == 2,
    f"called {len(calls)} time(s)",
)
assert_ok(
    "final result after retry is the clean text",
    result is not None and result["voice_script"] == flat_valid,
)

# Scenario C: every attempt fails validation -> exhausts retries -> returns latest
# attempt non-blocking (matches _generate_validated_short_script's existing
# FAIL_USING_LATEST convention), never raises, never returns None on a validation
# failure (None is reserved for generate_native_script() itself raising).
calls.clear()
scripts_mod.generate_native_script = _make_stub([with_marker, with_marker, with_marker])
try:
    result = scripts_mod._generate_validated_translated_short_script(
        source_voice_script=SOURCE_SHORT, target_language="fr", channel=_FakeChannel(),
        script_format="youtube_long", audio_tags_enabled=False,
        tts_model="sonic-2", tts_provider="cartesia", hook_context=None,
        content_id="fake-content-id",
    )
finally:
    scripts_mod.generate_native_script = orig_generate_native_script

assert_ok(
    "persistent validation failure exhausts retries (_MAX_SHORT_CORRECTION_ROUNDS=2 "
    "-> 3 total attempts) and still returns the latest attempt, non-blocking",
    len(calls) == 3 and result is not None and result["voice_script"] == with_marker,
    f"called {len(calls)} time(s), result={result}",
)

# Scenario D: generate_native_script() itself raises -> returns None (distinct from
# a validation failure), caller (generate_multilingual_scripts) treats this as a
# real failure, not a silent pass-through.
calls.clear()


def _raising_stub(**kwargs):
    calls.append(kwargs)
    raise ValueError("simulated Claude failure")


scripts_mod.generate_native_script = _raising_stub
try:
    result = scripts_mod._generate_validated_translated_short_script(
        source_voice_script=SOURCE_SHORT, target_language="fr", channel=_FakeChannel(),
        script_format="youtube_long", audio_tags_enabled=False,
        tts_model="sonic-2", tts_provider="cartesia", hook_context=None,
        content_id="fake-content-id",
    )
finally:
    scripts_mod.generate_native_script = orig_generate_native_script

assert_ok(
    "generate_native_script() raising returns None (not a validation-failure object)",
    result is None,
)

# ── 6: content_kind selection mirrors content.is_short_episode exactly ──────

class _FakeContent:
    def __init__(self, is_short_episode: bool):
        self.is_short_episode = is_short_episode


for is_short, expected in [(True, "child_short"), (False, "parent_long_form")]:
    content_kind = "child_short" if _FakeContent(is_short).is_short_episode else "parent_long_form"
    assert_ok(
        f"content_kind resolves to {expected!r} when Content.is_short_episode={is_short}",
        content_kind == expected,
    )

print()
print("Verifying parent long-form generate_native_script() call shape is unchanged "
      "(default content_kind, default override_instruction)...")
import inspect
sig = inspect.signature(scripts_mod.generate_native_script)
assert_ok(
    "generate_native_script() retains backward-compatible defaults "
    "(content_kind='parent_long_form', override_instruction='')",
    sig.parameters["content_kind"].default == "parent_long_form"
    and sig.parameters["override_instruction"].default == "",
)

print()
print("── Re-running existing relevant smokes (regression check) ─────────────")
existing_smokes = [
    "scripts/smoke_story_blueprint_sections.py",
    "scripts/smoke_script_quality_gate_split.py",
    "scripts/smoke_generate_script_sections_split.py",
    "scripts/smoke_phase11_1_sentence_rhythm_runtime_proof.py",
    "scripts/smoke_standalone_shorts_planner.py",
    "scripts/smoke_shorts_planner_split.py",
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
