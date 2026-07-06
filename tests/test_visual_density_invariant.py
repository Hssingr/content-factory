"""Runtime proof for roadmap Phase 3 item 3c visual density invariants.

No external APIs are called. These tests drive the real timestamp mapper,
micro-beat cleanup, and hold-cap logic with synthetic transcript fixtures.
"""

from __future__ import annotations

import logging
import unittest

from app.agents.agent4_visuals.subagents.storyboard import (
    _PARENT_VISUAL_MAX_HOLD_MS,
    _apply_visual_hold_cap,
    _cleanup_micro_beats,
    map_storyboard_beats_to_timestamps,
)


def _word(i: int) -> str:
    return f"word{i}"


def _transcript(total: int) -> list[dict]:
    return [
        {"word": _word(i), "start": i * 0.5, "end": (i + 1) * 0.5}
        for i in range(total)
    ]


def _beat(order: int, hint_index: int, *, intensity: str = "medium") -> dict:
    phrase = " ".join(_word(i) for i in range(hint_index, hint_index + 6))
    return {
        "beat_order": order,
        "start_hint": phrase,
        "end_hint": phrase,
        "visual_intent": f"intent {order}",
        "flux_prompt": f"concrete subject {order}, documentary photograph",
        "visual_type": "b-roll",
        "visual_category": "object",
        "environment": "indoor_office",
        "effect": "cut",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "motif": "object",
        "beat_intensity": intensity,
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
    }


class VisualDensityInvariantTest(unittest.TestCase):
    def test_mapper_fails_when_beats_times_max_hold_cannot_cover_duration(self) -> None:
        beats = [_beat(0, 0), _beat(1, 30)]
        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level=logging.ERROR
        ) as logs:
            result = map_storyboard_beats_to_timestamps(
                beats,
                _transcript(80),
                duration_ms=30_000,
                allow_legacy_fallback=False,
                language="en",
                max_hold_ms=_PARENT_VISUAL_MAX_HOLD_MS,
            )

        self.assertIsNone(result)
        self.assertTrue(any("VISUAL_DENSITY_INVARIANT_FAILED" in m for m in logs.output), logs.output)

    def test_micro_merge_discard_over_30_percent_fails_loud(self) -> None:
        boundaries = [(i * 100, (i + 1) * 100) for i in range(10)]
        beats = [{"beat_intensity": "low"} for _ in boundaries]
        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level=logging.WARNING
        ) as logs:
            stats = _cleanup_micro_beats(boundaries, duration_ms=5_000, beats=beats)

        self.assertGreater(stats.discard_ratio, 0.30)
        self.assertEqual(stats.before, 10)
        self.assertLess(stats.after, stats.before)
        self.assertTrue(any("MICRO_MERGE_DISCARDED" in m for m in logs.output), logs.output)

    def test_hold_cap_after_micro_merge_restores_nonterminal_ceiling(self) -> None:
        sections = [
            {"beat_order": 0, "audio_start_ms": 0, "audio_end_ms": 20_000},
            {"beat_order": 1, "audio_start_ms": 20_000, "audio_end_ms": 30_000},
            {"beat_order": 2, "audio_start_ms": 30_000, "audio_end_ms": 36_000},
        ]
        capped = _apply_visual_hold_cap(
            sections,
            max_hold_ms=_PARENT_VISUAL_MAX_HOLD_MS,
            log_prefix="PARENT_VISUAL_HOLD_CAP",
        )

        holds = [s["audio_end_ms"] - s["audio_start_ms"] for s in capped[:-1]]
        self.assertTrue(all(hold <= _PARENT_VISUAL_MAX_HOLD_MS for hold in holds))
        self.assertEqual(capped[1]["audio_start_ms"], _PARENT_VISUAL_MAX_HOLD_MS)


if __name__ == "__main__":
    unittest.main()
