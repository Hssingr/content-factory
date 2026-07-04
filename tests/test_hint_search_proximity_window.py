"""Runtime proof for the hint-search proximity window (frozen-frame fix).

Reconstructs, with a real synthetic Whisper transcript and the real internal
mapping chain (map_storyboard_beats_to_timestamps → _locate_beat_span →
_locate_phrase → _search_subsequence → _resolve_boundaries — nothing internal
is stubbed; no external API exists on this path), the exact failure shape of
run 9500c231 beat 50: narration reuses a phrase ("long before the …"), one
beat's start_hint only fuzzy-matches a distant duplicate of that phrase, and
without a proximity bound the beat anchors minutes downstream — freezing the
previous beat's image for the whole gap and desyncing every later beat.

Verified behaviors:
  1. Proximity bound — a match (full or prefix) beyond expected + 60 s is
     rejected; the beat degrades to local fallback instead of corrupting the
     timeline (and the rejection is logged as HINT_MATCH_REJECTED_OUT_OF_WINDOW).
  2. Shrink-prefix demotion — a ≤3-token match is only accepted within
     expected + 15 s; the same distance is fine for a full-phrase match.
  3. Happy path unchanged — consecutive well-formed hints all match exactly.
"""

import logging
import unittest

from app.agents.agent4_visuals.subagents.storyboard import (
    _HINT_SEARCH_WINDOW_MS,
    _HINT_SHORT_PREFIX_MAX_TOKENS,
    _HINT_SHORT_PREFIX_WINDOW_MS,
    _locate_phrase,
    _flatten_transcript,
    _search_subsequence,
    map_storyboard_beats_to_timestamps,
)

_WORD_MS = 400  # one transcript token every 400 ms → token index i starts at i*0.4 s


def _fill(i: int) -> str:
    """Unique letter-only filler token (no digits — keeps normalization inert)."""
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


def _beat(order: int, start_hint: str, end_hint: str = "", suggested: float = 3.0) -> dict:
    return {
        "beat_order": order,
        "start_hint": start_hint,
        "end_hint": end_hint or start_hint,
        "visual_intent": f"intent {order}",
        "suggested_duration_sec": suggested,
        "beat_intensity": "medium",
        "flux_prompt": f"prompt {order}",
    }


def _words_with(placements: dict[int, str], total: int) -> list[str]:
    """Build `total` unique filler words, overwriting `placements` (idx → phrase)."""
    words = [_fill(i) for i in range(total)]
    for start, phrase in placements.items():
        for j, w in enumerate(phrase.split()):
            words[start + j] = w
    return words


class TestProximityWindowRegression(unittest.TestCase):
    """Scenario A — the 9500c231 beat-50 shape: distant duplicate phrase."""

    def _run(self):
        # 400 tokens → 160 s of audio.
        words = _words_with(
            {
                0:   "alpha bravo charlie delta echo foxtrot",          # beat 0 (t=0.0s)
                10:  "long before the first ranger ever",               # beat 1 (t=4.0s)
                # beat 2's hint full sequence exists ONLY here, at t=120s —
                # and its 3-token prefix "long before the" also first re-occurs
                # here after the cursor (beat 1's match consumed the first one).
                300: "long before the trail systems existed",
                22:  "golf hotel india juliet kilo lima",               # beat 3 (t=8.8s)
                30:  "mike november oscar papa quebec romeo",           # beat 4 (t=12.0s)
            },
            total=400,
        )
        beats = [
            _beat(0, "alpha bravo charlie delta echo foxtrot"),
            _beat(1, "long before the first ranger ever"),
            _beat(2, "long before the trail systems existed"),
            _beat(3, "golf hotel india juliet kilo lima"),
            _beat(4, "mike november oscar papa quebec romeo"),
        ]
        return map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=400 * _WORD_MS,
            allow_legacy_fallback=True, language="en",
        )

    def test_distant_duplicate_is_rejected_and_beat_falls_back(self):
        with self.assertLogs(
            "app.agents.agent4_visuals.subagents.storyboard", level=logging.WARNING
        ) as logs:
            sections = self._run()
        self.assertIsNotNone(sections)

        # Beat 2 must be a fallback (script_text degrades to visual_intent),
        # NOT anchored at the 120 s duplicate.
        self.assertEqual(sections[2]["script_text"], "intent 2")
        self.assertLess(sections[2]["audio_start_ms"], 30_000)

        # The old bug: beat 1 absorbed the whole gap (would end at 120_000 ms).
        self.assertLess(sections[1]["audio_end_ms"], 30_000)

        # Beat 3 re-anchors at its true position (token 22 → 8 800 ms).
        self.assertEqual(sections[3]["audio_start_ms"], 8_800)

        # No non-terminal beat may span the old 100 s+ freeze.
        for s in sections[:-1]:
            self.assertLess(
                s["audio_end_ms"] - s["audio_start_ms"], 20_000,
                f"beat {s['beat_order']} froze: {s}",
            )

        # The guard's firing is observable in logs.
        self.assertTrue(
            any("HINT_MATCH_REJECTED_OUT_OF_WINDOW" in m for m in logs.output),
            logs.output,
        )


