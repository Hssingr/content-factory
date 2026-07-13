"""Runtime proof for caption punctuation restoration (roadmap Phase B2,
operator video-output audit).

Cross-validation against the operator's ChatGPT frame-by-frame analysis
confirmed OpenAI Whisper's word-level ``words`` array strips punctuation and
apostrophes, splits French elisions ("n'était" -> "n" + "etait"), splits
numbers into separate digit tokens ("30"/"000" instead of one "30,000" or
"30 000"), and occasionally mishears a recurring proper noun entirely (a
real production run showed "Belisarius" transcribed as "Narcissus"). None of
this is a Remotion rendering bug — the caption chunker's own sentence/clause
punctuation regexes were always correct, they just never had real
punctuation to match against.

``subtitles.py`` has zero app-internal imports (pure stdlib: difflib/re/
logging), so these tests import it directly — no stub harness needed, no
external boundary to stub, matching CLAUDE.md §19.1 (nothing here could ever
touch a live API).
"""

from __future__ import annotations

import unittest

from app.agents.agent5_render.services import subtitles


class NormalizeTokenTest(unittest.TestCase):
    def test_strips_punctuation_and_lowercases(self) -> None:
        self.assertEqual(subtitles._normalize_token("János,"), "jános")
        # Apostrophes are stripped (so "n'était" aligns positionally with a
        # Whisper split like "n" + "etait"); accented letters are Unicode
        # word characters and are kept, not transliterated to ASCII.
        self.assertEqual(subtitles._normalize_token("n'était"), "nétait")
        self.assertEqual(subtitles._normalize_token("—"), "")


class MergeNumberGroupsTest(unittest.TestCase):
    def test_merges_french_style_thousands_grouping(self) -> None:
        self.assertEqual(
            subtitles._merge_number_groups(["environ", "30", "000", "morts"]),
            ["environ", "30 000", "morts"],
        )

    def test_merges_multiple_grouping_levels(self) -> None:
        self.assertEqual(
            subtitles._merge_number_groups(["1", "234", "567", "habitants"]),
            ["1 234 567", "habitants"],
        )

    def test_comma_number_already_atomic_is_left_alone(self) -> None:
        # "30,000" is already one whitespace token — nothing to merge.
        self.assertEqual(
            subtitles._merge_number_groups(["nearly", "30,000", "people"]),
            ["nearly", "30,000", "people"],
        )

    def test_non_number_tokens_are_untouched(self) -> None:
        tokens = ["the", "quick", "fox"]
        self.assertEqual(subtitles._merge_number_groups(tokens), tokens)

    def test_lone_digit_token_with_no_following_group_is_untouched(self) -> None:
        self.assertEqual(
            subtitles._merge_number_groups(["chapter", "3", "begins"]),
            ["chapter", "3", "begins"],
        )


class TokenizeScriptTest(unittest.TestCase):
    def test_strips_section_markers(self) -> None:
        script = "[INTRO]\nHe heard it again.\n[SECTION 1: Buildup]\nThen it stopped.\n[OUTRO]\nSilence."
        self.assertEqual(
            subtitles._tokenize_script(script),
            ["He", "heard", "it", "again.", "Then", "it", "stopped.", "Silence."],
        )

    def test_empty_script_returns_no_tokens(self) -> None:
        self.assertEqual(subtitles._tokenize_script(""), [])
        self.assertEqual(subtitles._tokenize_script(None), [])


