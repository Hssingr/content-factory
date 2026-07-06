"""Runtime proof for roadmap Phase 2a (P0-1) —
code_report/forensic_output_audit_borrasca_run.md.

Root cause: a real production run stored ``duration_ms=161724`` for a real
616,835 ms audio file. Chain: ffmpeg was missing on that run, so
``_concat_mp3_chunks()`` fell back to raw byte concatenation of 3 mp3
chunks; ``mutagen.MP3(path).info.length`` then read only the *first
chunk's* header, not the real stream length — corrupting every downstream
timeline (beat budget, timestamp mapping, render length, WPM calibration).

Fix (this phase):
  1. ``_concat_mp3_chunks()`` no longer falls back to raw byte concat at any
     failure point (ffmpeg missing, silence-pad generation failure, concat
     failure) — every one now raises ``RuntimeError`` instead.
  2. Duration is measured via ffprobe (``storage.measure_audio_duration_ms()``),
     which reads the container's real stream duration, not a single frame
     header — used by both fresh generation (``save_audio()``) and the
     on-disk resume path in ``audio.py``.

This file proves both with **real ffmpeg/ffprobe subprocess calls** (local
system binaries, not a paid external API — nothing here is mocked for the
happy-path assertions) plus mocked-failure tests proving every fail-loud
path actually raises instead of silently returning corrupted bytes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))

from app.agents.agent3_audio.services import storage as storage_module
from app.agents.agent3_audio.services import tts as tts_module

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_silent_mp3_bytes(duration_sec: float) -> bytes:
    """Generate a real mp3 blob of exactly ``duration_sec`` seconds via ffmpeg."""
    with TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "chunk.mp3"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", f"{duration_sec:.3f}",
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(out_path),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return out_path.read_bytes()


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg/ffprobe not installed in this environment")
class TestRealFfprobeDurationMeasurement(unittest.TestCase):
    """No mocking — a real ffmpeg-generated file, measured by the real
    ffprobe-backed measure_audio_duration_ms()."""

    def test_measures_real_single_chunk_duration(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "single.mp3"
            path.write_bytes(_make_silent_mp3_bytes(2.0))
            duration_ms = storage_module.measure_audio_duration_ms(path)
        # ffmpeg/lavfi pads slightly; assert within 200ms of the requested length.
        self.assertAlmostEqual(duration_ms, 2000, delta=200)

    def test_raises_on_nonexistent_file(self):
        with self.assertRaises(RuntimeError):
            storage_module.measure_audio_duration_ms(Path("/nonexistent/path/does_not_exist.mp3"))

    def test_raises_on_corrupt_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "garbage.mp3"
            path.write_bytes(b"not a real mp3 file at all")
            with self.assertRaises(RuntimeError):
                storage_module.measure_audio_duration_ms(path)


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg/ffprobe not installed in this environment")
class TestRealMultiChunkConcatMeasuresFullDuration(unittest.TestCase):
    """The P0-1 regression proof: concatenate 3 real chunks with the real
    ffmpeg re-encode path, then measure the result with the real ffprobe
    path — the measured duration must reflect ALL chunks, not just the
    first one (which is exactly what the deleted raw-concat + mutagen
    combination got wrong in production)."""

    def test_concat_duration_reflects_all_chunks_not_just_first(self):
        chunk_durations = [1.0, 2.0, 1.5]
        chunks = [_make_silent_mp3_bytes(d) for d in chunk_durations]

        combined = tts_module._concat_mp3_chunks(chunks)

        with TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "combined.mp3"
            out_path.write_bytes(combined)
            measured_ms = storage_module.measure_audio_duration_ms(out_path)

        boundary_silence_ms = tts_module._CHUNK_BOUNDARY_SILENCE_SECONDS * 1000 * (len(chunks) - 1)
        expected_ms = sum(chunk_durations) * 1000 + boundary_silence_ms

        # Real-world encoder overhead only — nowhere near "only the first
        # chunk" (1000ms), which is the exact corruption this phase fixes.
        self.assertAlmostEqual(measured_ms, expected_ms, delta=300)
        self.assertGreater(measured_ms, chunk_durations[0] * 1000 + 500)


class TestConcatNeverFallsBackToRawByteConcat(unittest.TestCase):
    """Every ffmpeg failure point in _concat_mp3_chunks() must raise, never
    return b"".join(chunks) (the deleted corrupting fallback)."""

    # _concat_mp3_chunks() imports `subprocess` locally (module-scope import
    # would change unrelated behavior) — the local name is bound to the same
    # `subprocess` module object already in sys.modules, so patching the real
    # `subprocess.run` attribute affects the function's local call too.

    def test_ffmpeg_missing_raises(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no ffmpeg on PATH")):
            with self.assertRaises(RuntimeError) as ctx:
                tts_module._concat_mp3_chunks([b"chunk-a", b"chunk-b"])
        self.assertIn("ffmpeg", str(ctx.exception).lower())

    def test_ffmpeg_version_check_nonzero_exit_raises(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["ffmpeg", "-version"]),
        ):
            with self.assertRaises(RuntimeError):
                tts_module._concat_mp3_chunks([b"chunk-a", b"chunk-b"])

    @unittest.skipUnless(_FFMPEG_AVAILABLE, "requires a real ffmpeg for the version check")
    def test_silence_pad_generation_failure_raises(self):
        real_run = subprocess.run

        # Fixture chunks are generated BEFORE the patch is active — the
        # fake_run below intercepts the same "anullsrc" lavfi invocation
        # _concat_mp3_chunks() uses for its own boundary-silence pad, so
        # generating fixtures under the patch would corrupt them too.
        chunks = [_make_silent_mp3_bytes(1.0), _make_silent_mp3_bytes(1.0)]

        def fake_run(cmd, **kwargs):
            if "anullsrc" in " ".join(cmd):
                return SimpleNamespace(returncode=1, stderr=b"synthetic silence-pad failure")
            return real_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                tts_module._concat_mp3_chunks(chunks)
        self.assertIn("silence pad", str(ctx.exception).lower())

    @unittest.skipUnless(_FFMPEG_AVAILABLE, "requires a real ffmpeg for the version/silence steps")
    def test_concat_step_failure_raises(self):
        real_run = subprocess.run
        chunks = [_make_silent_mp3_bytes(1.0), _make_silent_mp3_bytes(1.0)]

        def fake_run(cmd, **kwargs):
            if "-f" in cmd and "concat" in cmd:
                return SimpleNamespace(returncode=1, stderr=b"synthetic concat failure")
            return real_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                tts_module._concat_mp3_chunks(chunks)
        self.assertIn("concat", str(ctx.exception).lower())

    def test_single_chunk_bypasses_ffmpeg_entirely(self):
        # A single chunk never needs concatenation — no subprocess call at all.
        with patch("subprocess.run", side_effect=AssertionError("must not invoke ffmpeg for a single chunk")):
            result = tts_module._concat_mp3_chunks([b"only-one-chunk"])
        self.assertEqual(result, b"only-one-chunk")


if __name__ == "__main__":
    unittest.main()
