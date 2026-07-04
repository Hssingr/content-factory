"""Runtime proof for generation-time storyboard diversity constraints.

Only the paid Claude boundary is stubbed. The test exercises the real
``split_into_beats`` orchestration, ledger update, continuity summary, and
second storyboard-batch user message.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.subagents.storyboard import split_into_beats


def make_beat(order: int, *, env: str, motif: str, start_hint: str, end_hint: str) -> dict:
    return {
        "beat_order": order,
        "start_hint": start_hint,
        "end_hint": end_hint,
        "visual_intent": f"Specific visual detail for {motif} in {env}",
        "visual_type": "action",
        "visual_category": "object",
        "environment": env,
        "flux_prompt": (
            "Specific interior office detail beside a desk lamp, folders, "
            "brass handle, practical lamp glow, documentary photograph, "
            "sharp focus, no readable text"
        ),
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": motif,
        "beat_intensity": "medium",
        "suggested_duration_sec": 1.0,
    }


def response(beats: list[dict]) -> tuple[dict, dict]:
    return (
        {
            "storyboard_status": "APPROVED",
            "overall_style": "documentary continuity",
            "global_notes": [],
            "beats": beats,
        },
        {"input_tokens": 100, "output_tokens": 200},
    )


class StoryboardGenerationDiversityConstraintsTest(unittest.TestCase):
    def test_split_into_beats_injects_forbidden_env_and_motif_into_next_segment(self) -> None:
        calls: list[dict] = []

        first_beats = [
            make_beat(0, env="indoor_office", motif="object", start_hint="Eli opens the", end_hint="folder under the"),
            make_beat(1, env="indoor_office", motif="object", start_hint="opens the office", end_hint="under the lamp"),
            make_beat(2, env="indoor_office", motif="object", start_hint="office drawer and", end_hint="the lamp nearby"),
        ]
        second_beats = [
            make_beat(3, env="urban_street", motif="doorway", start_hint="Mara steps outside", end_hint="from the porch"),
        ]

        def fake_claude(**kwargs):
            calls.append(kwargs)
            return response(first_beats if len(calls) == 1 else second_beats)

        script = (
            "[SECTION 1]\n"
            "Eli opens the office drawer and sees a folder under the lamp nearby.\n"
            "[SECTION 2]\n"
            "Mara steps outside and watches the driveway from the porch."
        )
        transcript_words = (
            "Eli opens the office drawer and sees a folder under the lamp nearby "
            "Mara steps outside and watches the driveway from the porch"
        ).split()
        transcript = [
            {"word": word, "start": index * 0.25, "end": (index + 1) * 0.25}
            for index, word in enumerate(transcript_words)
        ]

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_claude):
            beats = split_into_beats(
                voice_script=script,
                duration_ms=5000,
                channel=SimpleNamespace(niche="true scary stories", tone="tense"),
                script_format="youtube_long",
                whisper_transcript=transcript,
                allow_legacy_fallback=True,
            )

        self.assertTrue(beats)
        self.assertEqual(len(calls), 2)
        second_message = calls[1]["user_message"]
        self.assertIn("FORBIDDEN environments for the next segment: indoor_office", second_message)
        self.assertIn("FORBIDDEN motifs for the next segment: object", second_message)
        self.assertIn("Previous segment context", second_message)


if __name__ == "__main__":
    unittest.main()
