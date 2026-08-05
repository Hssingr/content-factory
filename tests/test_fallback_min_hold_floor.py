"""Runtime proof: proportional-fallback clusters never emit sub-floor beats.

Content 069d8d06 diagnostic (2026-08-05): a shortfall-topup call's beats
duplicated already-covered narration (see
tests/test_storyboard_topup_duplicate_rejection.py for that root-cause fix),
leaving 9 consecutive unmatched beats packed into an ~11 s gap between two
real anchors — proportional interpolation gave each one only a fraction of a
second, producing the reviewer-observed run of 1.0/2.0/3.0 s flashes, each
showing an image unrelated to the instant of narration it played against.

`_drop_surplus_fallback_beats` is this codebase's second, independent line of
defense (on top of the topup-duplicate fix): whatever the cause of a fallback
cluster, if the real time span it must share cannot give every beat in it at
least `settings.min_fallback_beat_hold_ms` (default 1 600 ms), the cluster is
deterministically thinned to as many evenly-spaced beats as the span can
support, rather than shipping every beat mismatched and sub-floor.
"""

import unittest

from app.agents.agent4_visuals.subagents.storyboard import (
    _MIN_FALLBACK_HOLD_MS,
    _drop_surplus_fallback_beats,
    _flatten_transcript,
    map_storyboard_beats_to_timestamps,
)
from app.config import settings


def _transcript(words: list[str], word_ms: int = 400) -> list[dict]:
    return [
        {"word": w, "start": i * word_ms / 1000.0, "end": (i + 1) * word_ms / 1000.0}
        for i, w in enumerate(words)
    ]


def _fill(i: int) -> str:
    letters = ""
    n = i
    while True:
        letters = chr(ord("a") + n % 26) + letters
        n //= 26
        if n == 0:
            break
    return "pad" + letters


def _beat(order: int, suggested: float = 3.0, intensity: str = "medium") -> dict:
    return {
        "beat_order": order,
        "start_hint": f"unmatched hint text {order}",
        "end_hint": f"unmatched hint text {order}",
        "suggested_duration_sec": suggested,
        "beat_intensity": intensity,
        "flux_prompt": f"prompt {order}",
    }


class TestSettingsWiring(unittest.TestCase):
    def test_module_constant_reads_from_settings(self):
        self.assertEqual(_MIN_FALLBACK_HOLD_MS, settings.min_fallback_beat_hold_ms)
        self.assertEqual(settings.min_fallback_beat_hold_ms, 1_600)


class TestDropSurplusFallbackBeatsUnit(unittest.TestCase):
    """Direct unit checks — the function is called between anchor-demotion and
    boundary resolution inside map_storyboard_beats_to_timestamps.

    A run immediately preceded by a trusted anchor (every run except a
    leading run before the first anchor) shares its interpolated span with
    that anchor's own weighted slice too (``_resolve_boundaries`` includes
    the anchor beat in the same weight pool) — one extra "slot" is reserved
    for it so the kept fallback beats clear ``floor_ms`` themselves, not just
    the combined anchor+fallback average.
    """

    def test_thins_a_cluster_packed_below_the_floor(self):
        # Anchors at token 0 (0 ms) and token 12 (4 800 ms); 8 fallback beats
        # share that 4 800 ms gap -> 600 ms/beat average, well under 1 600 ms.
        # total_slots = 4800 // 1600 = 3; minus 1 for the leading anchor's
        # own slice -> keep_count = 2.
        flat = _flatten_transcript(_transcript([f"tok{i}" for i in range(20)]))
        beats = [_beat(0)] + [_beat(k) for k in range(1, 9)] + [_beat(9)]
        matches = [(0, 0)] + [None] * 8 + [(12, 12)]
        match_type = ["exact"] + ["fallback"] * 8 + ["exact"]

        kept_matches, kept_types, kept_beats = _drop_surplus_fallback_beats(
            matches, match_type, beats, flat, duration_ms=8_000, floor_ms=1_600,
        )

        self.assertEqual(len(kept_beats), 1 + 2 + 1)
        self.assertEqual(len(kept_matches), len(kept_beats))
        self.assertEqual(len(kept_types), len(kept_beats))
        # Trusted anchors on both sides always survive.
        self.assertEqual(kept_types[0], "exact")
        self.assertEqual(kept_types[-1], "exact")
        self.assertEqual(kept_matches[0], (0, 0))
        self.assertEqual(kept_matches[-1], (12, 12))
        # The kept fallback beats are still unmatched (never invented anchors).
        self.assertEqual(kept_types[1], "fallback")
        self.assertEqual(kept_types[2], "fallback")
        # beat_order is renumbered contiguously — no gaps left for downstream
        # section_order persistence.
        self.assertEqual([b["beat_order"] for b in kept_beats], list(range(4)))

    def test_run_already_above_floor_is_left_untouched(self):
        # 8 000 ms shared across 4 fallback beats + the leading anchor's own
        # slice (5 slots total) = 1 600 ms/slot exactly at the floor ->
        # nothing is dropped, same objects come back.
        flat = _flatten_transcript(_transcript([f"tok{i}" for i in range(30)]))
        beats = [_beat(0)] + [_beat(k) for k in range(1, 5)] + [_beat(5)]
        matches = [(0, 0), None, None, None, None, (20, 20)]
        match_type = ["exact"] + ["fallback"] * 4 + ["exact"]

        kept_matches, kept_types, kept_beats = _drop_surplus_fallback_beats(
            matches, match_type, beats, flat, duration_ms=8_000, floor_ms=1_600,
        )
        self.assertIs(kept_beats, beats)
        self.assertIs(kept_matches, matches)
        self.assertIs(kept_types, match_type)

    def test_leading_run_before_any_anchor_has_no_anchor_tax(self):
        # No trusted anchor precedes this run (the very first beats of the
        # storyboard), so all 3 200 ms belongs to the 2 fallback beats alone:
        # 1 600 ms each, exactly at the floor -> untouched.
        flat = _flatten_transcript(_transcript([f"tok{i}" for i in range(20)]))
        beats = [_beat(0), _beat(1), _beat(2)]
        matches = [None, None, (8, 8)]
        match_type = ["fallback", "fallback", "exact"]
        kept_matches, kept_types, kept_beats = _drop_surplus_fallback_beats(
            matches, match_type, beats, flat, duration_ms=8_000, floor_ms=1_600,
        )
        self.assertEqual(len(kept_beats), 3)

    def test_single_beat_run_is_never_dropped(self):
        # A lone fallback beat between two anchors has nothing to thin — it
        # is not "surplus", it is the only beat available for that gap.
        flat = _flatten_transcript(_transcript([f"tok{i}" for i in range(10)]))
        beats = [_beat(0), _beat(1), _beat(2)]
        matches = [(0, 0), None, (5, 5)]
        match_type = ["exact", "fallback", "exact"]
        kept_matches, kept_types, kept_beats = _drop_surplus_fallback_beats(
            matches, match_type, beats, flat, duration_ms=4_000, floor_ms=1_600,
        )
        self.assertEqual(len(kept_beats), 3)

    def test_zero_floor_disables_thinning(self):
        flat = _flatten_transcript(_transcript([f"tok{i}" for i in range(20)]))
        beats = [_beat(0)] + [_beat(k) for k in range(1, 9)] + [_beat(9)]
        matches = [(0, 0)] + [None] * 8 + [(12, 12)]
        match_type = ["exact"] + ["fallback"] * 8 + ["exact"]
        kept_matches, kept_types, kept_beats = _drop_surplus_fallback_beats(
            matches, match_type, beats, flat, duration_ms=8_000, floor_ms=0,
        )
        self.assertEqual(len(kept_beats), 10)


