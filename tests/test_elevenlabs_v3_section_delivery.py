"""Runtime proof for roadmap 6 phase 4d / audit P1-8.

No live ElevenLabs call is made: the paid provider client is replaced with a
local fake at the external API boundary only. Internal section splitting,
delivery selection, TTS preparation, v3 tag insertion, VoiceSettings building,
and ffmpeg chunk concatenation all run for real.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class _VoiceSettingsStub:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


# Force-replace (not setdefault): other test modules register
# VoiceSettings=object via setdefault, and in a full-suite run whichever file
# imports first wins. This test asserts on VoiceSettings kwargs, so it needs
# the inspectable stub regardless of collection order — and tts must be
# (re)loaded against it if it was already imported with the plain stub.
sys.modules["elevenlabs"] = SimpleNamespace(ElevenLabs=object)
sys.modules["elevenlabs.types"] = SimpleNamespace(VoiceSettings=_VoiceSettingsStub)

import importlib

from app.agents.agent3_audio.services import tts

if getattr(tts, "VoiceSettings", None) is not _VoiceSettingsStub:
    tts = importlib.reload(tts)


class _FakeConvert:
    def __init__(self, mp3_bytes: bytes):
        self.calls: list[dict] = []
        self._mp3_bytes = mp3_bytes

    def convert(self, **kwargs):
        self.calls.append(kwargs)
        return [self._mp3_bytes]


class _FakeClient:
    def __init__(self, mp3_bytes: bytes):
        self.text_to_speech = _FakeConvert(mp3_bytes)


class _Voice:
    provider = "elevenlabs"
    tts_model = "eleven_v3"
    voice_id = "voice_test"
    emotion = "dramatic"
    speed_profile = "normal"
    v3_stability_preset = "natural"
    stability_override = None
    similarity_override = None
    style_override = None
    speed_override = None
    use_speaker_boost = True


def _tiny_mp3() -> bytes:
    # A very quiet real tone, not pure digital silence: generate_audio()'s
    # result now always passes through the real ffmpeg loudnorm pass
    # (roadmap Phase A3) — libmp3lame's psymodel has a known assertion
    # failure ("calc_energy: el >= 0") when loudnorm is asked to normalize
    # a literal all-zero-amplitude source, which pure `anullsrc` silence is.
    # This test exercises per-section delivery tags/voice settings, not
    # silence-input robustness, so a quiet-but-real tone is the correct
    # fixture rather than a workaround.
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "tiny.mp3"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=0.05",
                "-af", "volume=-40dB",
                "-c:a", "libmp3lame", "-b:a", "128k", str(output),
            ],
            check=True,
            capture_output=True,
        )
        return output.read_bytes()


class ElevenLabsV3SectionDeliveryTest(unittest.TestCase):
    def test_static_wiring_uses_v3_section_delivery_not_legacy_chunk_delivery(self):
        source = inspect.getsource(tts.generate_audio)
        self.assertIn('if model_id == "eleven_v3"', source)
        self.assertIn("_split_script_into_section_units(voice_script)", source)
        self.assertIn("_select_section_delivery(", source)
        self.assertIn("_with_elevenlabs_v3_delivery_tag", source)
        self.assertIn("emotion_override=delivery", source)

    def test_elevenlabs_v3_uses_per_section_tags_and_voice_settings(self):
        fake_client = _FakeClient(_tiny_mp3())
        original_get_client = tts.get_client
        tts.get_client = lambda: fake_client
        try:
            audio, section_boundaries = tts.generate_audio(
                "\n".join([
                    "[INTRO]",
                    "He heard the sound again. Then he found the missing photo inside the locked drawer.",
                    "[SECTION 1: First footsteps]",
                    "The hallway went quiet. Then she discovered the second key under the carpet.",
                    "[SECTION 3: The Reveal]",
                    "The truth was worse. Then they found my name on the hidden police tape.",
                    "[OUTRO]",
                    "Nobody spoke after that. The house finally felt empty.",
                ]),
                _Voice(),
                is_short_episode=False,
            )
        finally:
            tts.get_client = original_get_client

        calls = fake_client.text_to_speech.calls
        self.assertGreater(len(audio), 0)
        self.assertEqual(len(calls), 4)

        # roadmap Phase B1: eleven_v3 is always one chunk per section, so
        # real per-section boundaries must come back — 4 sections, in
        # order, covering [0, final_duration_ms) with no gaps/overlaps.
        self.assertEqual(len(section_boundaries), 4)
        self.assertEqual(
            [(s["section_type"], s["section_index"]) for s in section_boundaries],
            [("intro", None), ("body", 1), ("body", 3), ("outro", None)],
        )
        for prev, nxt in zip(section_boundaries, section_boundaries[1:]):
            self.assertEqual(prev["end_ms"], nxt["start_ms"])
        self.assertEqual(section_boundaries[0]["start_ms"], 0)
        self.assertTrue(calls[0]["text"].startswith("[whispers]"), calls[0]["text"])
        self.assertTrue(calls[1]["text"].startswith("[whispers]"), calls[1]["text"])
        self.assertTrue(calls[2]["text"].startswith("[gasps]"), calls[2]["text"])
        self.assertTrue(calls[3]["text"].startswith("[sighs]"), calls[3]["text"])
        self.assertNotIn("previous_text", calls[1])
        self.assertNotIn("next_text", calls[1])

        speeds = [call["voice_settings"].kwargs["speed"] for call in calls]
        styles = [call["voice_settings"].kwargs["style"] for call in calls]
        self.assertGreater(speeds[2], speeds[3])
        self.assertGreater(styles[2], styles[3])


if __name__ == "__main__":
    unittest.main()
