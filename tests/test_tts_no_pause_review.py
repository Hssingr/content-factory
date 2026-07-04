"""Runtime proof for roadmap 5.2: TTS prep has no Claude pause review.

No external APIs are called. The missing paid ElevenLabs SDK import is stubbed;
real internal TTS normalization, sentence limiting, and deterministic pacing run.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from types import SimpleNamespace


class _VoiceSettingsStub:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=_VoiceSettingsStub))

from app.agents.agent3_audio.services import tts


class TtsNoPauseReviewTest(unittest.TestCase):
    def test_prepare_script_keeps_deterministic_reveal_pacing_without_claude_hook(self) -> None:
        prepared = tts.prepare_script_for_tts(
            "[INTRO]\nHe opened the box. Then I found the missing photo inside the locked drawer.",
            "en",
            "calm",
            tts_model="sonic-2",
        )

        self.assertIn("... Then I found", prepared)
        self.assertNotIn("[INTRO]", prepared)
        self.assertFalse(hasattr(tts, "call_claude"))
        self.assertFalse(hasattr(tts, "_review_pause_marker_placement"))

    def test_prepare_script_source_has_no_paid_pause_review_task(self) -> None:
        source = inspect.getsource(tts.prepare_script_for_tts)

        self.assertIn("_apply_pacing_markers", source)
        self.assertNotIn("call_claude", source)
        self.assertNotIn("pause_marker_review", source)
        self.assertNotIn("_review_pause_marker_placement", source)


if __name__ == "__main__":
    unittest.main()
