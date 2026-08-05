"""P2-1 proof: fallback storyboard beats never store visual_intent as narration.

No external APIs. Exercises the real timestamp mapper and the real Agent 4
metadata serializer used before VideoSection persistence.
"""

from __future__ import annotations

import inspect
import logging
import unittest

from app.agents.agent4_visuals.services.visual_orchestrator import _beat_extras
from app.agents.agent4_visuals.subagents import storyboard


def _transcript(words: list[str]) -> list[dict]:
    return [
        {"word": word, "start": i * 0.4, "end": (i + 1) * 0.4}
        for i, word in enumerate(words)
    ]


class StoryboardScriptTextFallbackTest(unittest.TestCase):
    def test_static_no_visual_intent_script_text_fallback_remains(self):
        source = inspect.getsource(storyboard.map_storyboard_beats_to_timestamps)
        self.assertNotIn('else str(beat.get("visual_intent"', source)
        # 2026-08-05 (content 069d8d06 fix): a fallback beat's script_text is
        # now the real transcript span it plays against when one exists
        # (_words_in_ms_span), not unconditionally "" — but it must never
        # silently become the beat's own visual_intent/prompt language.
        self.assertIn('_words_in_ms_span(flat, start_ms, end_ms)', source)
        self.assertIn('STORYBOARD_SCRIPT_TEXT_EMPTY_FALLBACK', source)

    def test_runtime_fallback_uses_empty_script_text_and_persisted_flag(self):
        beats = [
            {
                "beat_order": 0,
                "start_hint": "alpha bravo charlie delta echo foxtrot",
                "end_hint": "alpha bravo charlie delta echo foxtrot",
                "visual_intent": "real narration must not become this prompt language",
                "suggested_duration_sec": 3.0,
                "beat_intensity": "medium",
                "flux_prompt": "a physical clue in a hallway",
            },
            {
                "beat_order": 1,
                "start_hint": "missing hint words never spoken",
                "end_hint": "missing hint words never spoken",
                "visual_intent": "camera direction, not narration",
                "suggested_duration_sec": 3.0,
                "beat_intensity": "medium",
                "flux_prompt": "a tense room detail",
            },
        ]
        transcript = _transcript("alpha bravo charlie delta echo foxtrot filler words".split())

        with self.assertLogs(storyboard.logger.name, level=logging.WARNING) as logs:
            sections = storyboard.map_storyboard_beats_to_timestamps(
                beats,
                transcript,
                duration_ms=8000,
                allow_legacy_fallback=True,
                language="en",
            )

        self.assertIsNotNone(sections)
        fallback = sections[1]
        self.assertEqual(fallback["script_text"], "")
        self.assertEqual(fallback["visual_intent"], "camera direction, not narration")
        self.assertEqual(fallback["script_text_source"], "empty_fallback_no_transcript_span")
        self.assertTrue(fallback["script_text_missing"])
        self.assertTrue(any("STORYBOARD_SCRIPT_TEXT_EMPTY_FALLBACK" in msg for msg in logs.output))

        extras = _beat_extras(fallback)
        self.assertEqual(extras["script_text_source"], "empty_fallback_no_transcript_span")
        self.assertTrue(extras["script_text_missing"])
        self.assertEqual(extras["visual_intent"], "camera direction, not narration")


if __name__ == "__main__":
    unittest.main()
