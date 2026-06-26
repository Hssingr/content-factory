"""Block 0 — model routing tests.

Covers:
- resolve_model() returns correct model per task (Sonnet vs Haiku)
- channel_suggestion is Sonnet (onboarding quality)
- story_research is Sonnet (web_search tool not available on Haiku)
- model_override bypasses routing
- Unknown task raises ValueError
- Dev and prod resolve every task identically (no env-based override)
- call_claude_structured parses tool_use response (mocked client)
- call_claude passes task+model to the underlying API (mocked client)
- cache_control applied on long prompts, not on short ones
"""

import unittest
from unittest.mock import MagicMock, patch

import anthropic


class TestResolveModel(unittest.TestCase):
    """Unit tests for resolve_model() in app.services.model_routing."""

    def _resolve(self, task: str, model_override: str | None = None) -> str:
        from app.services.model_routing import resolve_model
        return resolve_model(task, model_override)

    def test_sonnet_tasks_return_sonnet(self):
        from app.services.model_routing import SONNET
        for task in (
            "script_generation", "native_adaptation", "quality_rewrite",
            "intro_optimization", "auto_correction", "storyboard",
            "story_scoring", "revision", "story_research",
            # These two are Sonnet by explicit design decision:
            "channel_suggestion",   # Agent 1 — bad suggestions harm onboarding
        ):
            with self.subTest(task=task):
                self.assertEqual(self._resolve(task), SONNET)

    def test_haiku_tasks_return_haiku(self):
        from app.services.model_routing import HAIKU
        for task in (
            "script_quality_check", "script_validation", "media_scoring",
            "telegram_summary",
            "content_reformat", "section_splitting",
            "visual_reinterpretation",
        ):
            with self.subTest(task=task):
                self.assertEqual(self._resolve(task), HAIKU)

    def test_unknown_task_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._resolve("totally_unknown_task_xyz")
        self.assertIn("totally_unknown_task_xyz", str(ctx.exception))
        self.assertIn("MODEL_ROUTING", str(ctx.exception))

    def test_model_override_bypasses_routing(self):
        self.assertEqual(
            self._resolve("storyboard", model_override="claude-opus-4-8"),
            "claude-opus-4-8",
        )

    def test_override_bypasses_routing_for_haiku_task_too(self):
        self.assertEqual(
            self._resolve("media_scoring", model_override="claude-opus-4-8"),
            "claude-opus-4-8",
        )

    def test_dev_prod_parity(self):
        """Dev and prod must resolve every task to the same model.

        There must be no environment-based model override — what runs in dev
        is exactly what runs in prod. Concretely: resolve_model() must not
        read any environment variable or settings value to alter its output.
        """
        from app.services.model_routing import MODEL_ROUTING

        for tier_label in ("dev", "prod", "staging", ""):
            with patch("app.config.settings") as mock_settings:
                mock_settings.claude_tier = tier_label
                for task, expected_model in MODEL_ROUTING.items():
                    with self.subTest(tier=tier_label, task=task):
                        result = self._resolve(task)
                        self.assertEqual(
                            result,
                            expected_model,
                            msg=(
                                f"Task '{task}' resolved to '{result}' under "
                                f"CLAUDE_TIER={tier_label!r}, expected '{expected_model}'. "
                                f"resolve_model() must not vary by environment."
                            ),
                        )

    def test_story_research_is_sonnet(self):
        """story_research must always be Sonnet — Haiku does not support web_search tool."""
        from app.services.model_routing import SONNET
        self.assertEqual(self._resolve("story_research"), SONNET)

    def test_channel_suggestion_is_sonnet(self):
        """channel_suggestion must be Sonnet — poor suggestions degrade Agent 1 onboarding."""
        from app.services.model_routing import SONNET
        self.assertEqual(self._resolve("channel_suggestion"), SONNET)

    def test_all_routing_table_entries_are_valid_model_ids(self):
        """Every value in MODEL_ROUTING must be a known Claude model string."""
        from app.services.model_routing import MODEL_ROUTING, SONNET, HAIKU
        valid = {SONNET, HAIKU}
        for task, model in MODEL_ROUTING.items():
            with self.subTest(task=task):
                self.assertIn(
                    model, valid,
                    msg=f"Task '{task}' maps to unrecognised model '{model}'"
                )


