"""Runtime proof for pre-next-test roadmap Tier 2 (R5–R6) — caption integrity.

R5: `subtitles._tokenize_script()` must scrub every bracketed non-marker span
(ElevenLabs v3 audio tags) from the caption-restoration alignment reference.
Three confirmed production leaks drove this: FR main caption @389.2s
('…la payer." [dramatic pause] J'ai'), FR Short 0 @77.6s, FR Short 2 @39.5s —
a tag left in the reference fell inside a replace-block merge and shipped
verbatim as caption display text.

R6: all three native-adaptation base prompts carry "a question stays a
question" (a real FR adaptation shipped the final viewer-facing question with
a period instead of a question mark).

`subtitles.py` has zero app-internal imports — no stub harness needed.
No live API calls anywhere (CLAUDE.md §19.1).
"""

from __future__ import annotations

import unittest

from app.agents.agent5_render.services import subtitles


def _mk_words(spoken: str, start: float = 0.0, step: float = 0.4) -> list[dict]:
    """Whisper-shaped word list: unpunctuated tokens with synthetic timings."""
    words = []
    t = start
    for token in spoken.split():
        words.append({"word": token, "start": round(t, 2), "end": round(t + step, 2)})
        t += step
    return words


# The exact production leak shape (FR main caption #146 @389.2s): the script
# carries an authored v3 tag between a closing quote and an elision the
# alignment must merge.
_FR_SCRIPT = (
    'Que cette haine est la tienne seule, et qu\'ils ne sont ici que pour la '
    'payer." [dramatic pause] J\'ai déjà entendu des murmures.'
)
# Whisper never hears the tag (the TTS provider consumes it) and splits the
# elisions, exactly as OpenAI Whisper does for French.
_FR_SPOKEN = (
    "Que cette haine est la tienne seule et qu ils ne sont ici que pour la "
    "payer J ai déjà entendu des murmures"
)


class TokenizeScriptBracketScrubTest(unittest.TestCase):
    def test_inline_audio_tag_removed(self):
        tokens = subtitles._tokenize_script('la payer." [dramatic pause] J\'ai déjà')
        self.assertEqual(tokens, ['la', 'payer."', "J'ai", "déjà"])

    def test_multiple_tags_and_variants_removed(self):
        tokens = subtitles._tokenize_script(
            "[whispers] He opened it. [dramatic pause] The name was his. [sighs]"
        )
        self.assertEqual(
            tokens, ["He", "opened", "it.", "The", "name", "was", "his."],
        )

    def test_section_marker_lines_still_stripped(self):
        tokens = subtitles._tokenize_script(
            "[INTRO]\n\nThe house sat empty.\n\n[SECTION 1: The find]\n\nThen it began."
        )
        self.assertEqual(tokens, ["The", "house", "sat", "empty.", "Then", "it", "began."])

    def test_number_group_merge_still_works_after_scrub(self):
        tokens = subtitles._tokenize_script("[dramatic pause] 30 000 hommes sont morts.")
        self.assertEqual(tokens, ["30 000", "hommes", "sont", "morts."])

    def test_no_brackets_is_a_no_op(self):
        tokens = subtitles._tokenize_script("Plain narration with no tags at all.")
        self.assertEqual(tokens, ["Plain", "narration", "with", "no", "tags", "at", "all."])


class CaptionLeakRegressionTest(unittest.TestCase):
    """The FR #146 production defect shape, through the real public builders."""

    def test_standard_captions_carry_no_bracket_tag(self):
        captions = subtitles.build_standard_subtitles(
            _mk_words(_FR_SPOKEN), voice_script=_FR_SCRIPT,
        )
        self.assertTrue(captions)
        joined = " ".join(c["text"] for c in captions)
        self.assertNotIn("[", joined, f"tag leaked into standard captions: {joined!r}")
        self.assertNotIn("]", joined)
        # The elision merge itself must still work — the tag scrub must not
        # break the surrounding replace-block restoration.
        self.assertIn("J'ai", joined)
        self.assertIn('payer."', joined)

    def test_karaoke_captions_carry_no_bracket_tag(self):
        chunks = subtitles.build_karaoke_subtitles(
            _mk_words(_FR_SPOKEN), voice_script=_FR_SCRIPT,
        )
        self.assertTrue(chunks)
        all_words = [w["w"] for c in chunks for w in c["words"]]
        joined = " ".join(all_words)
        self.assertNotIn("[", joined, f"tag leaked into karaoke captions: {joined!r}")
        self.assertIn("J'ai", all_words)

    def test_tag_at_script_start_and_end(self):
        """Tags in leading/trailing position must scrub cleanly too (EN Short 3
        carries a leading authored tag)."""
        script = "[whispers] The old track simply ends. Would you go on? [sighs]"
        spoken = "The old track simply ends Would you go on"
        captions = subtitles.build_standard_subtitles(
            _mk_words(spoken), voice_script=script,
        )
        joined = " ".join(c["text"] for c in captions)
        self.assertNotIn("[", joined)
        self.assertIn("go on?", joined)

    def test_no_voice_script_behavior_unchanged(self):
        """Raw-Whisper mode (no voice_script) is untouched by R5."""
        captions = subtitles.build_standard_subtitles(_mk_words(_FR_SPOKEN))
        self.assertTrue(captions)
        joined = " ".join(c["text"] for c in captions)
        self.assertNotIn("[", joined)
        self.assertIn("payer", joined)  # raw, unpunctuated — as before


class QuestionStaysAQuestionPromptTest(unittest.TestCase):
    """R6 — static proof the rule reached all three native base prompts."""

    def test_rule_present_in_all_three_native_base_prompts(self):
        from app.agents.agent2_discovery import system_prompt as sp
        for name in (
            "_BASE_YOUTUBE_LONG_FORM_NATIVE",
            "_BASE_SHORT_FORM_NATIVE",
            "_BASE_CHILD_SHORT_NATIVE",
        ):
            base = getattr(sp, name)
            self.assertIn(
                "A question stays a question", base,
                f"{name} is missing the R6 question-preservation rule",
            )
            self.assertIn("question mark", base)

    def test_rule_survives_prompt_assembly(self):
        from app.agents.agent2_discovery import system_prompt as sp
        for kwargs in (
            {"script_format": "youtube_long", "content_kind": "parent_long_form"},
            {"script_format": "youtube_long", "content_kind": "child_short"},
            {"script_format": "short_form", "content_kind": "parent_long_form"},
        ):
            assembled = sp.build_native_system_prompt(tts_model="eleven_v3", **kwargs)
            self.assertIn("A question stays a question", assembled, f"missing for {kwargs}")

    def test_prompt_version_bumped_to_5_2(self):
        from app.agents.agent2_discovery import system_prompt as sp
        self.assertGreaterEqual(float(sp.PROMPT_VERSION), 5.2)


if __name__ == "__main__":
    unittest.main()
