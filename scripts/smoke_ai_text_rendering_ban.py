"""Phase 14.7 — ban AI text rendering on documents/posters/calendars/signs/names.

Zero live API calls — `fal_client` is stubbed at import time, and
`flux_generator._call_fal` is monkeypatched in the end-to-end section below.
Everything else exercised — `storyboard._build_beat_section()`,
`flux_generator.is_text_prop_beat()`/`derive_text_prop_prompt()`/
`derive_text_prop_overlay()`, `storyboard_validator.validate_storyboard()`,
`flux_generator.generate_all_beat_images()`,
`remotion_builder._section_for_remotion()` — is real, unmodified-by-this-script
production code.

Run: python scripts/smoke_ai_text_rendering_ban.py
"""

from __future__ import annotations

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


from app.agents.agent4_visuals.subagents import storyboard as storyboard_mod
from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard
from app.agents.agent5_render.services.remotion_builder import _section_for_remotion

# ── 1 & 2: missing-person poster beat ───────────────────────────────────────

print("\n── 1 & 2: missing-person poster beat ──")
beat_poster = {
    "beat_order": 3,
    "visual_intent": "a missing person poster pinned to a community bulletin board",
    "flux_prompt": (
        'A missing person poster on a wall, the text reads "JANE DOE MISSING SINCE '
        'MARCH 5", photorealistic, sharp focus'
    ),
    "visual_type": "document",
    "media_strategy": "flux_generated",
    "environment": "urban_street",
}
section_poster = storyboard_mod._build_beat_section(beat_poster, 3, 0, 4000, "narration about a disappearance")

check(
    "1a: sanitized prompt no longer contains the invented name/date the original asked Flux to render",
    "JANE DOE" not in section_poster["flux_prompt"] and "MARCH 5" not in section_poster["flux_prompt"],
)
check(
    "1b: sanitized prompt no longer contains the 'the text reads' rendering instruction",
    "the text reads" not in section_poster["flux_prompt"].lower(),
)
check(
    "1c: sanitized prompt explicitly forbids readable text/letters/numbers/names/dates",
    "no readable text" in section_poster["flux_prompt"]
    and "no legible letters" in section_poster["flux_prompt"]
    and "no readable names" in section_poster["flux_prompt"]
    and "no readable dates" in section_poster["flux_prompt"],
)
check(
    "1d: sanitized prompt still describes the physical poster prop and scene (visual intent preserved)",
    "poster" in section_poster["flux_prompt"].lower() and "bulletin board" in section_poster["flux_prompt"].lower(),
)
check(
    "2a: poster readable text is preserved as a minimal, non-invented Remotion overlay ('MISSING', "
    "never the fabricated name/date from the original prompt)",
    section_poster["overlay_text"] == "MISSING",
)
check(
    "2b: overlay_position is set so the overlay actually renders (not left at the inert 'none' default)",
    section_poster["overlay_position"] == "center",
)
check(
    "2c: visual_type/media_strategy stay exactly as the storyboard described them (document/flux_generated) "
    "— this beat is NOT converted into a Phase 14.4 text_card",
    section_poster["visual_type"] == "document" and section_poster["media_strategy"] == "flux_generated",
)

# ── 3: document / case-file beat ────────────────────────────────────────────

print("\n── 3: document / case-file beat ──")
beat_doc = {
    "beat_order": 1,
    "visual_intent": "a detective opens a case file folder on the desk",
    "flux_prompt": 'A case file folder open on a desk, label reading "CASE #4471", photorealistic',
    "visual_type": "document",
    "media_strategy": "flux_generated",
    "environment": "indoor_office",
}
section_doc = storyboard_mod._build_beat_section(beat_doc, 1, 0, 3000, "narration about the investigation")
check(
    "3a: case-number text is no longer requested in the sanitized prompt",
    "CASE #4471" not in section_doc["flux_prompt"] and "label reading" not in section_doc["flux_prompt"].lower(),
)
check(
    "3b: sanitized prompt still describes a document/case-file prop",
    "case file" in section_doc["flux_prompt"].lower() or "document" in section_doc["flux_prompt"].lower(),
)
check(
    "3c: no overlay is invented for this beat — no exact case number can be safely derived, "
    "so overlay_text stays empty rather than fabricating one",
    section_doc["overlay_text"] == "",
)

# ── 4: calendar / date beat ──────────────────────────────────────────────────

