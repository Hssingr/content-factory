"""Runtime proof for Elimination Mandate Phase 1a (D1.3, D1.4) —
code_report/forensic_output_audit_borrasca_run.md.

A real production run showed these retry loops actively degrade quality:
word counts got WORSE across correction rounds (205 -> 196 -> 218 words,
still over the 180-word cap) and shipped anyway; the AI Short quality gate's
PASSED-with-issues contract mismatch burned a retry into a worse draft;
section-retry override instructions pushed narration into a machine-gun
monotone. This file proves the replacements: exactly one Claude call per
section/Short regardless of MAJOR findings, and that findings are logged as
telemetry only, never trigger a second generation call.

Only the paid Claude boundary (``system_prompt.call_claude_structured``) is
stubbed — the real ``_generate_section_once()``, ``_run_body_section_loop()``,
``generate_script_sections()``, and ``_generate_short_script()`` functions run
unmodified, including their real deterministic checks.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import scripts
from app.agents.agent2_discovery.services.story import Story


class _Channel:
    niche = "Reddit horror story narration"
    tone = "documentary"


def _story() -> Story:
    return Story(
        url="https://reddit.com/r/nosleep/comments/abc/x",
        title="T", body="A concrete story body with real facts.", language="en",
        source_type="web", source_value="claude_web_search",
    )


def _section_payload(text: str, suggests_outro: bool = False) -> dict:
    return {
        "script_text": text,
        "summary": "summary",
        "reveals": ["turn one is revealed"],
        "open_questions": [],
        "suggests_outro": suggests_outro,
        "visual_intent": {"section_goal": "g", "primary_visual_focus": "f", "avoid_repeating": []},
    }


class TestSectionGenerationNeverRetries(unittest.TestCase):
    """D1.4: _generate_section_once() must call Claude exactly once, even when
    the draft has a MAJOR TTS/hook violation — no override-instruction retry."""

    def test_major_tts_violation_still_returns_on_first_and_only_call(self):
        # A single 40-word sentence with no terminal punctuation trips
        # check_tts_compliance's MAJOR sentence-length rule.
        bad_text = "word " * 40
        calls = {"n": 0}

        def fake_structured(**kwargs):
            calls["n"] += 1
            return _section_payload(bad_text.strip() + ".")

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            result = scripts._generate_section_once(
                label="INTRO",
                story=_story(),
                blueprint={"major_turns": ["turn one"], "final_payoff": "x", "comment_trigger": "y"},
                prior_sections_summary=[],
                visual_intent_accumulator={"avoid_repeating": []},
                channel=_Channel(),
                script_format="youtube_long",
                tts_model="sonic-2",
                tts_provider="cartesia",
                audio_tags_enabled=False,
                check_hook=True,
            )

        self.assertEqual(calls["n"], 1, "must call Claude exactly once — no retry")
        self.assertIsNotNone(result)

    def test_generation_error_returns_none_without_retry(self):
        """A hard exception from the Claude call must propagate as None on the
        first attempt — no retry loop to catch and re-attempt it."""
        calls = {"n": 0}

        def fake_structured(**kwargs):
            calls["n"] += 1
            raise RuntimeError("simulated API failure")

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            result = scripts._generate_section_once(
                label="INTRO",
                story=_story(),
                blueprint={"major_turns": [], "final_payoff": "", "comment_trigger": ""},
                prior_sections_summary=[],
                visual_intent_accumulator={"avoid_repeating": []},
                channel=_Channel(),
                script_format="youtube_long",
                tts_model="sonic-2",
                tts_provider="cartesia",
                audio_tags_enabled=False,
                check_hook=True,
            )

        self.assertEqual(calls["n"], 1)
        self.assertIsNone(result)


class TestGenerateScriptSectionsNoNarrativeRetry(unittest.TestCase):
    """D1.4: the narrative-completeness retry pass is deleted — a script whose
    final_payoff/comment_trigger are never referenced in OUTRO must NOT trigger
    any additional Claude call to regenerate a section. Proven by comparing the
    Claude call count between a completeness-satisfying OUTRO and a
    completeness-failing OUTRO: the ordinary INTRO+body+OUTRO loop shape is not
    hardcoded here (it depends on _should_stop_body_loop's own min-section
    logic), so equality between the two runs is what proves no extra call was
    added for the failing case specifically."""

    def _run(self, outro_text: str) -> int:
        blueprint = {
            "major_turns": ["the sound comes from the mine"],
            "final_payoff": "the sheriff signed her file himself and hid it for years",
            "comment_trigger": "would you have gone back for her",
            "suggested_section_count": 1,
        }
        call_log: list[str] = []

        def fake_structured(**kwargs):
            call_log.append(kwargs["user_message"])
            if "Now generate: OUTRO" in kwargs["user_message"]:
                return _section_payload(outro_text)
            return _section_payload(
                "Sam hears the sound from the mine every night.", suggests_outro=True
            )

        channel_voice = SimpleNamespace(tts_model="sonic-2", provider="cartesia")

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            scripts.generate_script_sections(
                story=_story(),
                blueprint=blueprint,
                channel=_Channel(),
                channel_voice=channel_voice,
            )
        return len(call_log)

    def test_narrative_completeness_failure_causes_no_extra_call(self):
        satisfying_outro = (
            "The sheriff signed her file himself and hid it for years. "
            "Would you have gone back for her?"
        )
        failing_outro = "They went home and nothing more was said."

        satisfying_call_count = self._run(satisfying_outro)
        failing_call_count = self._run(failing_outro)

        self.assertEqual(
            satisfying_call_count, failing_call_count,
            "a completeness-failing OUTRO must not add any extra Claude call",
        )

    def test_narrative_completeness_failure_is_still_logged_as_telemetry(self):
        blueprint = {
            "major_turns": ["the sound comes from the mine"],
            "final_payoff": "the sheriff signed her file himself and hid it for years",
            "comment_trigger": "would you have gone back for her",
            "suggested_section_count": 1,
        }

        def fake_structured(**kwargs):
            if "Now generate: OUTRO" in kwargs["user_message"]:
                return _section_payload("They went home and nothing more was said.")
            return _section_payload(
                "Sam hears the sound from the mine every night.", suggests_outro=True
            )

        channel_voice = SimpleNamespace(tts_model="sonic-2", provider="cartesia")

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured), \
             self.assertLogs("app.agents.agent2_discovery.services.scripts", level="WARNING") as log_ctx:
            result = scripts.generate_script_sections(
                story=_story(),
                blueprint=blueprint,
                channel=_Channel(),
                channel_voice=channel_voice,
            )

        self.assertIn("voice_script", result)
        joined_logs = " ".join(log_ctx.output)
        self.assertIn("narrative completeness issue", joined_logs.lower())
        self.assertIn("telemetry only, no retry", joined_logs.lower())


class TestShortScriptGenerationNeverRetries(unittest.TestCase):
    """D1.3: _generate_short_script() must call Claude exactly once per Short,
    even when the draft violates the word cap AND overlaps heavily with the
    parent script — both are logged as telemetry, neither triggers a retry.
    The AI Short Quality Gate must no longer exist at all."""

    def test_over_cap_and_overlapping_draft_still_returns_on_first_call(self):
        parent_script = (
            "Sam Walker moved to Drisking Missouri as a kid and grew up hearing "
            "a grinding sound through the mountain every single night without fail. " * 3
        )
        # Over the 270-word cap AND repeats a 6+ word run verbatim from the
        # parent (and comfortably above the 190-word floor, so the one
        # operator-approved word-floor regeneration never triggers here —
        # over-cap remains strictly telemetry, never a retry).
        over_cap_overlapping = (
            "Sam Walker moved to Drisking Missouri as a kid and grew up hearing "
            "a grinding sound through the mountain every single night without fail. "
            + ("More filler narration continues here. " * 52)
        )
        calls = {"n": 0}

        def fake_structured(**kwargs):
            calls["n"] += 1
            return {"title": "Part 1", "voice_script": over_cap_overlapping}

        channel_voice = SimpleNamespace(tts_model="sonic-2", provider="cartesia")

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured), \
             self.assertLogs("app.agents.agent2_discovery.services.scripts", level="WARNING") as log_ctx:
            result = scripts._generate_short_script(
                part_plan={"part": 1, "_total_parts": 4},
                part_n=1,
                voice_script=parent_script,
                blueprint={"hook": "x", "major_turns": ["a"], "final_payoff": "y"},
                channel=_Channel(),
                channel_voice=channel_voice,
                source_language="en",
            )

        self.assertEqual(calls["n"], 1, "must call Claude exactly once — no correction round")
        self.assertIsNotNone(result)
        joined_logs = " ".join(log_ctx.output)
        self.assertIn("telemetry only, no retry", joined_logs)

    def test_ai_short_quality_gate_fully_removed(self):
        """The AI Short Quality Gate (assess_short_script_quality, task=
        short_quality_check) must not exist anywhere — deleted, not disabled."""
        self.assertFalse(hasattr(system_prompt, "assess_short_script_quality"))
        self.assertFalse(hasattr(system_prompt, "_SHORT_QUALITY_SYSTEM_PROMPT"))
        self.assertFalse(hasattr(system_prompt, "_SHORT_QUALITY_SCHEMA"))
        self.assertFalse(hasattr(scripts, "_run_short_quality_gate"))
        self.assertFalse(hasattr(scripts, "_generate_validated_short_script"))
        self.assertFalse(hasattr(scripts, "_MAX_SECTION_RETRIES"))
        self.assertFalse(hasattr(scripts, "_run_narrative_completeness_retry"))

    def test_generation_error_returns_none_without_retry(self):
        calls = {"n": 0}

        def fake_structured(**kwargs):
            calls["n"] += 1
            raise RuntimeError("simulated API failure")

        channel_voice = SimpleNamespace(tts_model="sonic-2", provider="cartesia")

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            result = scripts._generate_short_script(
                part_plan={"part": 1, "_total_parts": 1},
                part_n=1,
                voice_script="parent text here",
                blueprint={"hook": "x", "major_turns": ["a"], "final_payoff": "y"},
                channel=_Channel(),
                channel_voice=channel_voice,
                source_language="en",
            )

        self.assertEqual(calls["n"], 1)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
