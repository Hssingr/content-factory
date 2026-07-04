"""Runtime proof for spoken-style delivery rules + midpoint_retention_trap
(roadmap 4.3 / audit S-3, §6).

The operator's standing feedback is that scripts read like books, not like
YouTube/social video, and the prompts corroborated it: _SECTION_GENERATION_
SYSTEM_PROMPT opened "You are a YouTube documentary scriptwriter...". This
proves:

  1. The section + Short prompts now instruct present tense, direct address,
     contractions, and a read-aloud test.
  2. The blueprint schema/prompt now requires midpoint_retention_trap.
  3. The value actually flows through multiple functions —
     _build_section_generation_context() computes an estimated midpoint body
     index from the blueprint's suggested_section_count, and the real
     _run_body_section_loop() injects the blueprint's midpoint_retention_trap
     into exactly that one section's Claude call (and no other) — with only
     the paid call_claude_structured boundary stubbed, never internal logic.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent2_discovery import system_prompt
from app.agents.agent2_discovery.services import scripts
from app.agents.agent2_discovery.services.story import Story


def _flatten(text: str) -> str:
    """Collapse whitespace/newlines so a phrase can be found regardless of
    where the prompt's own line-wrapping happens to fall."""
    return " ".join(text.split())


class TestSpokenStylePromptRules(unittest.TestCase):
    def test_section_prompt_has_all_four_delivery_rules(self):
        prompt = _flatten(system_prompt._SECTION_GENERATION_SYSTEM_PROMPT.lower())
        self.assertIn("present tense", prompt)
        self.assertIn('"you"', prompt)
        self.assertIn("contraction", prompt)
        self.assertIn("read-aloud test", prompt)

    def test_short_prompt_has_all_four_delivery_rules(self):
        prompt = _flatten(system_prompt._SHORT_EPISODE_SYSTEM_PROMPT.lower())
        self.assertIn("present tense", prompt)
        self.assertIn('"you"', prompt)
        self.assertIn("contraction", prompt)
        self.assertIn("read-aloud test", prompt)

    def test_prompt_version_bumped(self):
        self.assertEqual(system_prompt.PROMPT_VERSION, "4.4")


class TestBlueprintSchemaRequiresMidpointTrap(unittest.TestCase):
    def test_schema_requires_midpoint_retention_trap(self):
        self.assertIn("midpoint_retention_trap", system_prompt._STORY_BLUEPRINT_SCHEMA["required"])
        self.assertIn(
            "midpoint_retention_trap", system_prompt._STORY_BLUEPRINT_SCHEMA["properties"]
        )

    def test_blueprint_prompt_describes_the_field(self):
        self.assertIn("midpoint_retention_trap", system_prompt._STORY_BLUEPRINT_SYSTEM_PROMPT)
        self.assertIn("halfway", system_prompt._STORY_BLUEPRINT_SYSTEM_PROMPT.lower())


class TestGenerateStoryBlueprintPropagatesTrap(unittest.TestCase):
    """Runtime proof: generate_story_blueprint() (real function) forwards the
    trap field through untouched. Only call_claude_structured is stubbed."""

    def test_midpoint_retention_trap_survives_the_real_function(self):
        story = Story(
            url="https://reddit.com/r/nosleep/comments/abc/x",
            title="A porch light story",
            body="The witness said the porch light flickered twice.",
            language="en",
            source_type="web",
            source_value="claude_web_search",
        )
        channel = SimpleNamespace(niche="horror", tone="tense")
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return {
                "hook": "h", "central_question": "q",
                "major_turns": ["turn one", "turn two"],
                "final_payoff": "p", "comment_trigger": "c?",
                "midpoint_retention_trap": "the porch light was never a light at all",
                "suggested_section_count": 3, "suggested_title": "t",
            }

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            result = system_prompt.generate_story_blueprint(story, channel)

        self.assertEqual(
            result["midpoint_retention_trap"], "the porch light was never a light at all"
        )
        self.assertIn("midpoint_retention_trap", captured["input_schema"]["required"])