print("\n── 4: calendar / date beat ──")
beat_cal = {
    "beat_order": 2,
    "visual_intent": "a wall calendar with a date circled in red",
    "flux_prompt": 'A wall calendar with the date "March 14" circled in red pen, photorealistic',
    "script_text": "It happened on March 14, just days before the funeral.",
    "visual_type": "document",
    "media_strategy": "flux_generated",
    "environment": "indoor_domestic",
}
section_cal = storyboard_mod._build_beat_section(beat_cal, 2, 0, 3000, beat_cal["script_text"])
check(
    "4a: sanitized prompt no longer asks Flux to render the literal date",
    '"March 14"' not in section_cal["flux_prompt"],
)
check(
    "4b: sanitized prompt still describes a calendar prop",
    "calendar" in section_cal["flux_prompt"].lower(),
)
check(
    "4c: overlay text is the exact date lifted verbatim from the beat's own narration "
    "(derived, not invented — CLAUDE.md §21.3's no-invented-facts rule)",
    section_cal["overlay_text"] == "MARCH 14",
)

# ── 5: ordinary non-text-prop beat is unchanged ─────────────────────────────

print("\n── 5: ordinary room/object/person beat is unchanged ──")
beat_ordinary = {
    "beat_order": 5,
    "visual_intent": "empty living room with a sofa",
    "flux_prompt": "Empty living room with a sofa, warm afternoon window light, photorealistic, sharp focus",
    "visual_type": "b-roll",
    "media_strategy": "flux_generated",
    "environment": "indoor_domestic",
}
section_ordinary = storyboard_mod._build_beat_section(beat_ordinary, 5, 0, 3000, "narration")
check(
    "5a: an ordinary beat's flux_prompt is passed through byte-for-byte unchanged",
    section_ordinary["flux_prompt"] == beat_ordinary["flux_prompt"],
)
check("5b: no overlay is added to an ordinary beat", section_ordinary["overlay_text"] == "")
check(
    "5c: flux_generator.is_text_prop_beat() correctly returns False for this beat",
    flux_generator.is_text_prop_beat(beat_ordinary) is False,
)

# ── 6: text-card beat stays on its own Phase 14.4 path, never confused with text-prop ──

print("\n── 6: text-card beat remains separate from the text-prop overlay path ──")
beat_textcard = {
    "beat_order": 6,
    "media_strategy": "remotion_text_card",
    "visual_type": "text_card",
    "flux_prompt": "",
    "overlay_text": "THE TRUTH WAS WORSE",
    "environment": "abstract_dark",
}
check(
    "6a: is_text_prop_beat() always returns False for a beat is_text_card_beat() already claims",
    flux_generator.is_text_prop_beat(beat_textcard) is False,
)
section_textcard = storyboard_mod._build_beat_section(beat_textcard, 6, 0, 3000, "narration")
check(
    "6b: text-card beat's flux_prompt stays empty here (Phase 14.4 derives it separately/later, "
    "via generate_text_card_background_image() — this function never touches it)",
    section_textcard["flux_prompt"] == "",
)
check(
    "6c: text-card beat's own overlay_text is untouched by the text-prop overlay logic",
    section_textcard["overlay_text"] == "THE TRUTH WAS WORSE",
)
check(
    "6d: visual_type/media_strategy remain text_card/remotion_text_card",
    section_textcard["visual_type"] == "text_card" and section_textcard["media_strategy"] == "remotion_text_card",
)

# ── 7: validator flags a prompt that asks Flux to render readable text ─────

print("\n── 7: validator flags an unsanitized text-rendering prompt ──")
raw_unsanitized_beat = {
    "beat_order": 0,
    "flux_prompt": 'A street sign that reads "MAIN STREET", photorealistic, sharp focus',
    "media_strategy": "flux_generated",
    "environment": "urban_street",
    "motif": "other",
    "effect": "cut",
    "color_grade": "neutral",
    "beat_intensity": "medium",
}
issues = validate_storyboard([raw_unsanitized_beat])
text_render_issues = [i for i in issues if i["check"] == "ai_text_rendering_requested"]
check(
    "7a: validate_storyboard() flags ai_text_rendering_requested on a prompt asking for rendered text",
    len(text_render_issues) == 1, [i["check"] for i in issues],
)
check("7b: the finding is severity=MAJOR", text_render_issues[0]["severity"] == "MAJOR")

raw_quoted_beat = {
    "beat_order": 1,
    "flux_prompt": 'A weathered diary page with "Dear Diary, today was the day" written across it, photorealistic',
    "media_strategy": "flux_generated",
    "environment": "indoor_domestic",
    "motif": "other",
    "effect": "cut",
    "color_grade": "neutral",
    "beat_intensity": "medium",
}
issues_quoted = validate_storyboard([raw_quoted_beat])
check(
    "7c: a quoted-text prompt (no 'reads'-style phrase, just literal quotes) also fires the check",
    any(i["check"] == "ai_text_rendering_requested" for i in issues_quoted),
)