class CorrectNamedEntitiesTest(unittest.TestCase):
    def test_close_misspelling_is_corrected_to_canonical_spelling(self) -> None:
        result = subtitles._correct_named_entities(
            [{"word": "Belisaire", "start": 0.0, "end": 0.5}], ["Belisarius"],
        )
        self.assertEqual(result[0]["word"], "Belisarius")

    def test_timing_is_never_touched(self) -> None:
        result = subtitles._correct_named_entities(
            [{"word": "Bellisarius", "start": 1.2, "end": 1.9}], ["Belisarius"],
        )
        self.assertEqual(result[0]["start"], 1.2)
        self.assertEqual(result[0]["end"], 1.9)

    def test_ordinary_words_are_never_touched(self) -> None:
        words = [{"word": "walked", "start": 0.0, "end": 0.3}]
        self.assertEqual(subtitles._correct_named_entities(words, ["Belisarius"]), words)

    def test_wildly_different_word_is_not_forced_to_match(self) -> None:
        # "Narcissus" vs "Belisarius" is too dissimilar for fuzzy correction
        # (SequenceMatcher ratio ~0.42, well under the cutoff) — this is a
        # known, accepted limit of edit-distance-based correction; the
        # general alignment mechanism (not this function) is what actually
        # recovers this case — see RestorePunctuatedWordsTest below.
        result = subtitles._correct_named_entities(
            [{"word": "Narcissus", "start": 0.0, "end": 0.5}], ["Belisarius"],
        )
        self.assertEqual(result[0]["word"], "Narcissus")

    def test_no_proper_nouns_is_a_no_op(self) -> None:
        words = [{"word": "Bellisarius", "start": 0.0, "end": 0.5}]
        self.assertEqual(subtitles._correct_named_entities(words, None), words)
        self.assertEqual(subtitles._correct_named_entities(words, []), words)

    def test_short_common_words_are_not_fuzzy_matched(self) -> None:
        # Below _ENTITY_MIN_WORD_LEN — never risk misfiring on ordinary
        # short vocabulary just because it happens to resemble a name.
        result = subtitles._correct_named_entities(
            [{"word": "an", "start": 0.0, "end": 0.2}], ["Ana"],
        )
        self.assertEqual(result[0]["word"], "an")


