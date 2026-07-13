"""Runtime proof for loudness normalization (roadmap Phase A3, operator
video-output audit): a real production run measured a parent's EN narration
at -22.4 LUFS against the SAME content's FR narration at -18.3 LUFS — no
loudness normalization existed anywhere in the pipeline.

Uses REAL ffmpeg (a hard dependency of this codebase already, per
CLAUDE.md §10.3/§27.3) for both generating test audio and measuring the
actual resulting loudness via a second, independent ``ebur128`` analysis
pass — not a mock of the normalization effect. Only the paid provider
boundaries (``_generate_cartesia_audio`` / the ElevenLabs client, mirroring
the existing pattern in ``test_elevenlabs_v3_section_delivery.py``) are
stubbed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Same VoiceSettings-stub force-replace convention as
# test_elevenlabs_v3_section_delivery.py — other test modules register a
# plain object stub via setdefault, and whichever import wins first in a
# full-suite run sticks; reload tts against the real stub if needed.
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))

from app.agents.agent3_audio.services import tts


def _tone_mp3(*, volume_db: float, duration: float = 2.0) -> bytes:
    """A real MP3 encoding a 440Hz tone at a controlled, real loudness."""
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "tone.mp3"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                "-af", f"volume={volume_db}dB",
                "-c:a", "libmp3lame", "-b:a", "128k", str(output),
            ],
            check=True, capture_output=True,
        )
        return output.read_bytes()


def _measure_integrated_loudness(mp3_bytes: bytes) -> float:
    """Real, independent ffmpeg ebur128 measurement — not the code under test."""
    with tempfile.TemporaryDirectory() as tmp:
        input_path = Path(tmp) / "measure.mp3"
        input_path.write_bytes(mp3_bytes)
        proc = subprocess.run(
            ["ffmpeg", "-i", str(input_path), "-af", "ebur128", "-f", "null", "-"],
            capture_output=True,
        )
        stderr = proc.stderr.decode(errors="replace")
        match = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+\.?\d*) LUFS", stderr)
        if not match:
            raise AssertionError(f"could not parse integrated loudness from ffmpeg output:\n{stderr}")
        return float(match.group(1))


class NormalizeLoudnessUnitTest(unittest.TestCase):
    def test_empty_input_returns_empty_output(self) -> None:
        self.assertEqual(tts._normalize_loudness(b""), b"")

    def test_quiet_real_audio_is_normalized_toward_target(self) -> None:
        quiet = _tone_mp3(volume_db=-35)
        quiet_loudness = _measure_integrated_loudness(quiet)
        # Sanity: our synthetic fixture really is much quieter than the target.
        self.assertLess(quiet_loudness, tts._LOUDNORM_TARGET_I_LUFS - 15)

        normalized = tts._normalize_loudness(quiet)
        normalized_loudness = _measure_integrated_loudness(normalized)

        # Single-pass loudnorm is not exact — allow a few LU of tolerance —
        # but it must land close to the configured target, not stay quiet.
        self.assertAlmostEqual(normalized_loudness, tts._LOUDNORM_TARGET_I_LUFS, delta=2.0)

    def test_already_loud_real_audio_is_pulled_down_toward_target(self) -> None:
        # A 440Hz full-scale sine measures well below 0 LUFS under K-weighting
        # (empirically ~-22.2 LUFS at 0dB applied gain) — +12dB pushes it
        # comfortably above target+5 without needing destructive clipping.
        loud = _tone_mp3(volume_db=12)
        loud_loudness = _measure_integrated_loudness(loud)
        self.assertGreater(loud_loudness, tts._LOUDNORM_TARGET_I_LUFS + 5)

        normalized = tts._normalize_loudness(loud)
        normalized_loudness = _measure_integrated_loudness(normalized)
        self.assertAlmostEqual(normalized_loudness, tts._LOUDNORM_TARGET_I_LUFS, delta=2.0)

    def test_ffmpeg_failure_raises_runtime_error(self) -> None:
        # Build the fixture with the REAL subprocess.run first — only the
        # call inside _normalize_loudness() itself should see the failure.
        fixture = _tone_mp3(volume_db=-20, duration=0.2)
        fake_result = SimpleNamespace(returncode=1, stderr=b"ffmpeg: fake failure for this test")
        with patch("subprocess.run", return_value=fake_result):
            with self.assertRaises(RuntimeError):
                tts._normalize_loudness(fixture)


class GenerateAudioCartesiaWiringTest(unittest.TestCase):
    """Proves generate_audio()'s Cartesia branch actually routes its result
    through the real _normalize_loudness() — not just that some bytes come
    back unchanged."""

    def test_cartesia_path_output_is_really_normalized(self) -> None:
        quiet = _tone_mp3(volume_db=-35)
        channel_voice = SimpleNamespace(provider="cartesia")

        with patch.object(
            tts, "_generate_cartesia_audio", return_value=(quiet, [], []),
        ) as mocked:
            result, section_boundaries = tts.generate_audio("irrelevant script text", channel_voice)

        mocked.assert_called_once()
        result_loudness = _measure_integrated_loudness(result)
        self.assertAlmostEqual(result_loudness, tts._LOUDNORM_TARGET_I_LUFS, delta=2.0)
        # And it's provably not just the raw input passed through untouched.
        self.assertNotEqual(result, quiet)
        # No section metadata was supplied by the (stubbed) generator, so no
        # boundaries can be computed — never invented from nothing.
        self.assertEqual(section_boundaries, [])


class GenerateAudioElevenLabsWiringTest(unittest.TestCase):
    """Same proof for the ElevenLabs branch, mirroring the existing fake-client
    boundary pattern in test_elevenlabs_v3_section_delivery.py."""

    def test_elevenlabs_non_v3_path_output_is_really_normalized(self) -> None:
        quiet = _tone_mp3(volume_db=-35)

        class _FakeConvert:
            def convert(self, **kwargs):
                return [quiet]

        class _FakeClient:
            text_to_speech = _FakeConvert()

        class _Voice:
            provider = "elevenlabs"
            tts_model = "eleven_multilingual_v2"
            voice_id = "voice_test"
            emotion = "neutral"
            speed_profile = "normal"
            v3_stability_preset = "natural"
            stability_override = None
            similarity_override = None
            style_override = None
            speed_override = None
            use_speaker_boost = True

        original_get_client = tts.get_client
        tts.get_client = lambda: _FakeClient()
        try:
            result, section_boundaries = tts.generate_audio(
                "A short single-section narration line for this test.",
                _Voice(),
                is_short_episode=False,
            )
        finally:
            tts.get_client = original_get_client

        result_loudness = _measure_integrated_loudness(result)
        self.assertAlmostEqual(result_loudness, tts._LOUDNORM_TARGET_I_LUFS, delta=2.0)
        self.assertNotEqual(result, quiet)
        # Non-v3 ElevenLabs char-limit chunking can't guarantee one chunk
        # per section (roadmap Phase B1) — no boundaries computed.
        self.assertEqual(section_boundaries, [])


if __name__ == "__main__":
    unittest.main()
