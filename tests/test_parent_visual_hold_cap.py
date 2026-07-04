"""Runtime proof for the parent visual hold cap (last-line frozen-frame guard).

Even if a bad anchor slips past BOTH the hint proximity window
(tests/test_hint_search_proximity_window.py) AND the anchor span sanity check
(tests/test_boundary_span_sanity.py), no non-terminal parent beat may hold a
single image beyond ``settings.parent_visual_max_hold_ms`` (9 s default).
The cap is applied in ``split_into_beats()`` by advancing the next existing
beat — never by creating beats or touching audio/subtitle timing — via the
same shared core the child Short path already uses at its tighter 6 s ceiling.

The runtime test drives the REAL ``split_into_beats`` chain (segment split →
hint hardening → merge → timestamp mapping with the new guards →
``_apply_visual_hold_cap``) on a real synthetic transcript. Only
``generate_storyboard_batch`` — the paid Claude call — is stubbed, per the
"stub only paid APIs, never internal logic" rule.
"""

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.agents.agent4_visuals.subagents.storyboard import (
    _PARENT_VISUAL_MAX_HOLD_MS,
    _apply_short_visual_hold_cap,
    _apply_visual_hold_cap,
    split_into_beats,
)

_WORD_MS = 400


def _fill(i: int) -> str:
    letters = ""
    n = i
    while True:
        letters = chr(ord("a") + n % 26) + letters
        n //= 26
        if n == 0:
            break
    return "pad" + letters


def _transcript(words: list[str]) -> list[dict]:
    return [
        {"word": w, "start": i * _WORD_MS / 1000.0, "end": (i + 1) * _WORD_MS / 1000.0}
        for i, w in enumerate(words)
    ]


def _words_with(placements: dict[int, str], total: int) -> list[str]:
    words = [_fill(i) for i in range(total)]
    for start, phrase in placements.items():
        for j, w in enumerate(phrase.split()):
            words[start + j] = w
    return words


def _section(order: int, start_ms: int, end_ms: int) -> dict:
    return {
        "section_order": order,
        "beat_order": order,
        "audio_start_ms": start_ms,
        "audio_end_ms": end_ms,
        "duration_sec": (end_ms - start_ms) / 1000,
    }


class TestSharedHoldCapCore(unittest.TestCase):
    """Unit checks on _apply_visual_hold_cap and the child wrapper contract."""

    def test_parent_prefix_caps_and_logs(self):
        sections = [_section(0, 0, 15_000), _section(1, 15_000, 20_000)]
        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level=logging.INFO
        ) as logs:
            capped = _apply_visual_hold_cap(
                sections, max_hold_ms=9_000, log_prefix="PARENT_VISUAL_HOLD_CAP"
            )
        self.assertEqual(capped[0]["audio_end_ms"], 9_000)
        self.assertEqual(capped[1]["audio_start_ms"], 9_000)
        self.assertEqual(capped[0]["duration_sec"], 9.0)
        self.assertTrue(any("PARENT_VISUAL_HOLD_CAP_APPLIED" in m for m in logs.output))
        self.assertFalse(any("SHORT_VISUAL_HOLD_CAP" in m for m in logs.output))

    def test_terminal_beat_is_exempt(self):
        sections = [_section(0, 0, 5_000), _section(1, 5_000, 30_000)]
        capped = _apply_visual_hold_cap(
            sections, max_hold_ms=9_000, log_prefix="PARENT_VISUAL_HOLD_CAP"
        )
        # No later beat exists to absorb the remainder — terminal stays intact.
        self.assertEqual(capped[1]["audio_end_ms"], 30_000)

    def test_below_cap_is_untouched(self):
        sections = [_section(0, 0, 4_000), _section(1, 4_000, 8_000)]
        capped = _apply_visual_hold_cap(
            sections, max_hold_ms=9_000, log_prefix="PARENT_VISUAL_HOLD_CAP"
        )
        self.assertEqual(
            [(s["audio_start_ms"], s["audio_end_ms"]) for s in capped],
            [(0, 4_000), (4_000, 8_000)],
        )

    def test_child_wrapper_keeps_short_log_and_default_cap(self):
        sections = [_section(0, 0, 7_000), _section(1, 7_000, 10_000)]
        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level=logging.INFO
        ) as logs:
            capped = _apply_short_visual_hold_cap(sections)
        # Default Short cap (6 s from settings) applied under the SHORT log name.
        self.assertEqual(capped[0]["audio_end_ms"], settings.short_visual_max_hold_ms)
        self.assertTrue(any("SHORT_VISUAL_HOLD_CAP_APPLIED" in m for m in logs.output))

    def test_constant_reads_settings(self):
        self.assertEqual(_PARENT_VISUAL_MAX_HOLD_MS, settings.parent_visual_max_hold_ms)
        self.assertEqual(_PARENT_VISUAL_MAX_HOLD_MS, 9_000)


