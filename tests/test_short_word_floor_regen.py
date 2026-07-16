"""Runtime proof: the Short word-floor regeneration + recalibrated word economy.

Operator rules (2026-07-16, binding): a Short must NEVER run under 61 seconds;
it should stay at or under ~90 seconds, but exceeding 90s never fails anything.

Tier 1's silence compression raised the real Short narration rate from the
~120 wpm the old constants were calibrated on to ~176 wpm measured (run
41f7eeb8: 246 words → 83.7s) — at that rate the old 140–170-word target
produced 48–58s drafts, ALL under the hard 61s audio floor. Recalibration:
target 210–260 words, floor 190, cap 270 (telemetry-only).

The word-floor regeneration is the ONE operator-approved Elimination Mandate
exception: an objective word-count trigger (never an AI quality judgment),
exactly one regeneration, the LONGER draft kept deterministically.

No live API calls — generate_short_episode_script (the paid boundary) is
stubbed; _generate_short_script and every deterministic check run unmodified.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery.services import scripts as scripts_module


def _words(n: int) -> str:
    return "The scout returned pale and silent before dawn. " * (n // 8)


_PART_PLAN = {
    "part": 2, "_total_parts": 4, "title_hint": "The ledge",
    "opening_hook": "I sent one man down the cliff on a rope.",
    "main_reveal": "The rope came back cut.", "cliffhanger": "Something cut it from below.",
}
_BLUEPRINT = {"hook": "x", "final_payoff": "y", "comment_trigger": "Would you go on?"}


class ShortWordFloorRegenTest(unittest.TestCase):
    def _run(self, side_effect):
        channel = SimpleNamespace(niche="history", tone="dramatic")
        with patch.object(
            scripts_module, "generate_short_episode_script", side_effect=side_effect,
        ) as mock_gen:
            result = scripts_module._generate_short_script(
                part_plan=dict(_PART_PLAN), part_n=2,
                voice_script=_words(1600), blueprint=dict(_BLUEPRINT),
                channel=channel, channel_voice=None, source_language="en",
            )
        return result, mock_gen

    def test_draft_at_target_needs_no_regeneration(self):
        draft = {"title": "T", "voice_script": _words(224)}
        result, mock_gen = self._run([draft])
        self.assertEqual(mock_gen.call_count, 1)
        self.assertEqual(result["voice_script"], draft["voice_script"])

    def test_under_floor_draft_triggers_exactly_one_regeneration(self):
        short_draft = {"title": "T", "voice_script": _words(152)}
        long_draft = {"title": "T", "voice_script": _words(224)}
        result, mock_gen = self._run([short_draft, long_draft])
        self.assertEqual(mock_gen.call_count, 2)
        self.assertEqual(result["voice_script"], long_draft["voice_script"])
        note = mock_gen.call_args_list[1].kwargs["word_floor_note"]
        self.assertIn("MUST contain at least", note)
        self.assertIn("61 seconds", note)

    def test_both_under_floor_keeps_the_longer_draft_and_proceeds(self):
        """Deterministic choice, never an AI judgment — and never a block:
        the 61s audio gate remains the final enforcement."""
        first = {"title": "T", "voice_script": _words(120)}
        second = {"title": "T", "voice_script": _words(160)}
        with self.assertLogs(scripts_module.logger, level="ERROR") as logs:
            result, mock_gen = self._run([first, second])
        self.assertEqual(mock_gen.call_count, 2)
        self.assertEqual(result["voice_script"], second["voice_script"])
        self.assertTrue(any("SHORT_SCRIPT_STILL_UNDER_FLOOR" in l for l in logs.output))

    def test_regen_shorter_than_original_keeps_the_original(self):
        first = {"title": "T", "voice_script": _words(176)}
        second = {"title": "T", "voice_script": _words(120)}
        result, _ = self._run([first, second])
        self.assertEqual(result["voice_script"], first["voice_script"])

    def test_regen_transport_failure_keeps_the_original_draft(self):
        first = {"title": "T", "voice_script": _words(152)}
        result, mock_gen = self._run([first, RuntimeError("api down")])
        self.assertEqual(mock_gen.call_count, 2)
        self.assertEqual(result["voice_script"], first["voice_script"])


class WordEconomyRecalibrationTest(unittest.TestCase):
    """Static proof the recalibrated economy is coherent with the 61s floor."""

    def test_constants_recalibrated(self):
        self.assertEqual(scripts_module._MIN_SHORT_WORDS, 190)
        self.assertEqual(scripts_module._MAX_SHORT_WORDS, 270)

    def test_floor_clears_61s_at_measured_compressed_rate(self):
        # 176 wpm measured post-compression (run 41f7eeb8: 246 words → 83.7s)
        measured_wpm = 176.0
        floor_seconds = scripts_module._MIN_SHORT_WORDS / measured_wpm * 60
        self.assertGreaterEqual(floor_seconds, 61.0 + 3.0,
                                "the word floor must clear the hard 61s audio "
                                "gate with real margin at the measured rate")

    def test_prompts_carry_the_new_targets(self):
        from app.agents.agent2_discovery import system_prompt as sp
        self.assertIn("210–260", sp._SHORT_EPISODE_SYSTEM_PROMPT)
        self.assertIn("fewer than 190", sp._SHORT_EPISODE_SYSTEM_PROMPT)
        self.assertIn("210–260", sp._SHORTS_PLANNER_SYSTEM_PROMPT)
        self.assertIn("61 seconds", sp._SHORTS_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn("140–170", sp._SHORT_EPISODE_SYSTEM_PROMPT)
        self.assertNotIn("140–170", sp._SHORTS_PLANNER_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
