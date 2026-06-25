"""Phase 8A-1 — mechanical runtime proof that the storyboard pipeline works
correctly now that why_this_visual / story_progression_role are permanently
removed from production (STORYBOARD_SCHEMA_VERSION 6.1, Phase 6D-1B).

Zero live API calls — stubs only `call_claude_structured_with_usage`
(the Claude call inside generate_storyboard_batch()). Runs the REAL,
unmodified chain end to end:

    generate_storyboard_batch()  [Claude call stubbed]
      -> split_into_beats()      [real — merges batches, calls map_*]
      -> _build_beat_section()   [real, called inside map_*]
      -> map_storyboard_beats_to_timestamps()  [real]
      -> validate_storyboard()   [real]

The stubbed response intentionally matches the CURRENT (20-field, v6.1)
production schema shape — no why_this_visual, no story_progression_role —
to prove the full chain tolerates their absence with no missing-key errors
and no exceptions, and that the final beat-section shape persistence
expects is unaffected.

Run: python scripts/smoke_phase8a1_field_removal_runtime_proof.py
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


import app.agents.agent4_visuals.system_prompt as system_prompt
from app.agents.agent4_visuals.subagents.storyboard import split_into_beats
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard

# ── 1. Confirm the schema itself is the current, permanently-trimmed v6.1 shape ──

CURRENT_FIELDS = set(system_prompt._BEAT_SCHEMA["properties"].keys())
assert_ok(
    "_BEAT_SCHEMA has 20 fields (v6.1, dead fields already removed)",
    len(CURRENT_FIELDS) == 20,
    f"actual count={len(CURRENT_FIELDS)}",
)
assert_ok(
    "why_this_visual absent from current schema",
    "why_this_visual" not in CURRENT_FIELDS,
)
assert_ok(
    "story_progression_role absent from current schema",
    "story_progression_role" not in CURRENT_FIELDS,
)

# ── 2. Build a stubbed Claude response matching exactly the current schema shape ──

class _FakeChannel:
    niche = "history"
    tone = "documentary"

WORDS = [f"tok{idx:04d}" for idx in range(120)]
NARRATION = "[INTRO]\n" + " ".join(WORDS)
WORD_MS = 300
WHISPER_TRANSCRIPT = [
    {"word": w, "start": (i * WORD_MS) / 1000.0, "end": ((i + 1) * WORD_MS) / 1000.0}
    for i, w in enumerate(WORDS)
]
DURATION_MS = len(WORDS) * WORD_MS

N_BEATS = 12
CHUNK = len(WORDS) // N_BEATS


def _stub_beat(i: int) -> dict:
    start = i * CHUNK
    end = start + CHUNK if i < N_BEATS - 1 else len(WORDS)
    chunk_words = WORDS[start:end]
    beat = {
        "beat_order": i,
        "start_hint": " ".join(chunk_words[:8]),
        "end_hint": " ".join(chunk_words[-8:]),
        "visual_intent": f"Concrete subject for beat {i}.",
        "visual_type": "b-roll",
        "visual_category": "object",
        "environment": "indoor_office",
        "flux_prompt": f"A specific physical object on a desk, close-up, beat {i}, photorealistic, sharp focus",
        "effect": "slow_zoom",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "overlay_text": "",
        "overlay_position": "none",
        "motif": "object",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "stock_queries": [],
        "fallback_flux_prompt": "",
        "text_card_style": "default",
    }
    # Exact key-set parity check against the live schema, not a hardcoded list —
    # catches drift if _BEAT_SCHEMA changes shape in the future without this
    # stub being updated to match.
    assert set(beat.keys()) == CURRENT_FIELDS, (
        f"stub beat keys {set(beat.keys())} != live schema fields {CURRENT_FIELDS}"
    )
    return beat


stub_beats = [_stub_beat(i) for i in range(N_BEATS)]
stub_storyboard = {
    "storyboard_status": "APPROVED",
    "overall_style": "documentary, neutral",
    "beats": stub_beats,
    "global_notes": [],
}


def _stub_call(**kwargs):
    return dict(stub_storyboard), {"output_tokens": 2000, "input_tokens": 300}


orig_call = system_prompt.call_claude_structured_with_usage
system_prompt.call_claude_structured_with_usage = _stub_call
try:
    mapped = split_into_beats(
        voice_script=NARRATION,
        duration_ms=DURATION_MS,
        channel=_FakeChannel(),
        script_format="youtube_long",
        whisper_transcript=WHISPER_TRANSCRIPT,
        allow_legacy_fallback=True,
        language="en",
    )
except Exception as exc:
    print(f"FAIL [full chain raised an exception]: {exc!r}")
    sys.exit(1)
finally:
    system_prompt.call_claude_structured_with_usage = orig_call

assert_ok("split_into_beats() completed with no exception", True)
assert_ok(
    "no missing-key errors — beats were returned",
    mapped is not None and len(mapped) == N_BEATS,
    f"got {len(mapped) if mapped else 0} beats, expected {N_BEATS}",
)

# ── 3. Output structure matches previous behavior except the two fields are absent ──

EXPECTED_SECTION_KEYS = {
    "beat_order", "section_order", "audio_start_ms", "audio_end_ms", "duration_sec",
    "script_text", "visual_intent", "visual_type", "visual_category", "environment",
    "flux_prompt", "effect", "color_grade", "transition_to_next", "overlay_text",
    "overlay_position", "motif", "beat_intensity", "suggested_duration_sec",
    "media_strategy", "text_card_style", "media_url", "media_type",
}

for section in mapped:
    assert_ok(
        f"beat_order={section['beat_order']}: persisted dict has exactly the expected keys",
        set(section.keys()) == EXPECTED_SECTION_KEYS,
        f"diff: {set(section.keys()) ^ EXPECTED_SECTION_KEYS}",
    )
    assert_ok(
        f"beat_order={section['beat_order']}: why_this_visual absent from persisted output",
        "why_this_visual" not in section,
    )
    assert_ok(
        f"beat_order={section['beat_order']}: story_progression_role absent from persisted output",
        "story_progression_role" not in section,
    )
    assert_ok(
        f"beat_order={section['beat_order']}: has valid audio_start_ms/audio_end_ms (persistence-ready)",
        section["audio_end_ms"] > section["audio_start_ms"] >= 0,
    )

# ── 4. validate_storyboard() runs cleanly on the real output (no exception) ──

try:
    issues = validate_storyboard(mapped)
except Exception as exc:
    print(f"FAIL [validate_storyboard() raised]: {exc!r}")
    sys.exit(1)

assert_ok(
    "validate_storyboard() completed with no exception",
    True,
    f"{len(issues)} findings (informational, not a pass/fail signal here)",
)
assert_ok(
    "no validator finding references either removed field",
    all("why_this_visual" not in str(i) and "story_progression_role" not in str(i) for i in issues),
)

print()
print("SMOKE PASS")
