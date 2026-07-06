"""Runtime proof for the anchor span-sanity check + proportional interpolation.

Second line of defense behind the hint-search proximity window
(tests/test_hint_search_proximity_window.py): a wrong hint match can still
land INSIDE the 60 s window (a duplicate phrase 30–50 s ahead). After the
matching loop, `_demote_out_of_span_anchors()` drops any anchor whose gap
from the last trusted anchor cannot be covered by the beats between them
(gap > max(4 × Σ suggested, 20 s)), and `_resolve_boundaries()` now
interpolates unmatched beats proportionally between trusted anchors instead
of letting the last beat before the next anchor absorb the whole gap as a
frozen frame (run 9500c231: beats 51–104 crammed into floor-width slots
while one beat froze for 285 s).

Everything here drives the real internal chain
(map_storyboard_beats_to_timestamps → _demote_out_of_span_anchors →
_resolve_boundaries → _cleanup_micro_beats) on real synthetic transcripts —
nothing internal is stubbed; this path calls no external API.
"""

import logging
import unittest

from app.agents.agent4_visuals.subagents.storyboard import (
    _SPAN_SANITY_FACTOR,
    _SPAN_SANITY_MIN_MS,
    _flatten_transcript,
    _resolve_boundaries,
    map_storyboard_beats_to_timestamps,
)

_WORD_MS = 400  # token i starts at i*0.4 s


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


def _beat(order: int, start_hint: str, suggested: float = 3.0) -> dict:
    return {
        "beat_order": order,
        "start_hint": start_hint,
        "end_hint": start_hint,
        "visual_intent": f"intent {order}",
        "suggested_duration_sec": suggested,
        "beat_intensity": "medium",
        "flux_prompt": f"prompt {order}",
    }


def _words_with(placements: dict[int, str], total: int) -> list[str]:
    words = [_fill(i) for i in range(total)]
    for start, phrase in placements.items():
        for j, w in enumerate(phrase.split()):
            words[start + j] = w
    return words


class TestSpanSanityDemotion(unittest.TestCase):
    """An in-window wrong anchor is demoted; a later coverable anchor is kept."""

    def _run(self):
        # 150 tokens → 60 s. Beat 2's hint exists ONLY at t=40 s (a duplicate
        # phrase INSIDE the 60 s proximity window, so matching accepts it) —
        # but only one beat separates it from the trusted anchor at 4 s, so a
        # 36 s gap fails span sanity (threshold: max(4×3 s, 20 s) = 20 s).
        # Beats 3–9 have unmatchable hints. Beat 10's hint sits at t=44.8 s —
        # a 40.8 s gap covered by 9 beats (Σ suggested = 27 s → threshold
        # 108 s), so it is KEPT and the gap is interpolated across beats 1–9.
        words = _words_with(
            {
                0:   "alpha bravo charlie delta echo foxtrot",       # beat 0 (0.0 s)
                10:  "golf hotel india juliet kilo lima",            # beat 1 (4.0 s)
                100: "mike november oscar papa quebec romeo",        # beat 2's ONLY match (40.0 s)
                112: "sierra tango uniform victor whiskey xray",     # beat 10 (44.8 s)
            },
            total=150,
        )
        beats = [
            _beat(0, "alpha bravo charlie delta echo foxtrot"),
            _beat(1, "golf hotel india juliet kilo lima"),
            _beat(2, "mike november oscar papa quebec romeo"),
        ]
        beats += [_beat(k, f"nomatch{_fill(k)} " * 6) for k in range(3, 10)]
        beats.append(_beat(10, "sierra tango uniform victor whiskey xray"))
        return map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=150 * _WORD_MS,
            allow_legacy_fallback=True,
        )

    def test_bad_anchor_demoted_good_anchor_kept_gap_interpolated(self):
        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level=logging.WARNING
        ) as logs:
            sections = self._run()
        self.assertIsNotNone(sections)

        # The demotion fired and is observable.
        self.assertTrue(
            any("ANCHOR_SPAN_SANITY_DROPPED" in m for m in logs.output), logs.output
        )

        # Beat 2 was demoted: its script_text is empty, flagged, and it
        # is NOT anchored at the 40 s duplicate.
        self.assertEqual(sections[2]["script_text"], "")
        self.assertEqual(sections[2]["script_text_source"], "empty_fallback_no_transcript_span")
        self.assertTrue(sections[2]["script_text_missing"])
        self.assertLess(sections[2]["audio_start_ms"], 40_000)

        # Beat 10's anchor survived span sanity (9 covering beats) — kept exact.
        self.assertEqual(sections[10]["audio_start_ms"], 44_800)

        # The old failure shape is gone: without demotion+interpolation,
        # beat 1 would span 4 s → 40 s (36 s frozen frame). Now NO beat —
        # terminal included — may span more than 20 s.
        for s in sections:
            span = s["audio_end_ms"] - s["audio_start_ms"]
            self.assertLessEqual(span, 20_000, f"beat {s['beat_order']} froze: {s}")

        # Interpolated beats 2–9 are evenly spread (equal suggested weights):
        # increments of 40 800 / 9 ≈ 4 533 ms between consecutive starts.
        starts = [s["audio_start_ms"] for s in sections]
        self.assertEqual(starts, sorted(starts))
        diffs = [starts[k + 1] - starts[k] for k in range(1, 10)]
        for d in diffs:
            self.assertAlmostEqual(d, 40_800 / 9, delta=2)


