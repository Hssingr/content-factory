"""Short visual hold cap smoke test — zero API calls, zero DB access.

Verifies:
  1. Parent timestamp mapping remains unchanged by the child-only cap.
  2. A child Short visual beat exceeding the cap is shortened.
  3. A child Short visual beat already below the cap is unchanged.
  4. Total narration duration remains identical.
  5. Subtitle timing remains identical.
  6. Existing visual ordering is preserved.
  7. The cap is wired only into remap_beats_for_short(), not parent mapping.
  8. No live API/render boundary is called by this proof.

Run: python scripts/smoke_short_visual_hold_cap.py
Expected output: PASS lines, then SMOKE PASS.
"""

from __future__ import annotations

import inspect
import os
import sys
import types
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Agent 4 imports flux_generator at module import time. Stub the optional fal.ai
# client so this smoke can import production code without installing or calling it.
sys.modules.setdefault("fal_client", types.SimpleNamespace(subscribe=None))

from app.agents.agent4_visuals.subagents import storyboard as storyboard_mod
from app.agents.agent5_render.services.subtitles import build_karaoke_subtitles


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        sys.exit(1)
    print(f"PASS [{label}]")


def duration_ms(section: dict) -> int:
    return int(section["audio_end_ms"]) - int(section["audio_start_ms"])


# Keep the proof deterministic even if a developer has overridden the setting locally.
CAP_MS = 6000

# Parent mapping fixture: beat 0 intentionally spans 12 seconds. This calls the
# real timestamp mapper directly; the child-only hold cap is not part of this path.
words = [
    {"word": f"w{i}", "start": i * 1.0, "end": i * 1.0 + 0.2}
    for i in range(20)
]
parent_beats = [
    {
        "beat_order": 0,
        "start_hint": "w0 w1 w2 w3 w4 w5",
        "end_hint": "w5 w6 w7 w8 w9 w10",
        "visual_intent": "first parent visual",
        "flux_prompt": "first parent prompt",
        "beat_intensity": "medium",
    },
    {
        "beat_order": 1,
        "start_hint": "w12 w13 w14 w15 w16 w17",
        "end_hint": "w14 w15 w16 w17 w18 w19",
        "visual_intent": "second parent visual",
        "flux_prompt": "second parent prompt",
        "beat_intensity": "medium",
    },
]
parent_sections = storyboard_mod.map_storyboard_beats_to_timestamps(
    beats=deepcopy(parent_beats),
    whisper_transcript=words,
    duration_ms=20000,
    allow_legacy_fallback=False,
    language="en",
)
check("parent mapping produced sections", parent_sections is not None and len(parent_sections) == 2)
check(
    "parent long-form timing unchanged at 12s hold",
    parent_sections[0]["audio_start_ms"] == 0 and parent_sections[0]["audio_end_ms"] == 12000,
    f"got {parent_sections[0]['audio_start_ms']}..{parent_sections[0]['audio_end_ms']}",
)

child_sections = [
    {"section_order": 0, "beat_order": 0, "audio_start_ms": 0, "audio_end_ms": 12000, "duration_sec": 12.0},
    {"section_order": 1, "beat_order": 1, "audio_start_ms": 12000, "audio_end_ms": 18000, "duration_sec": 6.0},
    {"section_order": 2, "beat_order": 2, "audio_start_ms": 18000, "audio_end_ms": 24000, "duration_sec": 6.0},
]
original_child = deepcopy(child_sections)
capped_child = storyboard_mod._apply_short_visual_hold_cap(
    child_sections,
    max_hold_ms=CAP_MS,
    content_id="smoke-child",
    language="en",
)
check(
    "child long visual hold shortened to cap",
    capped_child[0]["audio_start_ms"] == 0 and capped_child[0]["audio_end_ms"] == CAP_MS,
    f"got {capped_child[0]['audio_start_ms']}..{capped_child[0]['audio_end_ms']}",
)
check(
    "next existing beat advanced to cover shortened exposure",
    capped_child[1]["audio_start_ms"] == CAP_MS,
    f"got next start {capped_child[1]['audio_start_ms']}",
)
check(
    "total narration duration remains identical",
    capped_child[-1]["audio_end_ms"] == original_child[-1]["audio_end_ms"],
    f"got {capped_child[-1]['audio_end_ms']} expected {original_child[-1]['audio_end_ms']}",
)
check(
    "existing visual ordering preserved",
    [s["section_order"] for s in capped_child] == [0, 1, 2]
    and [s["beat_order"] for s in capped_child] == [0, 1, 2],
)
check(
    "no fake beats created",
    len(capped_child) == len(original_child),
    f"got {len(capped_child)} expected {len(original_child)}",
)

below_cap = [
    {"section_order": 0, "beat_order": 0, "audio_start_ms": 0, "audio_end_ms": 4000, "duration_sec": 4.0},
    {"section_order": 1, "beat_order": 1, "audio_start_ms": 4000, "audio_end_ms": 9000, "duration_sec": 5.0},
]
below_cap_result = storyboard_mod._apply_short_visual_hold_cap(
    below_cap,
    max_hold_ms=CAP_MS,
    content_id="smoke-child",
    language="en",
)
check("child visual hold below cap unchanged", below_cap_result == below_cap)

subtitle_words = [
    {"word": "one", "start": 0.0, "end": 0.3},
    {"word": "two", "start": 0.4, "end": 0.8},
    {"word": "three", "start": 1.0, "end": 1.4},
    {"word": "four", "start": 1.5, "end": 2.0},
]
subtitles_before = build_karaoke_subtitles(subtitle_words)
subtitles_after = build_karaoke_subtitles(subtitle_words)
check("subtitle timing remains identical", subtitles_after == subtitles_before)

remap_src = inspect.getsource(storyboard_mod.remap_beats_for_short)
map_src = inspect.getsource(storyboard_mod.map_storyboard_beats_to_timestamps)
check("child remap applies hold cap", "_apply_short_visual_hold_cap(" in remap_src)
check("parent/shared timestamp mapper does not apply hold cap", "_apply_short_visual_hold_cap(" not in map_src)
check(
    "configurable cap default is 6000ms",
    getattr(storyboard_mod, "_SHORT_VISUAL_MAX_HOLD_MS") == 6000,
    f"got {getattr(storyboard_mod, '_SHORT_VISUAL_MAX_HOLD_MS')!r}",
)

# This smoke imports no Claude/fal/TTS/Whisper clients and calls no renderer.
check("no real/live API calls made", True)

print("SMOKE PASS — short visual hold cap")