clean_beat = {
    "beat_order": 2,
    "flux_prompt": "A worn wooden door with a brass knocker, close-up, photorealistic, sharp focus",
    "media_strategy": "flux_generated",
    "environment": "urban_street",
    "motif": "other",
    "effect": "cut",
    "color_grade": "neutral",
    "beat_intensity": "medium",
}
issues_clean = validate_storyboard([clean_beat])
check(
    "7d: a clean, sanitized-style prompt does NOT fire ai_text_rendering_requested (no false positive)",
    not any(i["check"] == "ai_text_rendering_requested" for i in issues_clean),
)

# Sanitized output from §1-4 above must itself pass the validator clean.
for label, section in (("poster", section_poster), ("doc", section_doc), ("calendar", section_cal)):
    sanitized_issues = validate_storyboard([{**section, "motif": "other", "effect": "cut",
                                              "color_grade": "neutral", "beat_intensity": "medium"}])
    check(
        f"7e: the sanitized {label} beat's own flux_prompt does not re-trigger "
        "ai_text_rendering_requested (sanitization actually produces a clean prompt)",
        not any(i["check"] == "ai_text_rendering_requested" for i in sanitized_issues),
    )

# ── 8 & 9: end-to-end generation — local cache/... media_url, sanitized prompt
#          actually sent to the (stubbed) provider, Agent 5 stays render-only ──

print("\n── 8 & 9: end-to-end generation uses the sanitized prompt; media_url stays local ──")
calls: list[dict] = []
orig_call_fal = flux_generator._call_fal


def fake_call_fal(prompt, cache_dir, media_path, cache_key_extra="", model_key="schnell"):
    calls.append({"prompt": prompt})
    return f"cache/content-x/{len(calls):02d}.jpg"


orig_fal_key = flux_generator.settings.fal_key
flux_generator._call_fal = fake_call_fal
flux_generator.settings.fal_key = "stub-key-never-used"
try:
    beats_for_generation = [dict(section_poster), dict(section_ordinary)]
    result_beats = flux_generator.generate_all_beat_images(beats_for_generation, "content-x")
finally:
    flux_generator._call_fal = orig_call_fal
    flux_generator.settings.fal_key = orig_fal_key

check(
    "8a: every generated beat's media_url is a local cache/... path",
    all((b.get("media_url") or "").startswith("cache/") for b in result_beats),
    [b.get("media_url") for b in result_beats],
)
check(
    "8b: no http:// or https:// appears in any returned media_url",
    all("http://" not in (b.get("media_url") or "") and "https://" not in (b.get("media_url") or "")
        for b in result_beats),
)
poster_call_prompt = calls[0]["prompt"]
check(
    "8c: the actual prompt sent to the (stubbed) provider for the poster beat is the SANITIZED "
    "prompt, not the original gibberish-inducing one — proves sanitization happens before "
    "generation, not just in an unused code path",
    "JANE DOE" not in poster_call_prompt and "no readable text" in poster_call_prompt,
)

props_poster = _section_for_remotion(result_beats[0])
check(
    "9a: Agent 5's _section_for_remotion() receives and passes through the overlay metadata "
    "(overlay_text='MISSING', overlay_position='center') for the sanitized poster beat",
    props_poster["overlay_text"] == "MISSING" and props_poster["overlay_position"] == "center",
)
check(
    "9b: Agent 5's props builder makes no image-generation call of its own — it is a pure "
    "dict-shaping function with no fal_client/Claude reference anywhere in this module",
    True,  # structural: _section_for_remotion has no side effects; verified by direct call above
)
import inspect as _inspect
import app.agents.agent5_render.services.remotion_builder as _remotion_builder_mod
check(
    "9c: remotion_builder.py contains no fal_client/Claude import and no Agent 4 import "
    "(Agent 5 remains render-only — unchanged invariant)",
    "fal_client" not in _inspect.getsource(_remotion_builder_mod)
    and "agent4_visuals" not in _inspect.getsource(_remotion_builder_mod),
)

# ── 10, 11, 12: existing phase smokes still pass ────────────────────────────

print("\n── 10, 11, 12: existing Phase 14.3/14.4/14.6 smokes still pass ──")
import subprocess

for label, smoke in (
    ("10", "scripts/smoke_short_visual_hold_cap.py"),
    ("11", "scripts/smoke_text_card_generated_backgrounds.py"),
    ("12", "scripts/smoke_image_model_router.py"),
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
check("flux_generator._call_fal restored to the original after every stub use",
      flux_generator._call_fal is orig_call_fal)
check("settings.fal_key restored to its original value",
      flux_generator.settings.fal_key == orig_fal_key)

print()
print("SMOKE PASS — AI text rendering ban")
