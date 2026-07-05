"""Runtime proofs for two real production failures observed in the same
`--confirm` run's shorts-planner step:

1. ``call_claude()`` crashed with ``'NoneType' object has no attribute
   'strip'`` when ``response.content[0].text`` came back empty/None. The
   first fix (retry on an empty/null block) did not resolve the real issue:
   a follow-up real run reproduced the identical empty-text symptom on
   12/12 consecutive real API calls (4 short-script parts x 3 retries each),
   with real non-zero `output_tokens` each time — proving the failure was
   not transient. The root mechanical bug: ``_call_claude_core()`` only ever
   read ``response.content[0]``, exactly the assumption
   ``call_claude_with_tools()`` (same file) had already learned not to make
   for this model — it scans every block for the first usable ``type=="text"``
   entry instead of indexing ``[0]``. ``_call_claude_core()`` now does the
   same scan.
2. ``parse_claude_json()`` rejected an otherwise well-formed response because
   a multi-paragraph string value contained a literal newline instead of an
   escaped ``\\n`` (observed verbatim: ``Claude JSON parse error: Invalid
   control character at: line 1 column 186``).

Only the paid Claude API boundary (``anthropic.Anthropic``) is stubbed — the
real ``_call_claude_core()``/``call_claude()``/``parse_claude_json()`` chain
runs unmodified.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import claude_client


def _usage(input_tokens=10, output_tokens=10, cache_read=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
    )


def _block(block_type, text=None):
    b = MagicMock()
    b.type = block_type
    b.text = text
    return b


def _text_block(text):
    return _block("text", text)


class TestBlockScanningReplacesIndexZeroAssumption(unittest.TestCase):
    def test_real_failure_shape_leading_empty_text_block_then_real_text(self):
        """Reproduces the actual observed shape: content[0] is a text block
        with no usable text, but a later block carries the real answer —
        this must succeed on the FIRST attempt, no retry needed."""
        response = SimpleNamespace(
            content=[_text_block(""), _text_block("the real short script json")],
            usage=_usage(output_tokens=500),
            stop_reason="end_turn",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch.object(claude_client, "_get_client", return_value=fake_client):
            result = claude_client.call_claude("system", "user", task="short_script")

        self.assertEqual(result, "the real short script json")
        self.assertEqual(fake_client.messages.create.call_count, 1)

    def test_leading_nontext_block_then_real_text_succeeds_first_attempt(self):
        response = SimpleNamespace(
            content=[_block("thinking", text=None), _text_block("the real answer")],
            usage=_usage(output_tokens=500),
            stop_reason="end_turn",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch.object(claude_client, "_get_client", return_value=fake_client):
            result = claude_client.call_claude("system", "user", task="short_script")

        self.assertEqual(result, "the real answer")
        self.assertEqual(fake_client.messages.create.call_count, 1)

    def test_single_text_block_still_works_unchanged(self):
        """The overwhelmingly common case (one text block) must be unaffected."""
        response = SimpleNamespace(
            content=[_text_block("plain single-block answer")],
            usage=_usage(), stop_reason="end_turn",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response

        with patch.object(claude_client, "_get_client", return_value=fake_client):
            result = claude_client.call_claude("system", "user", task="short_script")

        self.assertEqual(result, "plain single-block answer")


class TestNoUsableTextRetriesThenGivesUpWithDiagnostics(unittest.TestCase):
    def test_none_text_block_is_retried_and_second_attempt_succeeds(self):
        bad_response = SimpleNamespace(content=[_text_block(None)], usage=_usage(), stop_reason="end_turn")
        good_response = SimpleNamespace(
            content=[_text_block("real content")], usage=_usage(), stop_reason="end_turn"
        )
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [bad_response, good_response]

        with patch.object(claude_client, "_get_client", return_value=fake_client), \
             patch.object(claude_client._time, "sleep"):
            result = claude_client.call_claude("system", "user", task="short_script")

        self.assertEqual(result, "real content")
        self.assertEqual(fake_client.messages.create.call_count, 2)

    def test_empty_content_list_is_retried_and_second_attempt_succeeds(self):
        bad_response = SimpleNamespace(content=[], usage=_usage(), stop_reason="end_turn")
        good_response = SimpleNamespace(
            content=[_text_block("real content")], usage=_usage(), stop_reason="end_turn"
        )
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [bad_response, good_response]

        with patch.object(claude_client, "_get_client", return_value=fake_client), \
             patch.object(claude_client._time, "sleep"):
            result = claude_client.call_claude("system", "user", task="short_script")

        self.assertEqual(result, "real content")
        self.assertEqual(fake_client.messages.create.call_count, 2)

    def test_no_usable_text_in_any_block_exhausts_retries_with_clear_diagnostics(self):
        """Reproduces the exact real-run outcome: every attempt returns the
        identical no-usable-text shape (a systematic, not transient, failure).
        Must still retry (in case it really is transient) but ultimately fail
        loud with the block_types breakdown in the message for diagnosis."""
        bad_response = SimpleNamespace(
            content=[_text_block(""), _block("other", text=None)],
            usage=_usage(output_tokens=700), stop_reason="end_turn",
        )
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [bad_response] * claude_client._MAX_RETRIES

        with patch.object(claude_client, "_get_client", return_value=fake_client), \
             patch.object(claude_client._time, "sleep"):
            with self.assertRaises(ValueError) as ctx:
                claude_client.call_claude("system", "user", task="short_script")

        self.assertIn("no usable text block", str(ctx.exception))
        self.assertIn("block_types", str(ctx.exception))
        self.assertEqual(fake_client.messages.create.call_count, claude_client._MAX_RETRIES)

    def test_whitespace_only_text_is_treated_as_unusable_and_retried(self):
        bad_response = SimpleNamespace(content=[_text_block("   ")], usage=_usage(), stop_reason="end_turn")
        good_response = SimpleNamespace(
            content=[_text_block("real content")], usage=_usage(), stop_reason="end_turn"
        )
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [bad_response, good_response]

        with patch.object(claude_client, "_get_client", return_value=fake_client), \
             patch.object(claude_client._time, "sleep"):
            result = claude_client.call_claude("system", "user", task="short_script")

        self.assertEqual(result, "real content")


class TestParseClaudeJsonToleratesRawControlChars(unittest.TestCase):
    def test_literal_newline_inside_json_string_value_is_accepted(self):
        """Reproduces the exact real-run failure: a multi-paragraph
        voice_script value containing a literal (unescaped) newline character,
        which strict json.loads() rejects as an invalid control character."""
        raw = '{"title": "T", "voice_script": "Line one.\nLine two continues here."}'
        # Sanity: strict parsing really does reject this, proving the fixture
        # reproduces the real defect rather than being a no-op change.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw, strict=True)

        result = claude_client.parse_claude_json(
            raw,
            required_keys=["title", "voice_script"],
            type_checks={"title": str, "voice_script": str},
            allowed_keys=["title", "voice_script"],
        )
        self.assertEqual(result["title"], "T")
        self.assertIn("Line one.", result["voice_script"])
        self.assertIn("Line two continues here.", result["voice_script"])

    def test_genuinely_malformed_json_still_raises(self):
        with self.assertRaises(ValueError):
            claude_client.parse_claude_json("{not json at all", required_keys=["x"])


if __name__ == "__main__":
    unittest.main()