class TestShrinkPrefixDemotion(unittest.TestCase):
    """Scenario B — 3-token matches get the tight 15 s window; full matches get 60 s."""

    def test_full_match_within_60s_is_accepted(self):
        # Full 6-token sequence at t=16 s, expected ≈ 3 s → inside the 60 s
        # window (and inside the 20 s span-sanity budget, so the anchor is
        # kept — the span check is exercised separately in
        # tests/test_boundary_span_sanity.py).
        words = _words_with(
            {0: "alpha bravo charlie delta echo foxtrot",
             40: "mike november oscar papa quebec romeo"},
            total=200,
        )
        beats = [
            _beat(0, "alpha bravo charlie delta echo foxtrot"),
            _beat(1, "mike november oscar papa quebec romeo"),
        ]
        sections = map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=200 * _WORD_MS,
            allow_legacy_fallback=True,
        )
        self.assertEqual(sections[1]["audio_start_ms"], 16_000)
        self.assertNotEqual(sections[1]["script_text"], "intent 1")

    def test_three_token_match_beyond_15s_is_rejected(self):
        # Only the 3-token prefix exists, at t=28 s — beyond expected(3s)+15s.
        words = _words_with(
            {0: "alpha bravo charlie delta echo foxtrot",
             70: "sierra tango uniform"},
            total=200,
        )
        beats = [
            _beat(0, "alpha bravo charlie delta echo foxtrot"),
            _beat(1, "sierra tango uniform victor whiskey xray"),
        ]
        sections = map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=200 * _WORD_MS,
            allow_legacy_fallback=True,
        )
        self.assertEqual(sections[1]["script_text"], "intent 1")   # fallback
        # Not anchored at the rejected 28 s match — it gets a proportional
        # position from _resolve_boundaries instead (trailing interpolation).
        self.assertNotEqual(sections[1]["audio_start_ms"], 28_000)

    def test_three_token_match_within_15s_is_accepted(self):
        # Same 3-token-only situation but at t=4 s — inside expected(3s)+15s.
        words = _words_with(
            {0: "alpha bravo charlie delta echo foxtrot",
             10: "sierra tango uniform"},
            total=200,
        )
        beats = [
            _beat(0, "alpha bravo charlie delta echo foxtrot"),
            _beat(1, "sierra tango uniform victor whiskey xray"),
        ]
        sections = map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=200 * _WORD_MS,
            allow_legacy_fallback=True,
        )
        self.assertEqual(sections[1]["audio_start_ms"], 4_000)
        self.assertNotEqual(sections[1]["script_text"], "intent 1")

    def test_short_full_hint_gets_tight_window_too(self):
        # A hint whose FULL form is 3 tokens is as ambiguous as a 3-token
        # prefix — the tight window applies by candidate length.
        flat = _flatten_transcript(_transcript(_words_with(
            {70: "sierra tango uniform"}, total=200,
        )))
        hit = _locate_phrase(flat, 0, "sierra tango uniform", expected_start_ms=0.0)
        self.assertIsNone(hit)   # 28 s > 0 + 15 s tight window
        hit_unbounded = _locate_phrase(flat, 0, "sierra tango uniform")
        self.assertIsNotNone(hit_unbounded)  # bound disabled when no expectation


class TestHappyPathUnchanged(unittest.TestCase):
    """Scenario C — consecutive correct hints behave exactly as before."""

    def test_all_beats_match_exactly(self):
        words = _words_with(
            {0: "alpha bravo charlie delta echo foxtrot",
             8: "golf hotel india juliet kilo lima",
             16: "mike november oscar papa quebec romeo",
             24: "sierra tango uniform victor whiskey xray"},
            total=120,
        )
        beats = [
            _beat(0, "alpha bravo charlie delta echo foxtrot"),
            _beat(1, "golf hotel india juliet kilo lima"),
            _beat(2, "mike november oscar papa quebec romeo"),
            _beat(3, "sierra tango uniform victor whiskey xray"),
        ]
        sections = map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=120 * _WORD_MS,
            allow_legacy_fallback=False,
        )
        self.assertIsNotNone(sections)
        starts = [s["audio_start_ms"] for s in sections]
        self.assertEqual(starts, [0, 3_200, 6_400, 9_600])
        for s in sections:
            self.assertNotEqual(s["script_text"], f"intent {s['beat_order']}")


class TestSearchSubsequenceBound(unittest.TestCase):
    """Static/unit checks on the new bound plumbing."""

    def test_constants(self):
        self.assertEqual(_HINT_SEARCH_WINDOW_MS, 60_000)
        self.assertEqual(_HINT_SHORT_PREFIX_WINDOW_MS, 15_000)
        self.assertEqual(_HINT_SHORT_PREFIX_MAX_TOKENS, 3)

    def test_max_start_ms_stops_scan_early(self):
        flat = _flatten_transcript(_transcript(_words_with(
            {50: "alpha bravo charlie"}, total=100,
        )))
        # Match starts at token 50 → 20 000 ms.
        self.assertIsNotNone(_search_subsequence(flat, 0, ["alpha", "bravo", "charlie"]))
        self.assertIsNotNone(
            _search_subsequence(flat, 0, ["alpha", "bravo", "charlie"], max_start_ms=20_000)
        )
        self.assertIsNone(
            _search_subsequence(flat, 0, ["alpha", "bravo", "charlie"], max_start_ms=19_999)
        )


if __name__ == "__main__":
    unittest.main()
