"""Storyboard intensity smoke test — zero API calls, zero DB access.

Verifies:
  1. _build_beat_section, _cleanup_micro_beats, INTENSITY_FLOOR_MS importable.
  2. _BEAT_SCHEMA contains beat_intensity and suggested_duration_sec.
  3. STORYBOARD_SCHEMA_VERSION is "6.1".
  4. _cleanup_micro_beats raises a below-floor high-intensity beat to its 1000ms floor.
  5. _cleanup_micro_beats raises a below-floor low-intensity beat to its 3000ms floor.
  6. _cleanup_micro_beats leaves a beat already at/above floor untouched.
  7. _build_beat_section produces beat_intensity and suggested_duration_sec fields.
  8. why_this_visual / story_progression_role removed from _BEAT_SCHEMA and
     _STORYBOARD_SYSTEM_PROMPT (Phase 6D-1B).

Run: python scripts/smoke_storyboard_intensity.py
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

from app.agents.agent4_visuals.subagents.storyboard import (
    _build_beat_section,
    _cleanup_micro_beats,
    INTENSITY_FLOOR_MS,
)
from app.agents.agent4_visuals.system_prompt import (
    _BEAT_SCHEMA,
    STORYBOARD_SCHEMA_VERSION,
)

assert_ok("imports", True)

# ── 2. _BEAT_SCHEMA contains both new fields ──────────────────────────────────

props = _BEAT_SCHEMA.get("properties", {})
required = _BEAT_SCHEMA.get("required", [])

assert_ok(
    "_BEAT_SCHEMA has beat_intensity property",
    "beat_intensity" in props,
    f"properties keys: {list(props.keys())}",
)
assert_ok(
    "_BEAT_SCHEMA has suggested_duration_sec property",
    "suggested_duration_sec" in props,
    f"properties keys: {list(props.keys())}",
)
assert_ok(
    "beat_intensity in required",
    "beat_intensity" in required,
    f"required: {required}",
)
assert_ok(
    "suggested_duration_sec in required",
    "suggested_duration_sec" in required,
    f"required: {required}",
)

# ── 3. STORYBOARD_SCHEMA_VERSION == "6.1" ────────────────────────────────────

assert_ok(
    "STORYBOARD_SCHEMA_VERSION=6.1",
    STORYBOARD_SCHEMA_VERSION == "6.1",
    f"got {STORYBOARD_SCHEMA_VERSION!r}",
)

# ── 8. Phase 6D-1B: why_this_visual / story_progression_role removed ─────────

assert_ok(
    "_BEAT_SCHEMA no longer has why_this_visual property",
    "why_this_visual" not in props,
)
assert_ok(
    "_BEAT_SCHEMA no longer has story_progression_role property",
    "story_progression_role" not in props,
)
assert_ok(
    "why_this_visual not in required",
    "why_this_visual" not in required,
)
assert_ok(
    "story_progression_role not in required",
    "story_progression_role" not in required,
)
from app.agents.agent4_visuals.system_prompt import _STORYBOARD_SYSTEM_PROMPT
assert_ok(
    "_STORYBOARD_SYSTEM_PROMPT no longer mentions why_this_visual",
    "why_this_visual" not in _STORYBOARD_SYSTEM_PROMPT,
)
assert_ok(
    "_STORYBOARD_SYSTEM_PROMPT no longer mentions story_progression_role",
    "story_progression_role" not in _STORYBOARD_SYSTEM_PROMPT,
)

# ── 4. _cleanup_micro_beats: high beat below 1000ms floor is raised ───────────

beats_high = [{"beat_intensity": "high"}]
# 200ms beat — below 1000ms floor
boundaries_high = [(0, 200)]
_cleanup_micro_beats(boundaries_high, 5000, beats_high)
start, end = boundaries_high[0]
assert_ok(
    "_cleanup_micro_beats raises high beat to 1000ms floor",
    end - start >= INTENSITY_FLOOR_MS["high"],
    f"got duration {end - start}ms, expected >= {INTENSITY_FLOOR_MS['high']}ms",
)

# ── 5. _cleanup_micro_beats: low beat below 3000ms floor is raised ────────────

beats_low = [{"beat_intensity": "low"}]
boundaries_low = [(0, 500)]
_cleanup_micro_beats(boundaries_low, 10000, beats_low)
start, end = boundaries_low[0]
assert_ok(
    "_cleanup_micro_beats raises low beat to 3000ms floor",
    end - start >= INTENSITY_FLOOR_MS["low"],
    f"got duration {end - start}ms, expected >= {INTENSITY_FLOOR_MS['low']}ms",
)

# ── 6. First beat already at floor is not unnecessarily extended ──────────────
# Use two beats so beat[0] is not the last beat (last beat is always clamped to duration_ms).

beats_ok = [{"beat_intensity": "medium"}, {"beat_intensity": "medium"}]
boundaries_ok = [(0, 3000), (3000, 5000)]  # both well above 2000ms medium floor
_cleanup_micro_beats(boundaries_ok, 5000, beats_ok)
start, end = boundaries_ok[0]
assert_ok(
    "_cleanup_micro_beats leaves above-floor non-last beat untouched",
    end - start == 3000,
    f"got {end - start}ms, expected 3000ms",
)

# ── 7. _build_beat_section includes beat_intensity + suggested_duration_sec ───

fixture_beat = {
    "beat_order":           0,
    "visual_intent":        "A worn wooden door in an empty hallway.",
    "visual_type":          "b-roll",
    "visual_category":      "place",
    "environment":          "corridor_interior",
    "flux_prompt":          "Worn wooden door, empty hallway, diffuse light.",
    "effect":               "slow_zoom",
    "color_grade":          "desaturated",
    "transition_to_next":   "cut",
    "overlay_text":         "",
    "overlay_position":     "none",
    "motif":                "doorway",
    "beat_intensity":       "high",
    "suggested_duration_sec": 1.5,
}

section = _build_beat_section(fixture_beat, 0, 0, 1800, "Worn wooden door in hallway.")

assert_ok(
    "_build_beat_section includes beat_intensity",
    section.get("beat_intensity") == "high",
    f"got {section.get('beat_intensity')!r}",
)
assert_ok(
    "_build_beat_section includes suggested_duration_sec",
    section.get("suggested_duration_sec") == 1.5,
    f"got {section.get('suggested_duration_sec')!r}",
)

# ── Done ──────────────────────────────────────────────────────────────────────

print()
print("SMOKE PASS")