class TestGenerateSectionInjectsMidpointConstraint(unittest.TestCase):
    """Runtime proof for generate_section() (real function): the MUST-deliver
    clause appears only when midpoint_retention_trap is passed."""

    def _story(self) -> Story:
        return Story(
            url="https://reddit.com/r/nosleep/comments/abc/x",
            title="T", body="body text", language="en",
            source_type="web", source_value="claude_web_search",
        )

    def _section_response(self) -> dict:
        return {
            "script_text": "Something happens here.", "summary": "s", "reveals": [],
            "open_questions": [], "suggests_outro": False,
            "visual_intent": {"section_goal": "g", "primary_visual_focus": "f", "avoid_repeating": []},
        }

    def test_trap_clause_present_when_passed(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return self._section_response()

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            system_prompt.generate_section(
                label="SECTION 2",
                story=self._story(),
                blueprint={"hook": "h"},
                prior_sections_summary=[],
                visual_intent_accumulator={"avoid_repeating": []},
                channel=SimpleNamespace(niche="horror", tone="tense"),
                midpoint_retention_trap="the porch light was never a light at all",
            )

        self.assertIn("MUST deliver", captured["user_message"])
        self.assertIn("the porch light was never a light at all", captured["user_message"])

    def test_trap_clause_absent_when_not_midpoint(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return self._section_response()

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            system_prompt.generate_section(
                label="SECTION 1",
                story=self._story(),
                blueprint={"hook": "h"},
                prior_sections_summary=[],
                visual_intent_accumulator={"avoid_repeating": []},
                channel=SimpleNamespace(niche="horror", tone="tense"),
                midpoint_retention_trap=None,
            )

        self.assertNotIn("MUST deliver", captured["user_message"])


class TestMidpointBodyIndexComputation(unittest.TestCase):
    def _context(self, suggested_section_count: int) -> dict:
        blueprint = {
            "major_turns": ["t1", "t2"],
            "suggested_section_count": suggested_section_count,
        }
        return scripts._build_section_generation_context(None, blueprint)

    def test_midpoint_for_two_sections_is_first(self):
        self.assertEqual(self._context(2)["midpoint_body_index"], 1)

    def test_midpoint_for_three_sections_is_second(self):
        self.assertEqual(self._context(3)["midpoint_body_index"], 2)

    def test_midpoint_for_four_sections_is_second(self):
        self.assertEqual(self._context(4)["midpoint_body_index"], 2)

    def test_midpoint_for_five_sections_is_third(self):
        self.assertEqual(self._context(5)["midpoint_body_index"], 3)


class TestRunBodySectionLoopInjectsTrapOnlyAtMidpoint(unittest.TestCase):
    """Full-chain runtime proof: drives the real _run_body_section_loop()
    (which calls the real _generate_section_with_retry() ->
    _call_section_generation() -> generate_section()) with only
    call_claude_structured stubbed. Confirms the blueprint's
    midpoint_retention_trap reaches exactly the section at the computed
    midpoint_body_index and no other."""

    def test_only_the_midpoint_section_receives_the_trap_clause(self):
        major_turns = ["turn one is revealed", "turn two is revealed", "turn three is revealed"]
        trap_text = "the porch light was never a light at all"
        blueprint = {
            "major_turns": major_turns,
            "suggested_section_count": 3,
            "midpoint_retention_trap": trap_text,
        }
        context = scripts._build_section_generation_context(None, blueprint)
        self.assertEqual(context["midpoint_body_index"], 2)

        story = Story(
            url="https://reddit.com/r/nosleep/comments/abc/x",
            title="T", body="body text", language="en",
            source_type="web", source_value="claude_web_search",
        )
        channel = SimpleNamespace(niche="horror", tone="tense")
        state = scripts._create_section_loop_state()

        call_user_messages: list[str] = []

        def fake_structured(**kwargs):
            call_user_messages.append(kwargs["user_message"])
            call_index = len(call_user_messages)
            return {
                "script_text": f"Section {call_index} narration text here.",
                "summary": f"summary {call_index}",
                "reveals": [major_turns[call_index - 1]],
                "open_questions": [],
                "suggests_outro": call_index >= len(major_turns),
                "visual_intent": {
                    "section_goal": "g", "primary_visual_focus": "f", "avoid_repeating": [],
                },
            }

        with patch.object(system_prompt, "call_claude_structured", side_effect=fake_structured):
            scripts._run_body_section_loop(
                story=story,
                blueprint=blueprint,
                channel=channel,
                script_format="youtube_long",
                audio_tags_enabled=False,
                context=context,
                state=state,
            )

        self.assertEqual(len(call_user_messages), 3)
        # Note: trap_text itself appears in every message via the passive
        # full-blueprint JSON dump (same as major_turns do for
        # primary_required_turn) — the real signal this test proves is the
        # explicit, targeted "MUST deliver...now" directive appearing only
        # once, on the computed midpoint section.
        must_deliver_hits = [
            i for i, message in enumerate(call_user_messages, start=1)
            if "MUST deliver" in message
        ]
        self.assertEqual(must_deliver_hits, [context["midpoint_body_index"]])
        midpoint_message = call_user_messages[context["midpoint_body_index"] - 1]
        self.assertIn(trap_text, midpoint_message)


if __name__ == "__main__":
    unittest.main()
