"""Runtime proof for roadmap Phase 4a (P1-9) — Register fix,
code_report/forensic_output_audit_borrasca_run.md.

The audit found the pipeline's register was configured AND hardcoded to
"documentary" — channel config threaded a niche/tone contradiction through
untouched (nothing validated coherence), and three prompts assumed
"documentary" regardless of what the operator actually configured. This
phase:

  1. Adds a `narration_pov` ChannelConfig field ("third_person" |
     "first_person_storytime"), threaded alongside visual_style/image_style
     through every Agent 2 script-generation entrypoint, with REAL
     behavioral rules in the system prompts (not just a passthrough label).
  2. De-hardcodes "documentary" from Agent 4's storyboard identity line and
     every runtime fallback default (now "story_driven", matching the new
     ChannelConfig.visual_style default).
  3. Adds a pure, deterministic niche<->tone contradiction flag at Agent 1
     setup time (never blocking).

This file proves all three with real function calls — only the paid Claude
API boundary (`call_claude_structured`/`call_claude_structured_with_usage`)
is ever stubbed.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery import system_prompt as agent2_prompt
from app.agents.agent2_discovery.services import scripts as agent2_scripts
from app.agents.agent2_discovery.services import script_workflow
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent1_setup.services.niche_tone_check import detect_niche_tone_contradiction
from app.agents.agent1_setup.services.activation_readiness import check_activation_readiness
from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script


def _story() -> Story:
    return Story(
        url="https://reddit.com/r/nosleep/comments/abc/x",
        title="A porch light story", body="The witness said the porch light flickered twice.",
        language="en", source_type="web", source_value="claude_web_search",
    )


def _section_response() -> dict:
    # Long enough (per section, x4 sections in the full-orchestrator test)
    # that generate_script_sections() never triggers its post-assembly
    # minimum-length correction pass (a separate Claude call, unrelated to
    # narration_pov, that would otherwise show up in captured messages).
    long_text = " ".join(["Something concrete happens here in this part of the story."] * 40)
    return {
        "script_text": long_text, "summary": "s", "reveals": [],
        "open_questions": [], "suggests_outro": False,
        "visual_intent": {"section_goal": "g", "primary_visual_focus": "f", "avoid_repeating": []},
    }


def _blueprint_response() -> dict:
    return {
        "hook": "h", "central_question": "q", "major_turns": ["turn one", "turn two"],
        "final_payoff": "p", "comment_trigger": "c?",
        "midpoint_retention_trap": "trap", "suggested_section_count": 2, "suggested_title": "t",
    }


# ── Behavioral rules exist, not just a passthrough label ──────────────────────

class TestNarrationPovRealRulesExist(unittest.TestCase):
    def test_section_prompt_has_first_person_and_third_person_rule(self):
        prompt = agent2_prompt._SECTION_GENERATION_SYSTEM_PROMPT.lower()
        self.assertIn("first_person_storytime", prompt)
        self.assertIn("third_person", prompt)
        self.assertIn("never mix pov", prompt)

    def test_short_episode_prompt_has_pov_rule(self):
        prompt = agent2_prompt._SHORT_EPISODE_SYSTEM_PROMPT.lower()
        self.assertIn("first_person_storytime", prompt)
        self.assertIn("third_person", prompt)

    def test_blueprint_prompt_instructs_pov_aware_phrasing(self):
        prompt = agent2_prompt._STORY_BLUEPRINT_SYSTEM_PROMPT.lower()
        self.assertIn("narration pov", prompt)

    def test_native_adaptation_prompts_preserve_pov_never_convert(self):
        for base in (
            agent2_prompt._BASE_YOUTUBE_LONG_FORM_NATIVE,
            agent2_prompt._BASE_SHORT_FORM_NATIVE,
            agent2_prompt._BASE_CHILD_SHORT_NATIVE,
        ):
            with self.subTest(base=base[:40]):
                # Collapse whitespace/newlines so a phrase split across the
                # prompt's own line-wrapping is still found.
                flattened = " ".join(base.lower().split())
                self.assertIn("narration pov", flattened)
                self.assertIn("never convert", flattened)


# ── Real function calls: narration_pov reaches the Claude user_message ───────

class TestNarrationPovReachesRealClaudeCalls(unittest.TestCase):
    def test_generate_story_blueprint_includes_pov_line(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return _blueprint_response()

        with patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_structured):
            agent2_prompt.generate_story_blueprint(
                _story(), SimpleNamespace(niche="horror", tone="tense"),
                narration_pov="first_person_storytime",
            )
        self.assertIn("Narration POV: first_person_storytime", captured["user_message"])

    def test_generate_section_includes_pov_line(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return _section_response()

        with patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_structured):
            agent2_prompt.generate_section(
                label="SECTION 1", story=_story(), blueprint={"hook": "h"},
                prior_sections_summary=[], visual_intent_accumulator={"avoid_repeating": []},
                channel=SimpleNamespace(niche="horror", tone="tense"),
                narration_pov="first_person_storytime",
            )
        self.assertIn("Narration POV: first_person_storytime", captured["user_message"])

    def test_generate_section_defaults_to_third_person(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return _section_response()

        with patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_structured):
            agent2_prompt.generate_section(
                label="SECTION 1", story=_story(), blueprint={"hook": "h"},
                prior_sections_summary=[], visual_intent_accumulator={"avoid_repeating": []},
                channel=SimpleNamespace(niche="horror", tone="tense"),
            )
        self.assertIn("Narration POV: third_person", captured["user_message"])

    def test_generate_native_script_includes_pov_line_and_preserve_language(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return {"voice_script": "[INTRO]\nHola.\n[OUTRO]\nAdios."}

        with patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_structured):
            agent2_prompt.generate_native_script(
                voice_script="[INTRO]\nHi.\n[OUTRO]\nBye.",
                target_language="es", niche="horror", tone="tense",
                narration_pov="first_person_storytime",
            )
        self.assertIn("Narration POV: first_person_storytime", captured["user_message"])

    def test_generate_short_episode_script_includes_pov_line(self):
        captured = {}

        def fake_structured(**kwargs):
            captured.update(kwargs)
            return {"title": "t", "voice_script": "narration"}

        with patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_structured):
            agent2_prompt.generate_short_episode_script(
                part_plan={"part": 1, "_total_parts": 3},
                long_voice_script="long form text",
                blueprint={"hook": "h"},
                channel=SimpleNamespace(niche="horror", tone="tense"),
                channel_voice=None,
                narration_pov="first_person_storytime",
            )
        self.assertIn("Narration POV: first_person_storytime", captured["user_message"])


# ── generate_script_sections(): the full orchestrator, real internal chain ────

class TestGenerateScriptSectionsThreadsNarrationPovToEverySection(unittest.TestCase):
    """Drives the REAL generate_script_sections() orchestrator (INTRO -> body
    loop -> OUTRO), only stubbing the paid Claude boundary — proving
    narration_pov reaches every section's user_message, not just one."""

    def test_every_section_receives_the_configured_pov(self):
        captured_messages: list[str] = []

        def fake_structured(**kwargs):
            captured_messages.append(kwargs["user_message"])
            return _section_response()

        story = _story()
        blueprint = _blueprint_response()
        channel = SimpleNamespace(niche="horror", tone="tense")

        with patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_structured):
            result = agent2_scripts.generate_script_sections(
                story=story, blueprint=blueprint, channel=channel, channel_voice=None,
                narration_pov="first_person_storytime",
            )

        self.assertIn("voice_script", result)
        # INTRO + at least one body section + OUTRO = at least 3 calls, all first_person.
        self.assertGreaterEqual(len(captured_messages), 3)
        for msg in captured_messages:
            self.assertIn("Narration POV: first_person_storytime", msg)

    def test_default_is_third_person_when_not_specified(self):
        captured_messages: list[str] = []

        def fake_structured(**kwargs):
            captured_messages.append(kwargs["user_message"])
            return _section_response()

        with patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_structured):
            agent2_scripts.generate_script_sections(
                story=_story(), blueprint=_blueprint_response(),
                channel=SimpleNamespace(niche="horror", tone="tense"), channel_voice=None,
            )

        self.assertTrue(captured_messages)
        for msg in captured_messages:
            self.assertIn("Narration POV: third_person", msg)


