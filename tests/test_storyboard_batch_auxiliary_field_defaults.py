"""Runtime proof for a real production failure: a storyboard_batch response
that carried a complete, valid ``beats`` array but was missing
``storyboard_status``/``overall_style``/``global_notes`` was treated as fatal,
aborting storyboard generation for the whole segment (observed verbatim:
``STORYBOARD_SHAPE_INVALID segment=[INTRO] key=storyboard_status
issue=missing present_keys=['beats']``, on both the initial attempt and the
one retry, ultimately failing the entire content item to FAILED).

Grep confirms none of the three auxiliary fields have a real downstream
consumer (storyboard_status is never read past shape validation, global_notes
is never read at all, overall_style only feeds one diagnostic log line) — so
``_check_shape()`` now only hard-requires ``beats`` and defaults the rest with
a logged warning instead of raising.

Only the paid Claude boundary (``call_claude_structured_with_usage``) is
stubbed — the real ``generate_storyboard_batch()`` function (including
``_check_shape()``) runs unmodified.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals import system_prompt


def _valid_beat(order: int = 0) -> dict:
    return {
        "beat_order": order,
        "start_hint": "a grinding sound",
        "end_hint": "no one asks why",
        "visual_intent": "the mine entrance at dusk",
        "visual_type": "b-roll",
        "visual_category": "place",
        "environment": "industrial",
        "flux_prompt": "abandoned mine entrance, rusted machinery, overcast sky, wide shot",
        "effect": "slow_zoom",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "exterior",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
    }


class _Channel:
    niche = "Reddit horror story narration"
    tone = "tense, grounded"


class TestAuxiliaryFieldsDefaultedNotFatal(unittest.TestCase):
    def test_missing_all_three_auxiliary_fields_still_returns_beats(self):
        """Reproduces the exact real-run shape: only `beats` present."""
        response = {"beats": [_valid_beat(0), _valid_beat(1)]}
        with patch.object(
            system_prompt, "call_claude_structured_with_usage",
            return_value=(response, {"input_tokens": 100, "output_tokens": 200}),
        ) as fake_call:
            storyboard, usage, diag = system_prompt.generate_storyboard_batch(
                segment_label="[INTRO]",
                segment_text="A grinding metal sound never stops echoing from the mountain.",
                segment_index=1,
                segment_count=6,
                channel=_Channel(),
                target_beat_count=5,
            )

        # No retry needed — this must succeed on the first attempt.
        self.assertEqual(fake_call.call_count, 1)
        self.assertEqual(len(storyboard["beats"]), 2)
        # Auxiliary fields are defaulted in place, not left absent.
        self.assertEqual(storyboard["storyboard_status"], "")
        self.assertEqual(storyboard["overall_style"], "")
        self.assertEqual(storyboard["global_notes"], [])

    def test_wrong_typed_auxiliary_field_is_defaulted_not_fatal(self):
        response = {
            "beats": [_valid_beat(0)],
            "storyboard_status": 123,       # wrong type (int, not str)
            "overall_style": None,          # wrong type
            "global_notes": "not a list",   # wrong type
        }
        with patch.object(
            system_prompt, "call_claude_structured_with_usage",
            return_value=(response, {"input_tokens": 100, "output_tokens": 200}),
        ) as fake_call:
            storyboard, _usage, _diag = system_prompt.generate_storyboard_batch(
                segment_label="[SECTION 1]",
                segment_text="Body narration.",
                segment_index=2,
                segment_count=6,
                channel=_Channel(),
                target_beat_count=5,
            )

        self.assertEqual(fake_call.call_count, 1)
        self.assertEqual(storyboard["storyboard_status"], "")
        self.assertEqual(storyboard["overall_style"], "")
        self.assertEqual(storyboard["global_notes"], [])

    def test_missing_beats_is_still_fatal_and_retried(self):
        """The one truly load-bearing field must remain strictly required —
        this proves the fix didn't accidentally loosen validation on `beats`
        itself, only on the administrative fields."""
        bad_response = {"storyboard_status": "APPROVED", "overall_style": "x", "global_notes": []}
        with patch.object(
            system_prompt, "call_claude_structured_with_usage",
            return_value=(bad_response, {"input_tokens": 100, "output_tokens": 200}),
        ) as fake_call:
            with self.assertRaises(ValueError) as ctx:
                system_prompt.generate_storyboard_batch(
                    segment_label="[INTRO]",
                    segment_text="Narration.",
                    segment_index=1,
                    segment_count=1,
                    channel=_Channel(),
                    target_beat_count=5,
                )

        self.assertIn("beats", str(ctx.exception))
        # One retry attempted before failing loud (existing shape-retry convention).
        self.assertEqual(fake_call.call_count, 2)

    def test_beats_as_json_string_still_coerced_with_missing_auxiliary_fields(self):
        """Combined real-world shape: beats correctly generated but returned as
        a JSON-encoded string (the pre-existing coercion quirk), AND the
        auxiliary fields are absent. Both recoveries must compose."""
        import json
        response = {"beats": json.dumps([_valid_beat(0)])}
        with patch.object(
            system_prompt, "call_claude_structured_with_usage",
            return_value=(response, {"input_tokens": 100, "output_tokens": 200}),
        ) as fake_call:
            storyboard, _usage, _diag = system_prompt.generate_storyboard_batch(
                segment_label="[OUTRO]",
                segment_text="Narration.",
                segment_index=6,
                segment_count=6,
                channel=_Channel(),
                target_beat_count=5,
            )

        self.assertEqual(fake_call.call_count, 1)
        self.assertIsInstance(storyboard["beats"], list)
        self.assertEqual(storyboard["beats"][0]["beat_order"], 0)
        self.assertEqual(storyboard["storyboard_status"], "")


if __name__ == "__main__":
    unittest.main()