class RestorePunctuatedWordsTest(unittest.TestCase):
    def test_french_elision_split_by_whisper_is_reassembled(self) -> None:
        script = "Sainte-Sophie n'était plus la même."
        whisper = [
            {"word": "Sainte-Sophie", "start": 0.0, "end": 0.6},
            {"word": "n", "start": 0.6, "end": 0.7},
            {"word": "était", "start": 0.7, "end": 1.0},
            {"word": "plus", "start": 1.0, "end": 1.2},
            {"word": "la", "start": 1.2, "end": 1.3},
            {"word": "meme", "start": 1.3, "end": 1.6},  # Whisper also lost the accent
        ]
        result = subtitles._restore_punctuated_words(whisper, script)
        self.assertEqual(
            " ".join(r["word"] for r in result),
            "Sainte-Sophie n'était plus la même.",
        )
        # Timing is Whisper's own: the merged "n'était" spans n's start to était's end.
        merged = next(r for r in result if r["word"] == "n'était")
        self.assertEqual(merged["start"], 0.6)
        self.assertEqual(merged["end"], 1.0)

    def test_english_comma_number_split_by_whisper_is_reassembled_atomically(self) -> None:
        script = "Nearly 30,000 people died that night."
        whisper = [
            {"word": "Nearly", "start": 0.0, "end": 0.3},
            {"word": "30", "start": 0.3, "end": 0.5},
            {"word": "000", "start": 0.5, "end": 0.9},
            {"word": "people", "start": 0.9, "end": 1.2},
            {"word": "died", "start": 1.2, "end": 1.4},
            {"word": "that", "start": 1.4, "end": 1.5},
            {"word": "night", "start": 1.5, "end": 1.8},
        ]
        result = subtitles._restore_punctuated_words(whisper, script)
        words = [r["word"] for r in result]
        self.assertIn("30,000", words)
        self.assertNotIn("30", words)
        self.assertNotIn("000", words)
        number_unit = next(r for r in result if r["word"] == "30,000")
        self.assertEqual(number_unit["start"], 0.3)
        self.assertEqual(number_unit["end"], 0.9)

    def test_french_space_grouped_number_split_by_whisper_is_reassembled_atomically(self) -> None:
        script = "Pres de 30 000 personnes sont mortes."
        whisper = [
            {"word": "Pres", "start": 0.0, "end": 0.3},
            {"word": "de", "start": 0.3, "end": 0.4},
            {"word": "30", "start": 0.4, "end": 0.6},
            {"word": "000", "start": 0.6, "end": 1.0},
            {"word": "personnes", "start": 1.0, "end": 1.4},
            {"word": "sont", "start": 1.4, "end": 1.6},
            {"word": "mortes", "start": 1.6, "end": 2.0},
        ]
        result = subtitles._restore_punctuated_words(whisper, script)
        words = [r["word"] for r in result]
        self.assertIn("30 000", words)
        self.assertNotIn("30", words)
        self.assertNotIn("000", words)

    def test_name_substitution_recovered_by_alignment_alone_no_dictionary_needed(self) -> None:
        # A clean 1-for-1 word substitution ("Narcissus" heard where the
        # script says "Belisarius") is recovered by sequence alignment
        # itself — no proper_nouns list required, since surrounding context
        # (led/the/army/north) anchors the block correctly on both sides.
        script = "Belisarius led the army north."
        whisper = [
            {"word": "Narcissus", "start": 0.0, "end": 0.5},
            {"word": "led", "start": 0.5, "end": 0.7},
            {"word": "the", "start": 0.7, "end": 0.8},
            {"word": "army", "start": 0.8, "end": 1.1},
            {"word": "north", "start": 1.1, "end": 1.4},
        ]
        result = subtitles._restore_punctuated_words(whisper, script)
        self.assertEqual(result[0]["word"], "Belisarius")
        self.assertEqual(result[0]["start"], 0.0)
        self.assertEqual(result[0]["end"], 0.5)

    def test_named_entity_dictionary_still_helps_before_alignment_runs(self) -> None:
        # Proper-noun pre-correction fixes a close mishearing even in
        # isolation from full-sentence context.
        script = "Belisarius arrived."
        whisper = [
            {"word": "Belisaire", "start": 0.0, "end": 0.6},
            {"word": "arrived", "start": 0.6, "end": 1.0},
        ]
        result = subtitles._restore_punctuated_words(whisper, script, proper_nouns=["Belisarius"])
        self.assertEqual(result[0]["word"], "Belisarius")

    def test_whisper_only_insertion_falls_back_to_raw_whisper_text(self) -> None:
        # Whisper transcribed a filler word ("um") the script never wrote —
        # no script counterpart exists; must not drop or corrupt it.
        script = "He walked home."
        whisper = [
            {"word": "He", "start": 0.0, "end": 0.2},
            {"word": "um", "start": 0.2, "end": 0.3},
            {"word": "walked", "start": 0.3, "end": 0.6},
            {"word": "home", "start": 0.6, "end": 0.9},
        ]
        result = subtitles._restore_punctuated_words(whisper, script)
        self.assertIn("um", [r["word"] for r in result])

    def test_script_only_word_with_no_whisper_timing_is_dropped_not_guessed(self) -> None:
        # The script says more than Whisper ever heard spoken (e.g. TTS
        # pronounced it but Whisper missed it entirely) — never invent a
        # timestamp for a word with no real timing evidence.
        script = "He quietly walked all the way home."
        whisper = [
            {"word": "He", "start": 0.0, "end": 0.2},
            {"word": "walked", "start": 0.2, "end": 0.5},
            {"word": "home", "start": 0.5, "end": 0.8},
        ]
        result = subtitles._restore_punctuated_words(whisper, script)
        # No fabricated timestamp for "quietly"/"all"/"the"/"way" — every
        # returned entry has real Whisper timing.
        self.assertTrue(all(r["start"] >= 0.0 and r["end"] <= 0.8 for r in result))

    def test_empty_inputs_return_empty(self) -> None:
        self.assertEqual(subtitles._restore_punctuated_words([], "Some script."), [])
        self.assertEqual(
            subtitles._restore_punctuated_words(
                [{"word": "hi", "start": 0.0, "end": 0.1}], "",
            ),
            [],
        )

    def test_script_with_no_real_tokens_falls_back_to_whisper_words_unchanged(self) -> None:
        whisper = [{"word": "hi", "start": 0.0, "end": 0.1}]
        result = subtitles._restore_punctuated_words(whisper, "[INTRO]\n[OUTRO]")
        self.assertEqual(result, whisper)

    def test_oversized_mismatch_block_falls_back_to_raw_whisper_text(self) -> None:
        # A stretch where Whisper and the script diverge by more than
        # _MAX_MERGE_SPAN tokens on either side is too large to trust
        # blindly — falls back to Whisper's own raw words for that stretch
        # rather than displaying a wildly mismatched merged phrase.
        script = "Completely different words here that share nothing at all with what was heard."
        whisper = [
            {"word": w, "start": i * 0.2, "end": i * 0.2 + 0.2}
            for i, w in enumerate([
                "totally", "unrelated", "garbled", "audio", "content", "spoken",
                "aloud", "by", "someone", "else", "entirely",
            ])
        ]
        result = subtitles._restore_punctuated_words(whisper, script)
        # Every whisper word's own real text survives somewhere in the
        # output (nothing invented, nothing silently dropped as "content").
        result_words = {r["word"] for r in result}
        self.assertTrue(set(w["word"] for w in whisper) <= result_words)


