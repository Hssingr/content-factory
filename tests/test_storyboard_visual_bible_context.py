"""Runtime proof for Phase 2.1 visual-bible prompt inversion.

Only the paid Claude boundary is stubbed. The real ``generate_storyboard_batch``
function builds the user message, validates the structured response shape, and
returns diagnostics.
"""

from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.subagents.storyboard import split_into_beats


def _storyboard_response() -> tuple[dict, dict]:
    return (
        {
            "storyboard_status": "APPROVED",
            "overall_style": "documentary continuity",
            "global_notes": [],
            "beats": [
                {
                    "beat_order": 0,
                    "start_hint": "Eli stands",
                    "end_hint": "half-open door",
                    "visual_intent": "Eli beside the half-open bedroom door",
                    "visual_type": "action",
                    "visual_category": "person",
                    "environment": "upstairs hallway",
                    "flux_prompt": (
                        "Eli beside a half-open bedroom door, pale face turned toward "
                        "the hall, documentary photograph, natural shadows"
                    ),
                    "effect": "slow_zoom",
                    "color_grade": "desaturated",
                    "transition_to_next": "cut",
                    "motif": "half-open door",
                    "beat_intensity": "high",
                    "suggested_duration_sec": 2.0,
                }
            ],
        },
        {"input_tokens": 123, "output_tokens": 456},
    )


class StoryboardVisualBibleContextTest(unittest.TestCase):
    def test_generate_storyboard_batch_sends_compact_visual_bible_to_claude_boundary(self) -> None:
        calls: list[dict] = []

        def fake_claude(**kwargs):
            calls.append(kwargs)
            return _storyboard_response()

        visual_bible = {
            "story_visual_summary": "A narrow home hallway story with recurring door imagery.",
            "config_context": {"visual_style": "documentary horror", "image_style": "photorealistic"},
            "global_style": {
                "realism_level": "photorealistic",
                "color_grade": "cool blue-gray",
                "composition_rules": ["foreground obstruction"],
                "camera_language": ["slow push-in", "locked-off frame"],
                "lighting_rules": ["cold moonlight", "deep shadows"],
            },
            "characters": [
                {
                    "name": "Eli",
                    "role": "frightened teenage boy",
                    "appearance": "dark curls",
                    "clothing": "oversized gray hoodie",
                    "continuity_tags": ["eli-gray-hoodie"],
                }
            ],
            "locations": [
                {
                    "name": "upstairs hallway",
                    "description": "narrow hallway with peeling wallpaper",
                    "recurring_details": ["half-open bedroom door"],
                    "continuity_tags": ["upstairs-hallway"],
                }
            ],
        }

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_claude):
            storyboard, usage, diag = system_prompt.generate_storyboard_batch(
                segment_label="[SECTION 1]",
                segment_text="Eli stands beside the half-open door.",
                segment_index=1,
                segment_count=1,
                channel=SimpleNamespace(niche="true scary stories", tone="tense"),
                target_beat_count=1,
                visual_style="documentary horror",
                image_style="photorealistic",
                visual_bible_context=visual_bible,
            )

        self.assertEqual(storyboard["storyboard_status"], "APPROVED")
        self.assertEqual(usage["input_tokens"], 123)
        self.assertEqual(diag["attempt_count"], 1)
        self.assertEqual(len(calls), 1)

        user_message = calls[0]["user_message"]
        self.assertIn("Visual continuity bible (compact JSON):", user_message)
        json_line = user_message.split("Visual continuity bible (compact JSON):\n", 1)[1].split("\n", 1)[0]
        compact = json.loads(json_line)
        self.assertEqual(compact["characters"][0]["name"], "Eli")
        self.assertEqual(compact["locations"][0]["name"], "upstairs hallway")
        self.assertNotIn("camera_language", json_line)
        self.assertNotIn("lighting_rules", json_line)
        self.assertNotIn("slow push-in", json_line)
        self.assertNotIn("cold moonlight", json_line)

    def test_split_into_beats_threads_visual_bible_to_storyboard_batch(self) -> None:
        calls: list[dict] = []

        def fake_claude(**kwargs):
            calls.append(kwargs)
            return _storyboard_response()

        transcript_words = "Eli stands beside the half open door".split()
        transcript = [
            {"word": word, "start": index * 0.4, "end": (index + 1) * 0.4}
            for index, word in enumerate(transcript_words)
        ]
        visual_bible = {
            "characters": [{"name": "Eli", "appearance": "dark curls", "clothing": "oversized gray hoodie"}],
            "locations": [{"name": "upstairs hallway", "description": "narrow hallway"}],
            "global_style": {
                "camera_language": ["slow push-in"],
                "lighting_rules": ["cold moonlight"],
            },
        }

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_claude):
            beats = split_into_beats(
                voice_script="[SECTION 1]\nEli stands beside the half open door",
                duration_ms=2800,
                channel=SimpleNamespace(niche="true scary stories", tone="tense"),
                script_format="youtube_long",
                whisper_transcript=transcript,
                visual_style="documentary horror",
                image_style="photorealistic",
                visual_bible_context=visual_bible,
            )

        self.assertTrue(beats)
        self.assertEqual(len(calls), 1)
        user_message = calls[0]["user_message"]
        self.assertIn("Visual continuity bible (compact JSON):", user_message)
        self.assertIn('"name":"Eli"', user_message)
        self.assertIn('"name":"upstairs hallway"', user_message)
        self.assertNotIn("slow push-in", user_message)
        self.assertNotIn("cold moonlight", user_message)


if __name__ == "__main__":
    unittest.main()