class TestParentPathRuntime(unittest.TestCase):
    """split_into_beats applies the parent cap after real timestamp mapping."""

    def _storyboard_stub(self):
        def beat(order: int, start_hint: str) -> dict:
            return {
                "beat_order": order,
                "start_hint": start_hint,
                "end_hint": start_hint,
                "visual_intent": f"intent {order}",
                "flux_prompt": f"concrete subject {order}, wide shot, photorealistic",
                "visual_type": "b-roll",
                "visual_category": "place",
                "environment": "forest_nature",
                "effect": "slow_zoom",
                "color_grade": "neutral",
                "transition_to_next": "cut",
                "overlay_text": "",
                "overlay_position": "none",
                "motif": "exterior",
                "beat_intensity": "medium",
                "suggested_duration_sec": 3.0,
                "media_strategy": "flux_generated",
                "stock_queries": [],
                "fallback_flux_prompt": "",
                "text_card_style": "default",
            }

        storyboard = {
            "storyboard_status": "APPROVED",
            "overall_style": "documentary",
            "beats": [
                beat(0, "alpha bravo charlie delta echo foxtrot"),
                beat(1, "nomatchone nomatchtwo nomatchthree nomatchfour nomatchfive nomatchsix"),
                beat(2, "gapone gaptwo gapthree gapfour gapfive gapsix"),
                beat(3, "golf hotel india juliet kilo lima"),
            ],
            "global_notes": [],
        }
        usage = {"output_tokens": 500, "input_tokens": 100}
        diag = {"was_truncated": False, "attempt_count": 1, "input_tokens": 100, "elapsed_ms": 5}
        return storyboard, usage, diag

    def test_long_interpolated_holds_are_capped(self):
        # Trusted anchors at 0 s (beat 0) and 30 s (beat 3); beats 1–2 unmatched.
        # Span sanity keeps the 30 s anchor (3 covering beats → threshold 36 s);
        # interpolation yields 10 s holds — all above the 9 s parent cap.
        words = _words_with(
            {0: "alpha bravo charlie delta echo foxtrot",
             75: "golf hotel india juliet kilo lima"},
            total=90,
        )
        channel = SimpleNamespace(niche="mystery", tone="tense")
        voice_script = "[INTRO]\n" + " ".join(words)

        with patch(
            "app.agents.agent4_visuals.subagents.storyboard.generate_storyboard_batch",
            return_value=self._storyboard_stub(),
        ):
            with self.assertLogs(
                "app.agents.agent4_visuals.subagents.storyboard", level=logging.INFO
            ) as logs:
                sections = split_into_beats(
                    voice_script=voice_script,
                    duration_ms=90 * _WORD_MS,           # 36 s
                    channel=channel,
                    script_format="youtube_long",
                    whisper_transcript=_transcript(words),
                    allow_legacy_fallback=False,
                    language="en",
                )

        self.assertIsNotNone(sections)
        starts = [s["audio_start_ms"] for s in sections]
        holds = [s["audio_end_ms"] - s["audio_start_ms"] for s in sections]

        # Interpolated 10 s holds (0/10/20/30 s) are capped to 9 s each,
        # advancing every subsequent beat: 0/9/18/27 s, terminal exactly 9 s.
        self.assertEqual(starts, [0, 9_000, 18_000, 27_000])
        self.assertEqual(holds, [9_000, 9_000, 9_000, 9_000])
        self.assertEqual(sections[-1]["audio_end_ms"], 36_000)

        self.assertTrue(
            any("PARENT_VISUAL_HOLD_CAP_APPLIED" in m for m in logs.output), logs.output
        )


if __name__ == "__main__":
    unittest.main()
