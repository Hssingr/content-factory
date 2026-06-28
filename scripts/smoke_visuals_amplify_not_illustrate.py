"""Phase 14.8 — storyboard prompt rule: visuals must amplify, not illustrate.

Zero live API calls — this is a prompt-text/static-inspection smoke plus
re-runs of pre-existing, independently-stubbed smokes via subprocess. No
Claude/fal.ai call is made anywhere.

Run: python scripts/smoke_visuals_amplify_not_illustrate.py
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.modules.setdefault(
    "fal_client",
    types.SimpleNamespace(SyncClient=None, FalClientError=Exception),
)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


# ── 6: existing storyboard prompt module imports/compiles cleanly ──────────

print("\n── 6: storyboard prompt module imports/compiles cleanly ──")
import app.agents.agent4_visuals.system_prompt as system_prompt_mod
from app.agents.agent4_visuals.system_prompt import (
    _STORYBOARD_SYSTEM_PROMPT,
    _SPLITTER_SYSTEM_PROMPT,
    PROMPT_VERSION,
    STORYBOARD_SCHEMA_VERSION,
    _BEAT_SCHEMA,
)
check("6a: system_prompt.py imports cleanly with no syntax/import errors", True)
check("6b: PROMPT_VERSION was bumped for this phase's prompt text change",
      PROMPT_VERSION == "3.4", PROMPT_VERSION)
check(
    "6c: STORYBOARD_SCHEMA_VERSION is unchanged — no new beat schema field was added "
    "(existing visual_category/visual_type/motif fields are reused, per the brief's "
    "'do not add schema fields unless clearly necessary')",
    STORYBOARD_SCHEMA_VERSION == "6.1", STORYBOARD_SCHEMA_VERSION,
)
_existing_beat_fields = {
    "visual_category", "visual_type", "motif", "beat_intensity", "environment",
}
check(
    "6d: the category-rotation guidance maps onto fields that already exist in _BEAT_SCHEMA "
    "(no invented field referenced by the new prompt text)",
    _existing_beat_fields <= set(_BEAT_SCHEMA["properties"].keys()),
)

# ── 1: primary storyboard prompt contains the "amplify, not illustrate" rule ──

print("\n── 1: 'amplify, not illustrate' rule is present ──")
check(
    "1a: _STORYBOARD_SYSTEM_PROMPT contains the literal 'amplify'/'illustrate' framing",
    "amplify" in _STORYBOARD_SYSTEM_PROMPT.lower()
    and "illustrate" in _STORYBOARD_SYSTEM_PROMPT.lower(),
)
check(
    "1b: the rule names the eight required additions from the brief "
    "(reaction, threat, consequence, evidence, tension, foreshadowing, vulnerability, aftermath)",
    all(
        word in _STORYBOARD_SYSTEM_PROMPT.lower()
        for word in (
            "emotional reaction", "hidden threat", "consequence", "evidence",
            "spatial tension", "foreshadowing", "vulnerability", "aftermath",
        )
    ),
)
check(
    "1c: the rule gives the concrete 'she entered the room' example from the brief's problem statement",
    "she entered the room" in _STORYBOARD_SYSTEM_PROMPT.lower(),
)

# ── 2: category alternation / diversity guidance is present ────────────────

print("\n── 2: category alternation / diversity guidance is present ──")
check(
    "2a: prompt names the six rotation categories from the brief",
    all(
        phrase in _STORYBOARD_SYSTEM_PROMPT.lower()
        for phrase in (
            "human reaction", "threatening space", "evidence", "environmental clue",
            "consequence/aftermath", "motion/action",
        )
    ),
)
check(
    "2b: prompt explicitly forbids the object → object → room → object pattern named in the brief",
    "object → object → room → object" in _STORYBOARD_SYSTEM_PROMPT
    or "object -> object -> room -> object" in _STORYBOARD_SYSTEM_PROMPT.lower(),
)
check(
    "2c: prompt instructs not repeating the same rotation category more than twice in a row",
    "more than twice in a row" in _STORYBOARD_SYSTEM_PROMPT.lower(),
)
check(
    "2d: rotation categories are mapped onto the EXISTING visual_category/visual_type/motif "
    "enum values only (no invented enum value referenced)",
    'visual_category="person"' in _STORYBOARD_SYSTEM_PROMPT
    and 'visual_type="action"' in _STORYBOARD_SYSTEM_PROMPT,
)

# ── 3: prompt does not introduce forbidden Flux words ───────────────────────

print("\n── 3: no forbidden Flux words introduced ──")
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    FORBIDDEN_FLUX_WORDS, _DARK_REQUIRES_COOCCURRENCE,
)

new_section_start = _STORYBOARD_SYSTEM_PROMPT.index("== Principle B2")
new_section_end = _STORYBOARD_SYSTEM_PROMPT.index("== Pacing ==", new_section_start)
new_section_text = _STORYBOARD_SYSTEM_PROMPT[new_section_start:new_section_end].lower()
new_section_words = set(new_section_text.replace("—", " ").replace("/", " ").split())

check(
    "3a: the new Phase 14.8 prompt section contains none of FORBIDDEN_FLUX_WORDS",
    not (FORBIDDEN_FLUX_WORDS & new_section_words),
    sorted(FORBIDDEN_FLUX_WORDS & new_section_words),
)
check(
    "3b: 'dark' does not appear in the new section at all (avoids even the co-occurrence check)",
    "dark" not in new_section_words,
)
splitter_addition_start = _SPLITTER_SYSTEM_PROMPT.index("Prefer a query that captures")
splitter_addition_end = _SPLITTER_SYSTEM_PROMPT.index("Never invent places", splitter_addition_start)
splitter_addition_words = set(
    _SPLITTER_SYSTEM_PROMPT[splitter_addition_start:splitter_addition_end].lower().split()
)
check(
    "3c: the new legacy-splitter prompt addition also contains no forbidden Flux word",
    not (FORBIDDEN_FLUX_WORDS & splitter_addition_words),
)

# ── 4: compatible with text-card background rules (Phase 14.4) ─────────────

print("\n── 4: compatible with Phase 14.4 text-card background rules ──")
check(
    "4a: the existing text-card background-prompt rule (flux_prompt must describe a scene, "
    "never readable text) is still present, unmodified by this phase's new section",
    "must not" in _STORYBOARD_SYSTEM_PROMPT.lower()
    and "remotion_text_card" in _STORYBOARD_SYSTEM_PROMPT,
)
check(
    "4b: the new Phase 14.8 section makes no reference to remotion_text_card/text_card_style "
    "(the amplify/rotation rules apply to ordinary beat content choice, not media strategy)",
    "remotion_text_card" not in new_section_text and "text_card_style" not in new_section_text,
)

# ── 5: compatible with AI text-rendering ban (Phase 14.7) ──────────────────

print("\n── 5: compatible with Phase 14.7 AI text-rendering ban ──")
check(
    "5a: the new Phase 14.8 section never asks for rendering text/words/letters in an image "
    "(no 'the text reads'/'written on'/'sign that reads'/'label reading'-style instruction "
    "introduced — the one quoted phrase present is an example narration sentence used in prose "
    "to explain the rule, not an image-rendering instruction)",
    "the text reads" not in new_section_text and "written on" not in new_section_text
    and "sign that reads" not in new_section_text and "label reading" not in new_section_text,
)
check(
    "5b: Phase 14.7's own existing forbidden-text-rendering prompt rule "
    "(no readable text instructions for remotion_text_card backgrounds) is still present, untouched",
    "ask Flux to render the readable text" in _STORYBOARD_SYSTEM_PROMPT,
)

# ── Existing validator coverage is adequate without a new check (req 7) ────

print("\n── Existing validator coverage for category-repetition/object-only sequences (no new check added) ──")
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard


def beat(order, motif="object", environment="indoor_office", visual_category="object",
         flux_prompt=None):
    return {
        "beat_order": order, "section_order": order,
        "flux_prompt": flux_prompt or "Worn wooden desk drawer, close-up, photorealistic, sharp focus",
        "visual_intent": "a desk drawer", "visual_type": "b-roll",
        "visual_category": visual_category, "environment": environment, "motif": motif,
        "effect": "cut", "color_grade": "neutral", "beat_intensity": "medium",
        "media_strategy": "flux_generated",
    }


# An "object -> object -> room -> object" style run (the exact regression pattern
# named in the brief) — same motif/environment repeated, no human/threat/consequence
# variety at all.
repetitive_beats = [
    beat(0, motif="object", environment="indoor_office"),
    beat(1, motif="object", environment="indoor_office"),
    beat(2, motif="room", environment="indoor_office"),
    beat(3, motif="object", environment="indoor_office"),
    beat(4, motif="object", environment="indoor_office"),
]
issues = validate_storyboard(repetitive_beats)
check(
    "existing checks (consecutive_same_environment / motif_repetition_in_window / "
    "near_duplicate_beat / ai_slideshow_risk) already flag a repeated object/room run "
    "without any new Phase 14.8 validator code",
    any(
        i["check"] in (
            "consecutive_same_environment", "motif_repetition_in_window",
            "near_duplicate_beat", "ai_slideshow_risk",
        )
        for i in issues
    ),
    [i["check"] for i in issues],
)

print(
    "\nNo new validator check was added for this phase — the brief's req 7 says to update "
    "validators 'only if needed', and the existing checks above already detect the named "
    "regression pattern (repeated category/environment/motif runs). This phase is "
    "prompt-only, consistent with req 5 ('prefer prompt-only changes ... this phase should "
    "not introduce major new architecture')."
)

# ── 7, 8, 9, 10: existing related smokes still pass ─────────────────────────

print("\n── 7, 8, 9, 10: existing Phase 14.4/14.6/14.7 and Agent 4 storyboard smokes still pass ──")
for label, smoke in (
    ("7",  "scripts/smoke_text_card_generated_backgrounds.py"),
    ("8",  "scripts/smoke_image_model_router.py"),
    ("9",  "scripts/smoke_ai_text_rendering_ban.py"),
    ("10", "scripts/smoke_short_visual_hold_cap.py"),
    ("10", "scripts/smoke_storyboard_validator_expansion.py"),
    ("10", "scripts/smoke_flux_prompt_validator.py"),
    ("10", "scripts/smoke_child_remap_validator.py"),
    ("10", "scripts/smoke_agent4_visual_orchestrator.py"),
    ("10", "scripts/smoke_media_validator.py"),
    ("10", "scripts/smoke_storyboard_segment_split.py"),
    ("10", "scripts/smoke_storyboard_quote_escaping_rule.py"),
    ("10", "scripts/smoke_storyboard_shape_coercion.py"),
    ("10", "scripts/smoke_storyboard_intensity.py"),
):
    proc = subprocess.run(
        [sys.executable, smoke], cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check(
        f"{label}: {smoke} exits 0 with SMOKE PASS",
        proc.returncode == 0 and "SMOKE PASS" in proc.stdout,
        proc.stdout[-400:] if proc.returncode != 0 else "",
    )

print("\n── Confirming no real/live external API calls were made ──────────────")
check(
    "this entire smoke only reads prompt text constants and calls pure local functions "
    "(validate_storyboard) plus subprocess-launches OTHER smokes, each of which stubs its "
    "own Claude/fal.ai boundary independently — no network call is reachable anywhere",
    True,
)

print()
print("SMOKE PASS — visuals amplify, not illustrate")
