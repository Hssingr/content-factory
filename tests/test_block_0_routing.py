"""Block 0 — model routing tests.

Covers:
- resolve_model() returns configured primary/secondary model per task
- channel_suggestion uses the primary model (onboarding quality)
- story_research uses the primary model (web_search tool requires the primary model by default)
- model_override bypasses routing
- Unknown task raises ValueError
- primary_model and secondary_model settings control concrete routed model IDs
- call_claude_structured parses tool_use response (mocked client)
- call_claude passes task+model to the underlying API (mocked client)
- cache_control applied on long prompts, not on short ones
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import anthropic

class TestResolveModel(unittest.TestCase):
    """Unit tests for resolve_model() in app.services.model_routing."""

    def _resolve(self, task: str, model_override: str | None = None) -> str:
        from app.services.model_routing import resolve_model
        return resolve_model(task, model_override)

    def test_primary_tasks_return_configured_primary_model(self):
        primary = "claude-custom-primary"
        with patch("app.config.settings") as mock_settings:
            mock_settings.primary_model = primary
            mock_settings.secondary_model = "claude-custom-secondary"
            mock_settings.claude_tier = "prod"
            for task in (
                "script_generation", "native_adaptation", "quality_rewrite",
                "auto_correction", "storyboard", "visual_bible_generation",
                "story_gate_scoring", "revision", "story_research",
                "channel_suggestion", "channel_research", "story_blueprint",
                "section_generation", "short_script",
            ):
                with self.subTest(task=task):
                    self.assertEqual(self._resolve(task), primary)

    def test_secondary_tasks_return_configured_secondary_model(self):
        secondary = "claude-custom-secondary"
        with patch("app.config.settings") as mock_settings:
            mock_settings.primary_model = "claude-custom-primary"
            mock_settings.secondary_model = secondary
            mock_settings.claude_tier = "prod"
            for task in (
                "script_quality_check", "short_quality_check",
                "content_reformat",
                "global_validation", "shorts_planner",
                "short_storyboard_remap",
            ):
                with self.subTest(task=task):
                    self.assertEqual(self._resolve(task), secondary)

    def test_model_routing_table_contains_slots_not_model_ids(self):
        from app.services.model_routing import (
            DEFAULT_PRIMARY_MODEL,
            DEFAULT_SECONDARY_MODEL,
            MODEL_ROUTING,
            PRIMARY_MODEL,
            SECONDARY_MODEL,
        )
        valid_slots = {PRIMARY_MODEL, SECONDARY_MODEL}
        for task, slot in MODEL_ROUTING.items():
            with self.subTest(task=task):
                self.assertIn(slot, valid_slots)
                self.assertNotIn(slot, {DEFAULT_PRIMARY_MODEL, DEFAULT_SECONDARY_MODEL})

    def test_primary_and_secondary_models_load_from_environment(self):
        from app.config import Settings
        with patch.dict(os.environ, {
            "primary_model": "claude-env-primary",
            "secondary_model": "claude-env-secondary",
        }, clear=False):
            loaded = Settings(_env_file=None)

        self.assertEqual(loaded.primary_model, "claude-env-primary")
        self.assertEqual(loaded.secondary_model, "claude-env-secondary")

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

    def test_dev_tier_uses_secondary_except_hard_primary_exceptions(self):
        with patch("app.config.settings") as mock_settings:
            mock_settings.primary_model = "claude-custom-primary"
            mock_settings.secondary_model = "claude-custom-secondary"
            mock_settings.claude_tier = "dev"

            self.assertEqual(self._resolve("storyboard"), "claude-custom-secondary")
            self.assertEqual(self._resolve("content_reformat"), "claude-custom-secondary")
            self.assertEqual(self._resolve("story_research"), "claude-custom-primary")
            self.assertEqual(self._resolve("auto_correction"), "claude-custom-primary")

    def test_default_primary_and_secondary_model_ids(self):
        from app.services.model_routing import DEFAULT_PRIMARY_MODEL, DEFAULT_SECONDARY_MODEL
        self.assertEqual(DEFAULT_PRIMARY_MODEL, "claude-sonnet-5")
        self.assertEqual(DEFAULT_SECONDARY_MODEL, "claude-sonnet-4-6")

    def test_config_defaults_upgrade_creative_tier_and_quality_gates_off_haiku(self):
        """Roadmap 4.2 / audit S-2 (exec-6): with no env override, the creative
        tier must resolve to the configured Sonnet 5-class primary model, and
        the quality-gate tasks (previously pinned to Haiku) must resolve to a
        Sonnet-class secondary model — never Haiku. Uses a real Settings()
        instance (env_file=None so a local .env cannot mask the code default)
        and the real resolve_model(), with only app.config.settings patched
        to that instance — no internal routing logic is stubbed."""
        from app.config import Settings
        from app.services.model_routing import resolve_model

        default_settings = Settings(_env_file=None)
        self.assertEqual(default_settings.primary_model, "claude-sonnet-5")
        self.assertEqual(default_settings.secondary_model, "claude-sonnet-4-6")

        with patch("app.config.settings", default_settings):
            creative_tier_tasks = (
                "script_generation", "native_adaptation", "quality_rewrite",
                "storyboard", "visual_bible_generation", "story_blueprint",
                "section_generation", "short_script",
            )
            for task in creative_tier_tasks:
                with self.subTest(task=task):
                    self.assertEqual(resolve_model(task), "claude-sonnet-5")

            quality_gate_tasks = ("script_quality_check", "short_quality_check")
            for task in quality_gate_tasks:
                with self.subTest(task=task):
                    model = resolve_model(task)
                    self.assertEqual(model, "claude-sonnet-4-6")
                    self.assertNotIn("haiku", model.lower())


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
                task="content_reformat",
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
                task="global_validation",
            )

        mock_resolve.assert_called_once_with("global_validation", None)
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
