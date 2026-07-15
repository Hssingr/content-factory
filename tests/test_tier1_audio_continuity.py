"""Runtime proof for pre-next-test roadmap Tier 1 (R1–R3) — audio continuity.

A real production run (content 2704ad21, "Hannibal at the Alps") measured 31%
of a 12:40 video as near-digital silence: 219 gaps ≥0.5s, median 1.10s —
paragraph-per-sentence scripts × ElevenLabs v3 per-utterance pauses ×
Agent 3 pacing-tag stacking, with no pause bound anywhere.

- R1: `tts._compress_interior_silence()` caps interior silences per TTS chunk
  (real ffmpeg throughout — synthesizes audio with a known speech/silence
  layout and independently re-measures the result; never mocks the effect).
- R2: `_TTS_SHARED_CORE`'s blank-line rule inverted (static prompt proof).
- R3: `_apply_pacing_markers()` no longer stacks the generic slow-open
  `[dramatic pause]` onto a section that already carries an authored v3 tag.

Per CLAUDE.md §19.1: no live API calls — the only faked boundary is the paid
cartesia SDK (the integration test's WAVs are real, locally synthesized).
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent3_audio.services import tts
from app.config import settings


def _make_pattern_mp3(segments: list[tuple[str, float]], freq: float = 440.0) -> bytes:
    """Build a real MP3 from a (kind, seconds) pattern: kind is 'tone' or 'silence'."""
    parts = []
    for kind, secs in segments:
        if kind == "tone":
            parts.append(f"sine=frequency={freq}:duration={secs}")
        else:
            parts.append(f"anullsrc=r=44100:cl=mono,atrim=duration={secs}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pattern.mp3"
        inputs: list[str] = []
        filters: list[str] = []
        for i, expr in enumerate(parts):
            inputs += ["-f", "lavfi", "-i", expr.split(",")[0]]
            if expr.startswith("anullsrc"):
                filters.append(f"[{i}:a]atrim=duration={segments[i][1]}[s{i}]")
            else:
                filters.append(f"[{i}:a]acopy[s{i}]")
        concat_in = "".join(f"[s{i}]" for i in range(len(parts)))
        filter_complex = ";".join(filters) + f";{concat_in}concat=n={len(parts)}:v=0:a=1[out]"
        subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex,
             "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "192k", str(path)],
            check=True, capture_output=True,
        )
        return path.read_bytes()


def _duration_s(mp3_bytes: bytes) -> float:
    return tts._measure_mp3_bytes_duration_ms(mp3_bytes) / 1000.0


# ── R1: _compress_interior_silence() unit behavior ──────────────────────────

class CompressInteriorSilenceTest(unittest.TestCase):
    def test_long_interior_silence_capped_to_configured_max(self):
        """The audited defect shape: speech, a >1s dead gap, speech —
        the gap must come out at ~the 650ms cap, the speech untouched."""
        blob = _make_pattern_mp3([("tone", 2.0), ("silence", 2.0), ("tone", 2.0)])
        self.assertAlmostEqual(_duration_s(blob), 6.0, delta=0.25)

        with self.assertLogs("app.agents.agent3_audio.services.tts", level="INFO") as logs:
            out = tts._compress_interior_silence(blob)

        out_s = _duration_s(out)
        # 2.0 + ~0.65 + 2.0 — generous encoder/window slop either side.
        self.assertGreater(out_s, 4.3)
        self.assertLess(out_s, 5.2)
        self.assertTrue(
            any("AUDIO_SILENCE_COMPRESSED" in line for line in logs.output),
            f"expected AUDIO_SILENCE_COMPRESSED log marker, got: {logs.output}",
        )

    def test_multiple_long_silences_all_capped(self):
        blob = _make_pattern_mp3(
            [("tone", 1.0), ("silence", 1.5), ("tone", 1.0), ("silence", 2.0), ("tone", 1.0)]
        )
        in_s = _duration_s(blob)
        self.assertAlmostEqual(in_s, 6.5, delta=0.25)
        out = tts._compress_interior_silence(blob)
        out_s = _duration_s(out)
        # 3.0 speech + 2 × ~0.65 kept silence ≈ 4.3s; ≥1.8s removed.
        self.assertGreater(in_s - out_s, 1.8)
        self.assertGreater(out_s, 3.9)   # speech content is never eaten

    def test_short_silence_untouched_and_skips_reencode(self):
        """A sub-cap pause is normal sentence rhythm — the pre-pass must
        return the input object unchanged (no re-encode generation loss)."""
        blob = _make_pattern_mp3([("tone", 1.0), ("silence", 0.4), ("tone", 1.0)])
        out = tts._compress_interior_silence(blob)
        self.assertIs(out, blob)

    def test_disabled_by_config_returns_input_object(self):
        blob = _make_pattern_mp3([("tone", 0.5), ("silence", 2.0), ("tone", 0.5)])
        with patch.object(settings, "audio_max_interior_silence_ms", 0):
            self.assertIs(tts._compress_interior_silence(blob), blob)

    def test_empty_input_returns_empty(self):
        self.assertEqual(tts._compress_interior_silence(b""), b"")

    def test_undecodable_input_raises_runtime_error(self):
        """Fail loud, never silently ship unprocessed audio (same contract
        as _normalize_loudness/_concat_mp3_chunks)."""
        with self.assertRaises(RuntimeError):
            tts._compress_interior_silence(b"this is not an mp3 stream")


# ── R1: generate_audio() integration — compression BEFORE boundaries ────────

class _FakeTTSWithSilence:
    """Fake cartesia client whose WAVs carry a real interior dead gap:
    1.0s tone + 2.0s digital silence + 1.0s tone per section."""

    def __init__(self):
        self.calls: list[dict] = []

    def bytes(self, **kwargs):
        self.calls.append(kwargs)
        sample_rate = 22_050
        frames = bytearray()
        for kind, secs in (("tone", 1.0), ("silence", 2.0), ("tone", 1.0)):
            n = int(sample_rate * secs)
            for i in range(n):
                if kind == "tone":
                    sample = int(12_000 * math.sin(2.0 * math.pi * 330.0 * i / sample_rate))
                else:
                    sample = 0
                frames.extend(struct.pack("<h", sample))
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            with wave.open(tmp.name, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(bytes(frames))
            return Path(tmp.name).read_bytes()


class GenerateAudioSilenceCompressionIntegrationTest(unittest.TestCase):
    def test_compression_runs_before_section_boundaries_are_measured(self):
        """The critical ordering (CLAUDE.md §19.4 data-flow proof): chunks are
        compressed BEFORE concat and BEFORE _compute_section_boundaries(), so
        each persisted section span reflects the compressed audio — not the
        raw 4.0s the provider returned."""
        fake = _FakeTTSWithSilence()
        sys.modules["cartesia"] = SimpleNamespace(
            Cartesia=lambda api_key=None: SimpleNamespace(tts=fake)
        )
        sys.modules["cartesia.tts"] = SimpleNamespace(
            TTS=SimpleNamespace(bytes=lambda self, **kwargs: b"")
        )

        voice = SimpleNamespace(
            provider="cartesia", voice_id="voice-test", tts_model="sonic-2",
            emotion="dramatic", speed_profile="normal", speed_override=None,
            cartesia_pronunciation_dict_id=None,
        )
        script = "\n".join([
            "[INTRO]",
            "The recorder started on its own.",
            "[OUTRO]",
            "The house was silent after that.",
        ])

        audio_bytes, boundaries = tts.generate_audio(script, voice, is_short_episode=False)

        self.assertEqual(len(fake.calls), 2)
        final_ms = tts._measure_mp3_bytes_duration_ms(audio_bytes)
        # Two 4.0s chunks compressed to ~2.65s each + 0.08s pad ≈ 5.4s —
        # raw (uncompressed) would be ~8.1s.
        self.assertLess(final_ms, 6_400, "final audio still carries the uncompressed gaps")
        self.assertGreater(final_ms, 4_600, "compression must never eat speech content")

        self.assertEqual(len(boundaries), 2)
        self.assertEqual(boundaries[0]["start_ms"], 0)
        self.assertEqual(boundaries[-1]["end_ms"], final_ms)
        self.assertEqual(boundaries[0]["end_ms"], boundaries[1]["start_ms"])
        for b in boundaries:
            span = b["end_ms"] - b["start_ms"]
            self.assertLess(span, 3_300, f"section span {span}ms reflects RAW chunk duration")
            self.assertGreater(span, 2_200)


# ── R3: slow-open suppression when the section carries an authored tag ──────

class SlowOpenAuthoredTagSuppressionTest(unittest.TestCase):
    def test_slow_open_still_prepended_without_authored_tag(self):
        text = "The house sat empty for years. Nobody asked why the lights stayed on."
        out = tts._apply_pacing_markers(text, tone="dramatic", tts_model="eleven_v3")
        self.assertTrue(out.lstrip().startswith("[dramatic pause]"))

    def test_authored_tag_anywhere_suppresses_generic_slow_open(self):
        text = (
            "The house sat empty for years. "
            "[whispers] Nobody asked why the lights stayed on."
        )
        out = tts._apply_pacing_markers(text, tone="dramatic", tts_model="eleven_v3")
        self.assertFalse(
            out.lstrip().startswith("[dramatic pause]"),
            "generic slow-open must not stack onto a section with an authored tag",
        )
        self.assertIn("[whispers]", out)

    def test_authored_dramatic_pause_also_suppresses_slow_open(self):
        text = (
            "He opened the letter slowly. "
            "[dramatic pause] The name inside was his own."
        )
        out = tts._apply_pacing_markers(text, tone="dramatic", tts_model="eleven_v3")
        self.assertFalse(out.lstrip().startswith("[dramatic pause] He opened"))
        self.assertEqual(out.count("[dramatic pause]"), 1)

    def test_non_v3_ellipsis_slow_open_unaffected(self):
        """R3 is v3-specific — the sonic-2 ellipsis path keeps its behavior."""
        text = "The house sat empty. Nobody asked why the lights stayed on for years."
        out = tts._apply_pacing_markers(text, tone="dramatic", tts_model="sonic-2")
        self.assertIn("...", out)

    def test_prepare_script_for_tts_end_to_end_suppression(self):
        """Through the real public entrypoint, not just the helper."""
        script = (
            "The house sat empty for years. "
            "[whispers] Nobody asked why the lights stayed on."
        )
        out = tts.prepare_script_for_tts(
            script, language="en", tone="dramatic", tts_model="eleven_v3",
        )
        self.assertFalse(out.lstrip().startswith("[dramatic pause]"))


# ── R2: prompt density rule (static proof) ───────────────────────────────────

class ParagraphDensityPromptTest(unittest.TestCase):
    def test_old_blank_line_per_beat_rule_is_gone(self):
        from app.agents.agent2_discovery import system_prompt as sp
        self.assertNotIn("One blank line between narrative beats", sp._TTS_SHARED_CORE)

    def test_new_density_rule_present_in_every_tts_block(self):
        from app.agents.agent2_discovery import system_prompt as sp
        self.assertIn("ONLY at a genuine scene", sp._TTS_SHARED_CORE)
        self.assertIn("3-6", sp._TTS_SHARED_CORE)
        for model_key, block in sp.TTS_BLOCK.items():
            self.assertIn(
                "ONLY at a genuine scene", block,
                f"TTS_BLOCK[{model_key!r}] lost the shared density rule",
            )

    def test_prompt_version_bumped(self):
        from app.agents.agent2_discovery import system_prompt as sp
        # ≥ 5.1 (the R2 bump) — later tiers may bump further; this test must
        # not pin an exact version it does not own.
        self.assertGreaterEqual(float(sp.PROMPT_VERSION), 5.1)


if __name__ == "__main__":
    unittest.main()
