"""Runtime proof for child Short parent-reuse pool filtering.

Only the paid Claude remap boundary is stubbed. The test exercises the real
``remap_beats_for_short`` path: parent DB rows are parsed, the Claude candidate
pool is built, assignments are applied, and the result is mapped to the Short
transcript.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals.subagents import storyboard
from app.agents.agent4_visuals.subagents.storyboard import remap_beats_for_short


class FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows


class FakeDb:
    def __init__(self, rows: list[SimpleNamespace]):
        self.rows = rows

    def query(self, model):
        return FakeQuery(self.rows)


def parent_row(order: int, *, extras: dict, flux_prompt: str = "parent prompt") -> SimpleNamespace:
    return SimpleNamespace(
        section_order=order,
        generation_prompt=json.dumps(extras),
        flux_prompt=flux_prompt,
        effect="cut",
        color_grade="neutral",
    )


def transcript(words: str) -> list[dict]:
    return [
        {"word": word, "start": index * 0.25, "end": (index + 1) * 0.25}
        for index, word in enumerate(words.split())
    ]


class ShortRemapReusePoolTest(unittest.TestCase):
    def test_legacy_parent_text_card_is_not_offered_or_reused(self) -> None:
        parent_id = uuid.uuid4()
        rows = [
            parent_row(
                0,
                extras={
                    "media_url": "cache/parent/reusable.jpg",
                    "visual_intent": "usable hallway clue",
                    "environment": "corridor_interior",
                    "motif": "doorway",
                    "visual_type": "action",
                },
                flux_prompt="Hallway clue beside a practical lamp, no readable text",
            ),
            parent_row(
                1,
                extras={
                    "media_url": "cache/parent/text-card-background.jpg",
                    "media_strategy": "remotion_text_card",
                    "visual_type": "text_card",
                    "visual_intent": "legacy text card background",
                    "environment": "indoor_office",
                    "motif": "document",
                },
                flux_prompt="Legacy text-card background with pseudo lettering",
            ),
        ]
        captured: dict[str, str] = {}

        def fake_remap_call(**kwargs):
            captured["user_message"] = kwargs["user_message"]
            return (
                {
                    "assignments": [
                        {
                            "short_beat_order": 0,
                            "long_beat_order": 1,
                            "match_score": 100,
                            "narration_phrase": "Mara checks the hallway clue near the quiet porch",
                            "new_image_prompt": "",
                            "beat_intensity": "medium",
                        }
                    ]
                },
                {"output_tokens": 120},
            )

        with patch.object(storyboard, "call_claude_structured_with_usage", side_effect=fake_remap_call):
            beats = remap_beats_for_short(
                short_content=SimpleNamespace(id=uuid.uuid4()),
                short_voice_script="Mara checks the hallway clue near the quiet porch.",
                short_audio_file=SimpleNamespace(
                    duration_ms=3000,
                    language="en",
                    whisper_transcript=transcript("Mara checks the hallway clue near the quiet porch"),
                ),
                parent_content_id=parent_id,
                db=FakeDb(rows),
            )

        self.assertIn('"beat_order": 0', captured["user_message"])
        self.assertNotIn('"beat_order": 1', captured["user_message"])
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["media_url"], "")
        self.assertNotEqual(beats[0].get("media_url"), "cache/parent/text-card-background.jpg")
        self.assertNotEqual(beats[0].get("visual_type"), "text_card")

    def test_prompt_inheritance_by_score_all_beats_pending(self) -> None:
        """Fresh full-system audit §3.1: match_score decides PROMPT inheritance
        only — every beat leaves remap pending (media_url="") because portrait
        generation regenerates every child image fresh; there is no physical
        reuse and no reuse budget anymore."""
        rows = [
            parent_row(
                i,
                extras={
                    "media_url": f"cache/parent/{i}.jpg",
                    "visual_intent": f"usable parent beat {i}",
                    "environment": "corridor_interior",
                    "motif": "doorway",
                    "visual_type": "action",
                },
                flux_prompt=f"Parent image prompt {i}, practical lamp, no readable text",
            )
            for i in range(5)
        ]
        scores = [100, 99, 98, 70, 42]

        def fake_remap_call(**kwargs):
            return (
                {
                    "assignments": [
                        {
                            "short_beat_order": i,
                            "long_beat_order": i,
                            "match_score": score,
                            "narration_phrase": f"Mara checks hallway clue number {i} near the quiet porch",
                            "new_image_prompt": (
                                "" if score >= 70
                                else f"Vertical hallway clue close-up number {i}, practical lamp, no readable text"
                            ),
                            "beat_intensity": "medium",
                        }
                        for i, score in enumerate(scores)
                    ]
                },
                {"output_tokens": 200},
            )

        with patch.object(storyboard, "call_claude_structured_with_usage", side_effect=fake_remap_call):
            beats = remap_beats_for_short(
                short_content=SimpleNamespace(id=uuid.uuid4()),
                short_voice_script="Mara checks several hallway clues near the quiet porch.",
                short_audio_file=SimpleNamespace(
                    # 50 transcript words * 250ms/word (see transcript()) = 12500ms —
                    # duration_ms must cover the real transcript span, or micro-beat
                    # cleanup correctly clamps/discards every beat past the stated
                    # audio duration (this is the timestamp mapper's real,
                    # already-implemented fail-loud density guard, not a bug).
                    duration_ms=12500,
                    language="en",
                    whisper_transcript=transcript(
                        "Mara checks hallway clue number 0 near the quiet porch "
                        "Mara checks hallway clue number 1 near the quiet porch "
                        "Mara checks hallway clue number 2 near the quiet porch "
                        "Mara checks hallway clue number 3 near the quiet porch "
                        "Mara checks hallway clue number 4 near the quiet porch"
                    ),
                ),
                parent_content_id=uuid.uuid4(),
                db=FakeDb(rows),
            )

        self.assertEqual(len(beats), 5)
        # Every beat is pending generation — no physical parent image reuse.
        self.assertEqual([beat["media_url"] for beat in beats], [""] * 5)
        # Scores >= 70 inherit the parent beat's prompt; below 70 use the new prompt.
        for i, score in enumerate(scores):
            if score >= 70:
                self.assertEqual(
                    beats[i]["flux_prompt"],
                    f"Parent image prompt {i}, practical lamp, no readable text",
                )
            else:
                self.assertIn("Vertical hallway clue close-up", beats[i]["flux_prompt"])


    def test_low_score_child_new_image_uses_real_new_prompt(self) -> None:
        rejected_parent_prompt = "Rejected parent prompt with stale wide-frame visual language"
        child_prompt = (
            "Vertical documentary photograph of Mara holding a brass door chain in a narrow hallway, "
            "foreground hands sharp, apartment doorway behind her, practical lamp light, no readable text"
        )
        rows = [
            parent_row(
                0,
                extras={
                    "media_url": "cache/parent/rejected-wide.jpg",
                    "visual_intent": "old parent hallway card",
                    "environment": "corridor_interior",
                    "motif": "doorway",
                    "visual_type": "action",
                },
                flux_prompt=rejected_parent_prompt,
            )
        ]

        def fake_remap_call(**kwargs):
            return (
                {
                    "assignments": [
                        {
                            "short_beat_order": 0,
                            "long_beat_order": 0,
                            "match_score": 42,
                            "narration_phrase": "Mara finds the chain hanging open after midnight",
                            "new_image_prompt": child_prompt,
                            "beat_intensity": "high",
                        }
                    ]
                },
                {"output_tokens": 120},
            )

        with patch.object(storyboard, "call_claude_structured_with_usage", side_effect=fake_remap_call):
            beats = remap_beats_for_short(
                short_content=SimpleNamespace(id=uuid.uuid4()),
                short_voice_script="Mara finds the chain hanging open after midnight.",
                short_audio_file=SimpleNamespace(
                    duration_ms=3000,
                    language="en",
                    whisper_transcript=transcript("Mara finds the chain hanging open after midnight"),
                ),
                parent_content_id=uuid.uuid4(),
                db=FakeDb(rows),
            )

        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["media_url"], "")
        self.assertEqual(beats[0]["flux_prompt"], child_prompt)
        self.assertNotIn(rejected_parent_prompt, beats[0]["flux_prompt"])
        self.assertNotEqual(beats[0]["flux_prompt"], beats[0]["script_text"])

    def test_low_score_child_new_image_missing_prompt_fails_remap(self) -> None:
        rows = [
            parent_row(
                0,
                extras={
                    "media_url": "cache/parent/rejected-wide.jpg",
                    "visual_intent": "old parent hallway card",
                    "environment": "corridor_interior",
                    "motif": "doorway",
                    "visual_type": "action",
                },
                flux_prompt="Rejected parent prompt that must not be inherited",
            )
        ]

        def fake_remap_call(**kwargs):
            return (
                {
                    "assignments": [
                        {
                            "short_beat_order": 0,
                            "long_beat_order": 0,
                            "match_score": 42,
                            "narration_phrase": "Mara finds the chain hanging open after midnight",
                            "new_image_prompt": "",
                            "beat_intensity": "high",
                        }
                    ]
                },
                {"output_tokens": 120},
            )

        with patch.object(storyboard, "call_claude_structured_with_usage", side_effect=fake_remap_call):
            beats = remap_beats_for_short(
                short_content=SimpleNamespace(id=uuid.uuid4()),
                short_voice_script="Mara finds the chain hanging open after midnight.",
                short_audio_file=SimpleNamespace(
                    duration_ms=3000,
                    language="en",
                    whisper_transcript=transcript("Mara finds the chain hanging open after midnight"),
                ),
                parent_content_id=uuid.uuid4(),
                db=FakeDb(rows),
            )

        self.assertEqual(beats, [])


if __name__ == "__main__":
    unittest.main()