class TestUnmatchedRunInterpolation(unittest.TestCase):
    """Unmatched beats between two good anchors tile the gap proportionally."""

    def test_even_interpolation_replaces_floor_cramming(self):
        # Beats 1–4 unmatchable; beat 0 at 0 s, beat 5 at 30 s, audio 36 s.
        # Span sanity keeps beat 5 (gap 30 s ≤ 4 × 15 s). Old behavior:
        # starts 0,0,0,0,0,30 000 → floors crammed beats 1–4 at 2 s slots and
        # beat 4 froze for ~22 s. New behavior: even 6 s tiling.
        words = _words_with(
            {0: "alpha bravo charlie delta echo foxtrot",
             75: "golf hotel india juliet kilo lima"},
            total=90,
        )
        beats = [_beat(0, "alpha bravo charlie delta echo foxtrot")]
        beats += [_beat(k, f"nomatch{_fill(k)} " * 6) for k in range(1, 5)]
        beats.append(_beat(5, "golf hotel india juliet kilo lima"))
        sections = map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=90 * _WORD_MS,
            allow_legacy_fallback=True,
        )
        starts = [s["audio_start_ms"] for s in sections]
        self.assertEqual(starts, [0, 6_000, 12_000, 18_000, 24_000, 30_000])
        spans = [s["audio_end_ms"] - s["audio_start_ms"] for s in sections]
        self.assertEqual(spans, [6_000] * 6)

    def test_no_anchor_at_all_tiles_full_duration(self):
        words = [_fill(i) for i in range(30)]   # nothing matches
        beats = [_beat(k, f"nomatch{_fill(k)} " * 6) for k in range(4)]
        sections = map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=12_000,
            allow_legacy_fallback=True,
        )
        starts = [s["audio_start_ms"] for s in sections]
        self.assertEqual(starts, [0, 3_000, 6_000, 9_000])


class TestResolveBoundariesUnit(unittest.TestCase):
    """Direct unit checks on interpolation weighting and the leading run."""

    def _flat(self, n_tokens: int):
        return _flatten_transcript(_transcript([_fill(i) for i in range(n_tokens)]))

    def test_weighted_interpolation(self):
        # Beats 1 (2 s) and 2 (6 s) unmatched between anchors at 0 and 22 s
        # (token 55): the gap splits by suggested weight, not evenly. Spans are
        # kept above the intensity floors so _cleanup_micro_beats stays inert.
        flat = self._flat(80)
        beats = [_beat(0, "x"), _beat(1, "x", suggested=2.0),
                 _beat(2, "x", suggested=6.0), _beat(3, "x")]
        matches = [(0, 1), None, None, (55, 56)]
        boundaries = _resolve_boundaries(matches, beats, flat, duration_ms=30_000)
        starts = [b[0] for b in boundaries]
        # weights: beat0=3, beat1=2, beat2=6 (total 11) across 22 000 ms
        self.assertEqual(starts[0], 0)
        self.assertAlmostEqual(starts[1], 22_000 * 3 / 11, delta=2)
        self.assertAlmostEqual(starts[2], 22_000 * 5 / 11, delta=2)
        self.assertEqual(starts[3], 22_000)
        self.assertEqual(boundaries[-1][1], 30_000)

    def test_leading_unmatched_run_starts_at_zero(self):
        flat = self._flat(40)
        beats = [_beat(0, "x"), _beat(1, "x"), _beat(2, "x")]
        matches = [None, None, (15, 16)]   # first anchor at 6 000 ms
        boundaries = _resolve_boundaries(matches, beats, flat, duration_ms=12_000)
        starts = [b[0] for b in boundaries]
        self.assertEqual(starts[0], 0)
        self.assertEqual(starts[1], 3_000)   # even split of the 6 s lead
        self.assertEqual(starts[2], 6_000)

    def test_constants(self):
        self.assertEqual(_SPAN_SANITY_FACTOR, 4.0)
        self.assertEqual(_SPAN_SANITY_MIN_MS, 20_000)


if __name__ == "__main__":
    unittest.main()
