"""Runtime proof for roadmap 5.1 titled-section TTS delivery.

No external APIs are called. These tests exercise the real deterministic
section parser and delivery selector used before Cartesia requests are built.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace


class _VoiceSettingsStub:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=_VoiceSettingsStub))

from app.agents.agent3_audio.services import tts


class TtsSectionTitleDeliveryTest(unittest.TestCase):
    def test_titled_reveal_section_selects_scared_fast_delivery(self) -> None:
        units = tts._split_script_into_section_units(
            "[INTRO]\nThe house looked normal.\n\n"
            "[SECTION 1: The scratches under the door]\nThe first clue appears.\n\n"
            "[SECTION 3: The receipt reveals who paid]\nThe truth lands at once.\n\n"
            "[OUTRO]\nNobody opens the door again."
        )

        reveal = units[2]
        self.assertEqual(reveal["section_title"], "The receipt reveals who paid")

        delivery = tts._select_section_delivery(
            reveal,
            channel_emotion="calm",
            channel_speed_profile="normal",
            is_short_episode=False,
        )

        self.assertEqual(delivery["reason"], "climax_title")
        self.assertEqual(delivery["emotion"], "scared")
        self.assertEqual(delivery["speed_profile"], "fast")

    def test_blueprint_turn_exposes_title_selects_climax_delivery(self) -> None:
        context = tts._parse_section_context(
            "[SECTION 4: The hidden camera footage exposes the alibi]\n"
            "The footage changes the case."
        )

        delivery = tts._select_section_delivery(
            context,
            channel_emotion="warm",
            channel_speed_profile="slow",
            is_short_episode=False,
        )

        self.assertEqual(delivery["reason"], "climax_title")
        self.assertEqual(delivery["emotion"], "scared")
        self.assertEqual(delivery["speed_profile"], "fast")

    def test_bare_section_marker_keeps_buildup_fallback_arc(self) -> None:
        context = tts._parse_section_context("[SECTION 3]\nThe case gets worse.")

        delivery = tts._select_section_delivery(
            context,
            channel_emotion="calm",
            channel_speed_profile="normal",
            is_short_episode=False,
        )

        self.assertIsNone(context["section_title"])
        self.assertEqual(delivery["reason"], "late_buildup")
        self.assertEqual(delivery["emotion"], "tense")
        self.assertEqual(delivery["speed_profile"], "fast")

    def test_short_episode_ignores_section_title_policy(self) -> None:
        context = tts._parse_section_context(
            "[SECTION 1: The final reveal]\nThis short keeps channel delivery."
        )

        delivery = tts._select_section_delivery(
            context,
            channel_emotion="dramatic",
            channel_speed_profile="fast",
            is_short_episode=True,
        )

        self.assertEqual(delivery["source"], "fallback")
        self.assertEqual(delivery["reason"], "short_episode_static_policy")
        self.assertEqual(delivery["emotion"], "dramatic")
        self.assertEqual(delivery["speed_profile"], "fast")


if __name__ == "__main__":
    unittest.main()