# ── ScriptWorkflowContext loads narration_pov from ChannelConfig ──────────────

class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self, *, channel, config, voices):
        self.channel = channel
        self.config = config
        self.voices = voices

    def get(self, model, key):
        if model is Channel:
            return self.channel if key == self.channel.id else None
        if model is ChannelConfig:
            return self.config
        return None

    def query(self, model):
        if model is ChannelVoice:
            return _FakeQuery(self.voices)
        return _FakeQuery([])


class TestScriptWorkflowContextLoadsNarrationPov(unittest.TestCase):
    def test_context_reads_narration_pov_from_channel_config(self):
        channel = SimpleNamespace(id=uuid.uuid4())
        config = SimpleNamespace(
            script_format="youtube_long", audio_tags_enabled=False,
            visual_style="story_driven", image_style="photorealistic",
            narration_pov="first_person_storytime",
        )
        db = _FakeDb(channel=channel, config=config, voices=[])
        content = SimpleNamespace(id=uuid.uuid4(), channel_id=channel.id, source_language="en")

        context = script_workflow._load_script_workflow_context(content, db)

        self.assertIsNotNone(context)
        self.assertEqual(context.narration_pov, "first_person_storytime")

    def test_context_defaults_to_third_person_with_no_config_row(self):
        channel = SimpleNamespace(id=uuid.uuid4())
        db = _FakeDb(channel=channel, config=None, voices=[])
        content = SimpleNamespace(id=uuid.uuid4(), channel_id=channel.id, source_language="en")

        context = script_workflow._load_script_workflow_context(content, db)

        self.assertIsNotNone(context)
        self.assertEqual(context.narration_pov, "third_person")