class TestCallClaudeStructured(unittest.TestCase):
    """Unit tests for call_claude_structured() with a mocked Anthropic client."""

    def _make_tool_use_response(self, schema_name: str, data: dict) -> MagicMock:
        """Build a mock Anthropic messages.create() response containing a tool_use block."""
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = schema_name
        tool_block.input = data

        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 50
        usage.cache_read_input_tokens = 0

        response = MagicMock()
        response.content = [tool_block]
        response.stop_reason = "tool_use"
        response.usage = usage
        return response

    def _make_mock_client(self, response: MagicMock) -> MagicMock:
        client = MagicMock()
        client.messages.create.return_value = response
        return client

    def test_returns_parsed_input_dict(self):
        from app.services.claude_client import call_claude_structured

        expected = {"beats": [{"order": 0, "visual_intent": "test"}]}
        response = self._make_tool_use_response("storyboard_batch", expected)
        mock_client = self._make_mock_client(response)

        with patch("app.services.claude_client._get_client", return_value=mock_client), \
             patch("app.services.claude_client.resolve_model", return_value="claude-sonnet-4-6"):
            result = call_claude_structured(
                task="storyboard",
                system_prompt="You are a storyboard agent.",
                user_message="Generate beats.",
                schema_name="storyboard_batch",
                input_schema={"type": "object", "properties": {"beats": {"type": "array"}}},
                max_tokens=512,
            )
        self.assertEqual(result, expected)

    def test_raises_when_no_tool_use_block(self):
        """If Claude returns no tool_use block, raise ValueError."""
        from app.services.claude_client import call_claude_structured

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "I am unable to fill the schema."

        usage = MagicMock()
        usage.input_tokens = 50
        usage.output_tokens = 10
        usage.cache_read_input_tokens = 0

        response = MagicMock()
        response.content = [text_block]
        response.stop_reason = "end_turn"
        response.usage = usage

        mock_client = self._make_mock_client(response)

        with patch("app.services.claude_client._get_client", return_value=mock_client), \
             patch("app.services.claude_client.resolve_model", return_value="claude-haiku-4-5-20251001"):
            with self.assertRaises(ValueError) as ctx:
                call_claude_structured(
                    task="storyboard",
                    system_prompt="You are a storyboard agent.",
                    user_message="Generate beats.",
                    schema_name="storyboard_batch",
                    input_schema={"type": "object"},
                    max_tokens=512,
                )
        self.assertIn("storyboard_batch", str(ctx.exception))

    def test_tool_choice_sent_to_api(self):
        """Verify forced tool-use is set in the API call kwargs."""
        from app.services.claude_client import call_claude_structured

        expected = {"result": "ok"}
        response = self._make_tool_use_response("my_schema", expected)
        mock_client = self._make_mock_client(response)

        with patch("app.services.claude_client._get_client", return_value=mock_client), \
             patch("app.services.claude_client.resolve_model", return_value="claude-haiku-4-5-20251001"):
            call_claude_structured(
                task="media_scoring",
                system_prompt="Score candidates.",
                user_message="Here are candidates.",
                schema_name="my_schema",
                input_schema={"type": "object"},
                max_tokens=256,
            )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        self.assertEqual(call_kwargs["tool_choice"], {"type": "tool", "name": "my_schema"})
        tools = call_kwargs["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "my_schema")


class TestCallClaudeTaskRouting(unittest.TestCase):
    """Verify call_claude passes the task-resolved model to the underlying API."""

    def _make_text_response(self, text: str) -> MagicMock:
        block = MagicMock(spec=anthropic.types.TextBlock)
        block.type = "text"
        block.text = text

        usage = MagicMock()
        usage.input_tokens = 30
        usage.output_tokens = 10
        usage.cache_read_input_tokens = 0

        response = MagicMock()
        response.content = [block]
        response.usage = usage
        return response

    def test_model_forwarded_to_api_create(self):
        from app.services.claude_client import call_claude

        response = self._make_text_response('{"status": "PASSED", "issues": []}')
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=mock_client), \
             patch("app.services.claude_client.resolve_model", return_value="claude-haiku-4-5-20251001") as mock_resolve:
            result = call_claude(
                "You are a validator.",
                "Validate this.",
                max_tokens=256,
                task="script_validation",
            )

        mock_resolve.assert_called_once_with("script_validation", None)
        self.assertEqual(
            mock_client.messages.create.call_args.kwargs["model"],
            "claude-haiku-4-5-20251001",
        )
        self.assertEqual(result, '{"status": "PASSED", "issues": []}')

    def test_unknown_task_raises_before_api_call(self):
        from app.services.model_routing import resolve_model
        with self.assertRaises(ValueError):
            resolve_model("nonexistent_task_abc")

    def test_cache_control_applied_on_long_prompt(self):
        """System prompts >800 chars should be sent as a list with cache_control."""
        from app.services.claude_client import call_claude

        long_prompt = "x" * 801
        response = self._make_text_response("hello")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=mock_client), \
             patch("app.services.claude_client.resolve_model", return_value="claude-haiku-4-5-20251001"):
            call_claude(long_prompt, "hi", max_tokens=64, task="telegram_summary")

        system_arg = mock_client.messages.create.call_args.kwargs["system"]
        self.assertIsInstance(system_arg, list)
        self.assertEqual(system_arg[0]["cache_control"]["type"], "ephemeral")

    def test_short_prompt_not_wrapped(self):
        """System prompts ≤800 chars are passed as a plain string."""
        from app.services.claude_client import call_claude

        short_prompt = "Short prompt."
        response = self._make_text_response("hello")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response

        with patch("app.services.claude_client._get_client", return_value=mock_client), \
             patch("app.services.claude_client.resolve_model", return_value="claude-haiku-4-5-20251001"):
            call_claude(short_prompt, "hi", max_tokens=64, task="telegram_summary")

        system_arg = mock_client.messages.create.call_args.kwargs["system"]
        self.assertIsInstance(system_arg, str)


if __name__ == "__main__":
    unittest.main()
