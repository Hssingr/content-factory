"""Runtime proof for "hook by construction" (roadmap 4c / audit P1-4,
code_report/forensic_output_audit_borrasca_run.md).

A real production run showed the blueprint's own hook generated — designed
to pass check_hook_quality's shape rules (<=15 words, concrete, withheld
mechanism) — but buried at sentence 6 of the INTRO, with the script instead
opening on a "character roster" sentence that check_hook_quality() waved
through anyway (also concrete, also <=15 words). The validator was checking
shape, not which sentence Claude chose to place first. This proves the fix:
the blueprint's hook is now deterministically prepended as the INTRO's
literal opening line during assembly, and check_hook_quality() remains
telemetry only — it never triggers a retry, even when the check itself would
still fail.

Only the paid Claude section-generation call (`scripts.generate_section`) is
stubbed — every other function (`_generate_intro_section`,
`_generate_section_once`, `_apply_hook_by_construction`,
`_collect_section_issues`, `_build_section_generation_context`,
`_create_section_loop_state`) is the real, unmocked implementation.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery.services import scripts


class TestApplyHookByConstruction(unittest.TestCase):
    def test_prepends_hook_and_adds_terminal_punctuation(self):
        result = {"script_text": "A boy named Sam moved to town."}
        hook = "A grinding metal sound never stops echoing from the mountain"

        updated = scripts._apply_hook_by_construction(result, hook)

        self.assertEqual(
            updated["script_text"],
            "A grinding metal sound never stops echoing from the mountain. "
            "A boy named Sam moved to town.",
        )

    def test_preserves_existing_terminal_punctuation(self):
        result = {"script_text": "Body text."}

        updated = scripts._apply_hook_by_construction(result, "Something happened!")

        self.assertTrue(updated["script_text"].startswith("Something happened! "))

    def test_noop_when_hook_empty(self):
        result = {"script_text": "Body text."}

        updated = scripts._apply_hook_by_construction(result, "")

        self.assertEqual(updated, result)

    def test_noop_when_intro_already_opens_with_hook_verbatim(self):
        result = {"script_text": "A grinding metal sound never stops. More text follows."}

        updated = scripts._apply_hook_by_construction(
            result, "A grinding metal sound never stops"
        )

        # No duplicated hook text prepended — the sentence was already there.
        self.assertEqual(updated["script_text"], result["script_text"])

    def test_hook_becomes_whole_intro_when_generated_body_is_empty(self):
        result = {"script_text": ""}

        updated = scripts._apply_hook_by_construction(result, "A sound echoes")

        self.assertEqual(updated["script_text"], "A sound echoes.")

    def test_does_not_mutate_input_dict(self):
        result = {"script_text": "Body.", "other_field": "kept"}

        updated = scripts._apply_hook_by_construction(result, "Hook here")

        self.assertEqual(result["script_text"], "Body.")  # original untouched
        self.assertEqual(updated["other_field"], "kept")  # other keys preserved


class TestIntroGenerationConstructsHookDeterministically(unittest.TestCase):
    """Full-chain proof through the real _generate_intro_section() ->
    _generate_section_once() -> _apply_hook_by_construction() path."""

    def _blueprint(self, hook: str | None = None):
        return {
            "hook": hook if hook is not None else (
                "A grinding metal sound never stops echoing from the mountain"
            ),
            "central_question": "What is making the sound?",
            "major_turns": ["turn one", "turn two"],
            "final_payoff": "payoff",
            "comment_trigger": "What would you have done?",
            "midpoint_retention_trap": "a reveal",
            "suggested_section_count": 2,
            "suggested_title": "T",
        }

    def _run_intro(self, blueprint, fake_generate_section):
        context = scripts._build_section_generation_context(
            channel_voice=None, blueprint=blueprint,
        )
        state = scripts._create_section_loop_state()
        with patch.object(scripts, "generate_section", side_effect=fake_generate_section):
            scripts._generate_intro_section(
                story=SimpleNamespace(title="T", body="body"),
                blueprint=blueprint,
                channel=SimpleNamespace(niche="horror", tone="tense"),
                script_format="youtube_long",
                audio_tags_enabled=False,
                context=context,
                state=state,
            )
        return state

    def test_roster_opening_is_pushed_behind_the_deterministic_hook(self):
        """Reproduces the exact audited defect shape: Claude opens on a
        character-roster sentence instead of the blueprint's hook."""
        blueprint = self._blueprint()
        roster_opening = (
            "A boy named Sam Walker moved to Drisking, Missouri as a kid. "
            "He had a sister named Whitney, a friend named Kyle, and another "
            "named Kimber."
        )
        call_count = {"n": 0}

        def fake_generate_section(**kwargs):
            call_count["n"] += 1
            return {
                "script_text": roster_opening,
                "summary": "intro summary",
                "reveals": [],
                "open_questions": [],
                "suggests_outro": False,
                "visual_intent": {},
            }

        state = self._run_intro(blueprint, fake_generate_section)

        self.assertEqual(call_count["n"], 1)  # exactly one Claude call, no retry
        intro_text = state["sections"][0]["script_text"]
        self.assertTrue(
            intro_text.startswith(
                "A grinding metal sound never stops echoing from the mountain."
            ),
            f"INTRO did not open with the constructed hook: {intro_text[:120]!r}",
        )
        # Nothing was discarded — the roster sentence still follows, just reordered.
        self.assertIn("A boy named Sam Walker moved to Drisking, Missouri", intro_text)

    def test_check_hook_quality_stays_telemetry_only_even_on_failure(self):
        """Even a blueprint hook that itself violates check_hook_quality's own
        <=15-word rule must never trigger a second Claude call — the check
        remains observability-only per the roadmap."""
        blueprint = self._blueprint(hook=" ".join(["word"] * 30))
        call_count = {"n": 0}

        def fake_generate_section(**kwargs):
            call_count["n"] += 1
            return {
                "script_text": "Some intro body text.",
                "summary": "s", "reveals": [], "open_questions": [],
                "suggests_outro": False, "visual_intent": {},
            }

        with self.assertLogs(
            "app.agents.agent2_discovery.services.scripts", level="WARNING"
        ) as log_ctx:
            state = self._run_intro(blueprint, fake_generate_section)

        self.assertEqual(call_count["n"], 1)
        joined = " ".join(log_ctx.output)
        self.assertIn("telemetry only, no retry", joined)
        # The hook was still constructed despite failing its own word-count rule.
        self.assertTrue(state["sections"][0]["script_text"].startswith("word word"))

    def test_body_and_outro_sections_are_unaffected(self):
        """check_hook is False for body/OUTRO sections — hook-by-construction
        must never touch their opening line."""
        blueprint = self._blueprint()
        original_text = "Something else entirely happens here."

        def fake_generate_section(**kwargs):
            return {
                "script_text": original_text,
                "summary": "s", "reveals": [], "open_questions": [],
                "suggests_outro": False, "visual_intent": {},
            }

        with patch.object(scripts, "generate_section", side_effect=fake_generate_section):
            body_result = scripts._generate_section_once(
                label="SECTION 1",
                story=SimpleNamespace(title="T", body="body"),
                blueprint=blueprint,
                prior_sections_summary=[],
                visual_intent_accumulator={"avoid_repeating": []},
                channel=SimpleNamespace(niche="horror", tone="tense"),
                script_format="youtube_long",
                tts_model="sonic-3.5",
                tts_provider="cartesia",
                audio_tags_enabled=False,
                check_hook=False,
                prior_summary_text="",
            )

        self.assertEqual(body_result["script_text"], original_text)


if __name__ == "__main__":
    unittest.main()
