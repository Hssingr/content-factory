"""Runtime proof: hint/transcript matching folds diacritics symmetrically.

Content 069d8d06 diagnostic (2026-08-05, code_report/TASK2 investigation): the
storyboard's own transcript-side normalizer (``_normalize_word`` in
``agent4_visuals/subagents/storyboard.py``) already strips accents via NFD
decomposition, but the hint-side normalizer — ``app.shared.text_normalize
.normalize_for_matching``, also used by Agent 3 — did not. That asymmetry
silently fails hint matching for any accented/foreign proper noun whenever
Whisper's own transcription happens to drop (or add) an accent, exactly the
real production shape: a storyboard hint spelled "Potosí" against a Whisper
transcript that spelled the same word "Potosi". ``normalize_for_matching``
now folds diacritics (NFD, strip combining marks) before anything else, on
both sides of every matching call site.
"""

import unittest

from app.agents.agent4_visuals.subagents.storyboard import (
    _flatten_transcript,
    _locate_phrase,
    map_storyboard_beats_to_timestamps,
)
from app.shared.text_normalize import normalize_for_matching


class TestNormalizeForMatchingFoldsDiacritics(unittest.TestCase):
    def test_accented_and_unaccented_forms_normalize_identically(self):
        self.assertEqual(
            normalize_for_matching("Potosí", "en"),
            normalize_for_matching("Potosi", "en"),
        )
        self.assertEqual(normalize_for_matching("Potosí", "en"), ["potosi"])

    def test_multiple_diacritics_across_unicode_blocks_fold(self):
        self.assertEqual(normalize_for_matching("café", "en"), ["cafe"])
        self.assertEqual(normalize_for_matching("naïve", "en"), ["naive"])
        self.assertEqual(normalize_for_matching("Genève", "en"), ["geneve"])
        self.assertEqual(normalize_for_matching("asientos", "en"), ["asientos"])

    def test_digit_expansion_still_works_after_diacritic_folding(self):
        # Regression guard: the new diacritic-folding step must run without
        # disturbing the existing digit -> spoken-word expansion.
        self.assertEqual(
            normalize_for_matching("In 1984, he was 24.", "en"),
            ["in", "nineteen", "eighty", "four", "he", "was", "twenty", "four"],
        )

    def test_plain_ascii_text_is_unaffected(self):
        self.assertEqual(
            normalize_for_matching("The riches of the mountain.", "en"),
            ["the", "riches", "of", "the", "mountain"],
        )


def _transcript(words: list[str], word_ms: int = 400) -> list[dict]:
    return [
        {"word": w, "start": i * word_ms / 1000.0, "end": (i + 1) * word_ms / 1000.0}
        for i, w in enumerate(words)
    ]


class TestAccentedHintMatchesUnaccentedTranscript(unittest.TestCase):
    """Reproduces the exact content 069d8d06 asymmetry: a storyboard hint
    keeps the accented spelling of a proper noun while Whisper's own
    transcription of the spoken audio drops it."""

    def test_locate_phrase_matches_across_the_accent_gap(self):
        # Transcript ("Whisper") spells it without the accent.
        words = "the riches of Potosi are built on bodies".split()
        flat = _flatten_transcript(_transcript(words))
        # Hint (storyboard/Claude) keeps the accented spelling.
        hit = _locate_phrase(flat, 0, "The riches of Potosí are built")
        self.assertIsNotNone(hit)
        start_idx, _end_idx = hit
        self.assertEqual(start_idx, 0)

    def test_reverse_direction_also_matches(self):
        # Transcript spells it WITH the accent; hint omits it — the asymmetry
        # this fix closes could fail in either direction depending on which
        # side Whisper happened to preserve.
        words = "the riches of Potosí are built on bodies".split()
        flat = _flatten_transcript(_transcript(words))
        hit = _locate_phrase(flat, 0, "The riches of Potosi are built")
        self.assertIsNotNone(hit)

    def test_full_beat_mapping_anchors_the_accented_hint(self):
        words = "the riches of Potosi are built on bodies not just ore".split()
        beats = [{
            "beat_order": 0,
            "start_hint": "The riches of Potosí are built",
            "end_hint": "built on bodies, not just ore.",
            "suggested_duration_sec": 4.0,
            "beat_intensity": "medium",
            "flux_prompt": "prompt",
        }]
        sections = map_storyboard_beats_to_timestamps(
            beats, _transcript(words), duration_ms=len(words) * 400,
            allow_legacy_fallback=True,
        )
        self.assertIsNotNone(sections)
        self.assertEqual(sections[0]["audio_start_ms"], 0)
        self.assertFalse(sections[0]["script_text_missing"])
        self.assertEqual(sections[0]["script_text_source"], "whisper_transcript")


if __name__ == "__main__":
    unittest.main()
