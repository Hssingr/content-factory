"""P2-3 proof: storyboard beat-count overshoot is trimmed, not regenerated.

Only the paid Claude storyboard batch boundary is stubbed. The real
split_into_beats() orchestration, deterministic overshoot trim, merge, hint
matching, and timestamp mapping all run.
"""

from __future__ import annotations

import inspect
import logging
import unittest

from app.agents.agent4_visuals.subagents import storyboard as sb


def _beat(order: int, phrase: str) -> dict:
    return {
        "beat_order": order,
        "start_hint": phrase,
        "end_hint": phrase,
        "visual_intent": f"visual intent {order}",
        "visual_type": "b-roll",
        "visual_category": "place",
        "environment": "other",
        "flux_prompt": f"physical scene {order}",
        "effect": "slow_zoom",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "motif": "other",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "text_card_style": "default",
    }


def _transcript(words: list[str]) -> list[dict]:
    return [
        {"word": word, "start": i * 0.25, "end": i * 0.25 + 0.2}
        for i, word in enumerate(words)
    ]


class StoryboardBeatCountOvershootTrimTest(unittest.TestCase):
    def test_static_trim_is_deterministic_and_logged(self):
        source = inspect.getsource(sb._trim_beat_count_overshoot)
        self.assertIn("STORYBOARD_BEAT_COUNT_OVERSHOOT_TRIMMED", source)
        self.assertIn("return kept", source)
        self.assertNotIn("generate_storyboard_batch", source)

    def test_split_into_beats_trims_tail_overshoot_without_retry(self):
        words = [f"word{i}" for i in range(72)]
        voice_script = "[SECTION 1]\n" + " ".join(words)
        transcript = _transcript(words)
        calls: list[int] = []

        def fake_generate_storyboard_batch(**kwargs):
            target = int(kwargs["target_beat_count"])
            calls.append(target)
            generated = target + 7
            beats = []
            for order in range(generated):
                start = order * 6
                phrase_words = words[start:start + 6] or words[-6:]
                beats.append(_beat(order, " ".join(phrase_words)))
            return (
                {"storyboard_status": "APPROVED", "overall_style": "test", "beats": beats, "global_notes": []},
                {"output_tokens": 100, "input_tokens": 50},
                {"was_truncated": False, "attempt_count": 1, "input_tokens": 50, "elapsed_ms": 1},
            )

        original = sb.generate_storyboard_batch
        sb.generate_storyboard_batch = fake_generate_storyboard_batch
        try:
            with self.assertLogs(sb.logger.name, level=logging.WARNING) as logs:
                sections = sb.split_into_beats(
                    voice_script=voice_script,
                    duration_ms=12_000,
                    channel=type("Channel", (), {"niche": "test", "tone": "test"})(),
                    script_format="youtube_long",
                    whisper_transcript=transcript,
                    allow_legacy_fallback=True,
                    language="en",
                )
        finally:
            sb.generate_storyboard_batch = original

        self.assertEqual(len(calls), 1, "overshoot must not trigger a regeneration")
        target = calls[0]
        self.assertIsNotNone(sections)
        self.assertEqual(len(sections), target + 2)
        self.assertEqual([section["beat_order"] for section in sections], list(range(target + 2)))
        self.assertTrue(
            any("STORYBOARD_BEAT_COUNT_OVERSHOOT_TRIMMED" in msg for msg in logs.output),
            logs.output,
        )


if __name__ == "__main__":
    unittest.main()