class TestEndToEndClusterThinning(unittest.TestCase):
    """Full map_storyboard_beats_to_timestamps chain — nothing internal
    stubbed, no external API on this path."""

    def test_dense_fallback_cluster_between_close_anchors_is_thinned(self):
        # 30 tokens @ 400 ms = 12 000 ms of audio. Anchor 0 at t=0, anchor 9
        # at token 12 (4 800 ms) — the classic content-069d8d06 shape: a
        # short real gap, several unmatchable beats claiming it. "high"
        # intensity (floor 1 000 ms) keeps this test isolated from the
        # separate, pre-existing per-intensity floor/steal-time mechanism in
        # _cleanup_micro_beats — that mechanism is exercised elsewhere
        # (tests/test_boundary_span_sanity.py) and is not what this test
        # proves.
        words = [_fill(i) for i in range(30)]
        for j, w in enumerate("alpha bravo charlie".split()):
            words[j] = w
        for j, w in enumerate("delta echo foxtrot".split()):
            words[12 + j] = w

        beats = [
            {
                "beat_order": 0, "start_hint": "alpha bravo charlie",
                "end_hint": "alpha bravo charlie", "suggested_duration_sec": 3.0,
                "beat_intensity": "high", "flux_prompt": "p0",
            },
        ]
        beats += [_beat(k, intensity="high") for k in range(1, 9)]  # 8 unmatchable hints
        beats.append({
            "beat_order": 9, "start_hint": "delta echo foxtrot",
            "end_hint": "delta echo foxtrot", "suggested_duration_sec": 3.0,
            "beat_intensity": "high", "flux_prompt": "p9",
        })

        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level="WARNING"
        ) as logs:
            sections = map_storyboard_beats_to_timestamps(
                beats, _transcript(words), duration_ms=len(words) * 400,
                allow_legacy_fallback=True,
            )

        self.assertIsNotNone(sections)
        # Thinned from 10 to 4 sections (anchor, 2 kept fallback, anchor) —
        # the cluster no longer ships every one of its 8 originally-requested
        # fallback beats.
        self.assertEqual(len(sections), 4)
        self.assertTrue(
            any("STORYBOARD_FALLBACK_CLUSTER_THINNED" in m for m in logs.output),
            logs.output,
        )
        # section_order stays contiguous after thinning.
        self.assertEqual(
            [s["section_order"] for s in sections], list(range(len(sections)))
        )
        # No non-terminal beat holds under the floor.
        for s in sections[:-1]:
            span = s["audio_end_ms"] - s["audio_start_ms"]
            self.assertGreaterEqual(
                span, _MIN_FALLBACK_HOLD_MS,
                f"beat {s['beat_order']} held {span}ms, under the floor: {s}",
            )
        # The two surviving fallback beats are still flagged as such —
        # thinning changes which beats ship, not their honesty about being
        # unmatched.
        self.assertTrue(sections[1]["script_text_missing"])
        self.assertTrue(sections[2]["script_text_missing"])


if __name__ == "__main__":
    unittest.main()
