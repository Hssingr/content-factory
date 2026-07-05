"""Runtime proof that the four free-form-JSON call sites flagged in
CLAUDE.md section 7.1/22 (their own code comments read "Intentional
free-form JSON path: retained to avoid changing behavior in a rule-cleanup
pass") are now on the forced tool-use path (``call_claude_structured``),
per CLAUDE.md section 22: "Use call_claude_structured() for... any JSON
output used by code."

This is the deeper fix behind two real production failures traced to the
free-form call_claude()/parse_claude_json() path (see
tests/test_claude_call_empty_block_retry.py) — forced tool-use has no
content[0]-only assumption (scans every block for the named tool_use) and
no markdown-fence/control-character JSON parsing at all, since the SDK
delivers an already-parsed dict.

Only the paid Claude API boundary (``anthropic.Anthropic``) is stubbed — the
real ``generate_native_script()`` / ``generate_revised_scripts()`` /
``assess_script_quality()`` / ``generate_short_episode_script()`` functions
run unmodified, including their real system-prompt assembly.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agents.agent2_discovery import system_prompt


def _tool_use_block(schema_name, input_dict):
    b = MagicMock()
    b.type = "tool_use"
    b.name = schema_name
    b.input = input_dict
    return b


def _usage(input_tokens=10, output_tokens=10):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                            cache_read_input_tokens=0)


class _Channel:
    niche = "Reddit horror story narration"
    tone = "tense, grounded"


class TestNativeAdaptationUsesStructuredCall(unittest.TestCase):
    def test_returns_parsed_dict_via_tool_use_no_free_form_parsing(self):
        response = SimpleNamespace(
            content=[_tool_use_block("native_adaptation_output", {"voice_script": "Texte en français."})],
            usage=_usage(), stop_reason="tool_use",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=fake_client):
            result = system_prompt.generate_native_script(
                voice_script="English source script.",
                target_language="fr",
                niche="Reddit horror story narration",
                tone="tense",
            )

        self.assertEqual(result, {"voice_script": "Texte en français."})
        call_kwargs = fake_client.messages.create.call_args.kwargs
        self.assertIn("tools", call_kwargs)
        self.assertEqual(call_kwargs["tool_choice"], {"type": "tool", "name": "native_adaptation_output"})


class TestRevisionUsesStructuredCall(unittest.TestCase):
    def test_returns_parsed_dict_with_changes_array(self):
        payload = {
            "title": "Revised Title",
            "voice_script": "[INTRO] Revised text. [OUTRO] Done.",
            "changes": [{"section": "INTRO", "before_summary": "old", "after_summary": "new"}],
        }
        response = SimpleNamespace(
            content=[_tool_use_block("revision_output", payload)],
            usage=_usage(), stop_reason="tool_use",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=fake_client):
            result = system_prompt.generate_revised_scripts(
                current_scripts={"title": "Old Title", "voice_script": "[INTRO] Old text."},
                feedback="Make the intro punchier.",
                channel=_Channel(),
            )

        self.assertEqual(result, payload)


class TestScriptQualityUsesStructuredCall(unittest.TestCase):
    def test_passed_status_returns_empty_issues(self):
        response = SimpleNamespace(
            content=[_tool_use_block("script_quality_output", {"status": "PASSED", "issues": []})],
            usage=_usage(), stop_reason="tool_use",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=fake_client):
            result = system_prompt.assess_script_quality(
                scripts={"title": "T", "voice_script": "[INTRO] Text. [OUTRO] End."},
                channel=_Channel(),
            )

        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["issues"], [])

    def test_unexpected_status_value_still_raises(self):
        """The post-hoc status check must survive the migration unchanged —
        proves the enum in the schema doesn't silently swallow this guard."""
        response = SimpleNamespace(
            content=[_tool_use_block("script_quality_output", {"status": "MAYBE", "issues": []})],
            usage=_usage(), stop_reason="tool_use",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=fake_client):
            with self.assertRaises(ValueError) as ctx:
                system_prompt.assess_script_quality(
                    scripts={"title": "T", "voice_script": "text"}, channel=_Channel(),
                )
        self.assertIn("unexpected status", str(ctx.exception))


class TestShortEpisodeScriptUsesStructuredCall(unittest.TestCase):
    def test_reproduces_the_real_failure_scenario_now_fixed(self):
        """The exact real-run scenario (dark subject matter, short_script task)
        that previously produced an empty content[0] text block under the
        free-form path. Forced tool-use has no such failure mode: the tool
        input arrives as an already-parsed dict, never free text."""
        payload = {"title": "The Sheriff Signed Her File Himself", "voice_script": "Grim narration text."}
        response = SimpleNamespace(
            content=[_tool_use_block("short_episode_script_output", payload)],
            usage=_usage(output_tokens=600), stop_reason="tool_use",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=fake_client):
            result = system_prompt.generate_short_episode_script(
                part_plan={"part": 3, "_total_parts": 4},
                long_voice_script="Long form parent script text.",
                blueprint={"hook": "x", "major_turns": ["a"], "final_payoff": "y"},
                channel=_Channel(),
                channel_voice=None,
            )

        self.assertEqual(result, payload)
        call_kwargs = fake_client.messages.create.call_args.kwargs
        self.assertEqual(
            call_kwargs["tool_choice"], {"type": "tool", "name": "short_episode_script_output"}
        )


if __name__ == "__main__":
    unittest.main()
