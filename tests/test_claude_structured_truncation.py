"""Runtime proof for a real Agent 1 production incident: `research_channel_ideas()`
(app/agents/agent1_setup/system_prompt.py, task="channel_research") returned a
200 response whose `primary_recommendation` was missing 15 of its 18 required
fields, only discovered downstream by FastAPI's own `ResponseValidationError` —
`call_claude_structured()` had accepted a truncated (`stop_reason="max_tokens"`)
tool_use block as a normal successful result.

Root cause: Anthropic still returns a syntactically-valid tool_use `input`
dict when generation is cut off mid-object — it is just missing whatever
properties weren't generated yet. `call_claude_structured_with_usage()`
(app/services/claude_client.py) now checks `response.stop_reason` and raises
loudly instead of silently returning that partial dict.

Only the paid Claude API boundary (`anthropic.Anthropic`, via `_get_client()`)
is stubbed — the real `call_claude_structured()` chain runs unmodified.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import claude_client

_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    "required": ["a", "b"],
}


def _usage(input_tokens=100, output_tokens=4096, cache_read=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
    )


def _tool_use_block(name, input_dict):
    b = MagicMock()
    b.type = "tool_use"
    b.name = name
    b.input = input_dict
    return b


class TruncatedStructuredResponseTest(unittest.TestCase):
    def test_max_tokens_stop_reason_raises_instead_of_returning_partial_input(self) -> None:
        """Reproduces the real incident's shape: a tool_use block is present
        and looks superficially fine, but the generation was cut off."""
        response = SimpleNamespace(
            content=[_tool_use_block("channel_research_ideas", {"a": "only this survived"})],
            usage=_usage(output_tokens=4096),
            stop_reason="max_tokens",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch.object(claude_client, "_get_client", return_value=fake_client):
            with self.assertRaises(ValueError) as ctx:
                claude_client.call_claude_structured(
                    task="channel_research",
                    system_prompt="system",
                    user_message="user",
                    schema_name="channel_research_ideas",
                    input_schema=_SCHEMA,
                    max_tokens=4096,
                )

        message = str(ctx.exception)
        self.assertIn("truncated", message)
        self.assertIn("channel_research", message)
        self.assertIn("channel_research_ideas", message)

    def test_completed_tool_use_is_unaffected(self) -> None:
        """The overwhelmingly common case — a normal, complete response —
        must return exactly as before this fix, no regression."""
        response = SimpleNamespace(
            content=[_tool_use_block("channel_research_ideas", {"a": "x", "b": "y"})],
            usage=_usage(output_tokens=500),
            stop_reason="tool_use",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch.object(claude_client, "_get_client", return_value=fake_client):
            result = claude_client.call_claude_structured(
                task="channel_research",
                system_prompt="system",
                user_message="user",
                schema_name="channel_research_ideas",
                input_schema=_SCHEMA,
            )

        self.assertEqual(result, {"a": "x", "b": "y"})

    def test_end_turn_stop_reason_is_also_unaffected(self) -> None:
        """Only the literal string "max_tokens" is treated as truncation —
        any other stop_reason alongside a real tool_use block still works."""
        response = SimpleNamespace(
            content=[_tool_use_block("channel_research_ideas", {"a": "x", "b": "y"})],
            usage=_usage(output_tokens=200),
            stop_reason="end_turn",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch.object(claude_client, "_get_client", return_value=fake_client):
            result = claude_client.call_claude_structured(
                task="channel_research",
                system_prompt="system",
                user_message="user",
                schema_name="channel_research_ideas",
                input_schema=_SCHEMA,
            )

        self.assertEqual(result, {"a": "x", "b": "y"})

    def test_call_claude_structured_with_usage_still_returns_usage_on_success(self) -> None:
        response = SimpleNamespace(
            content=[_tool_use_block("channel_research_ideas", {"a": "x", "b": "y"})],
            usage=_usage(input_tokens=321, output_tokens=123),
            stop_reason="tool_use",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch.object(claude_client, "_get_client", return_value=fake_client):
            result, usage = claude_client.call_claude_structured_with_usage(
                task="channel_research",
                system_prompt="system",
                user_message="user",
                schema_name="channel_research_ideas",
                input_schema=_SCHEMA,
            )

        self.assertEqual(result, {"a": "x", "b": "y"})
        self.assertEqual(usage["input_tokens"], 321)
        self.assertEqual(usage["output_tokens"], 123)


if __name__ == "__main__":
    unittest.main()