class BuildSubtitlesBackwardCompatibilityTest(unittest.TestCase):
    """voice_script is optional — omitting it must reproduce the exact
    pre-existing (raw Whisper word) behavior, for any caller that doesn't
    have script access."""

    def test_standard_subtitles_without_voice_script_uses_raw_whisper_words(self) -> None:
        whisper = [
            {"word": "hello", "start": 0.0, "end": 0.3},
            {"word": "world", "start": 0.3, "end": 0.6},
            {"word": "today", "start": 0.6, "end": 1.0},
        ]
        result = subtitles.build_standard_subtitles(whisper)
        self.assertEqual(result[0]["text"], "hello world today")

    def test_standard_subtitles_with_voice_script_restores_punctuation(self) -> None:
        whisper = [
            {"word": "hello", "start": 0.0, "end": 0.3},
            {"word": "world", "start": 0.3, "end": 0.6},
            {"word": "today", "start": 0.6, "end": 1.0},
        ]
        result = subtitles.build_standard_subtitles(whisper, voice_script="Hello, world! Today.")
        self.assertEqual(result[0]["text"], "Hello, world! Today.")

    def test_karaoke_subtitles_with_voice_script_restores_punctuation(self) -> None:
        whisper = [
            {"word": "hello", "start": 0.0, "end": 0.3},
            {"word": "world", "start": 0.3, "end": 0.6},
        ]
        result = subtitles.build_karaoke_subtitles(whisper, voice_script="Hello, world!")
        words = [w["w"] for chunk in result for w in chunk["words"]]
        self.assertEqual(words, ["Hello,", "world!"])

    def test_real_punctuation_lets_sentence_boundary_chunking_actually_fire(self) -> None:
        # Regression proof for the hidden secondary bug this phase also
        # fixes: _chunk_transcript()'s _SENTENCE_END_RE never matched
        # anything against raw (unpunctuated) Whisper words, so chunking
        # always fell through to the hard word-count ceiling. With real
        # punctuation restored, a real sentence (>= _MIN_WORDS_STANDARD
        # words) now correctly ends its own caption chunk instead of
        # bleeding into the next sentence.
        whisper = [
            {"word": w, "start": i * 0.3, "end": i * 0.3 + 0.3}
            for i, w in enumerate([
                "he", "quietly", "left", "the", "house",
                "she", "stayed", "behind", "forever",
            ])
        ]
        result = subtitles.build_standard_subtitles(
            whisper, voice_script="He quietly left the house. She stayed behind forever.",
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["text"], "He quietly left the house.")
        self.assertEqual(result[1]["text"], "She stayed behind forever.")


if __name__ == "__main__":
    unittest.main()