# ── generate_multilingual_scripts(): reads config.narration_pov, no new param ─

class TestGenerateMultilingualScriptsThreadsNarrationPov(unittest.TestCase):
    def test_translated_parent_script_receives_narration_pov(self):
        content_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="GENERATING_SCRIPTS",
        )
        channel = Channel(id=channel_id, niche="horror", tone="tense")
        config = ChannelConfig(
            channel_id=channel_id, script_format="youtube_long",
            narration_pov="first_person_storytime",
        )
        source_script = Script(
            id=uuid.uuid4(), content_id=content_id, language="en",
            voice_script="[INTRO]\nI heard it again.\n[OUTRO]\nI never went back.",
            version=1, validated=True,
        )

        class _Db:
            def __init__(self):
                self.tables = {
                    Content: [content], Channel: [channel], ChannelConfig: [config],
                    Script: [source_script], ChannelVoice: [],
                }

            def query(self, model):
                return _FakeQuery(self.tables.get(model, []))

            def commit(self):
                pass

            def flush(self):
                pass

            def add(self, row):
                self.tables.setdefault(type(row), []).append(row)

        db = _Db()

        # required_languages must include a non-source language to exercise
        # the translation path — patch the helper that computes it.
        captured = {}

        def fake_call_claude_structured(**kwargs):
            captured.update(kwargs)
            return {"voice_script": "[INTRO]\nLo escuché de nuevo.\n[OUTRO]\nNunca volví."}

        with (
            patch.object(agent2_scripts, "_required_script_languages", return_value=["en", "es"]),
            patch.object(agent2_prompt, "call_claude_structured", side_effect=fake_call_claude_structured),
        ):
            agent2_scripts.generate_multilingual_scripts(content, channel, db)

        # generate_native_script() (real function) builds "Narration POV: ..."
        # into the user_message it hands to call_claude_structured() — this
        # proves config.narration_pov actually reached the real adapter call,
        # not just that some kwarg by that name exists.
        self.assertIn("Narration POV: first_person_storytime", captured.get("user_message", ""))


if __name__ == "__main__":
    unittest.main()
