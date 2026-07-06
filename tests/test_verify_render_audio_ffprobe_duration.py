"""Runtime proof for roadmap Phase 2c (P2-5, P2-6) —
code_report/forensic_output_audit_borrasca_run.md.

P2-5: `verify_render` used to validate a render's duration against
`audio.duration_ms` — the exact DB value this check exists to audit. Had
ffprobe been installed on the audited run, the 161.7s corrupted video would
have **passed** verification, because the "expected" duration was the same
corrupted number. The fix: `verify_render()` now measures the expected
duration directly via ffprobe on the source audio file, never a DB column.

P2-6: Shorts previously passed `expected_duration_ms=None` unconditionally
("no bookend padding"), disabling the one deterministic check that would
catch a child timeline corruption on the format with the least margin for
error. The fix: Shorts now pass a real `audio_file_path` too.

This file proves:
1. `verify_render()`'s signature no longer accepts any duration value at
   all — only a source audio file path (structural proof: there is no way
   for a caller to pass a DB value even by mistake).
2. `_check_ffprobe()` measures duration by shelling out to ffprobe on the
   audio file path it is given (mocked-subprocess proof, matching this
   suite's existing `test_block_6_render.py` convention).
3. A **real** ffmpeg-generated audio file's duration is correctly measured
   by the same `_probe_duration_sec()` helper `_check_ffprobe()` now reuses
   for the audio side — no mocking, a real local ffprobe subprocess call.
4. `_run_short_render()` (the actual Short render call site) now passes a
   real `audio_file_path` to `verify_render()` instead of `None` — the
   concrete P2-6 regression proof.
"""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent5_render.services import verify as verify_module
from app.agents.agent5_render.services import video as video_module

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _ffprobe_result(stdout: str, returncode: int = 0) -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = ""
    return r


def _ffmpeg_result(stderr: str = "", returncode: int = 0) -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = ""
    r.stderr = stderr
    return r


class TestVerifyRenderSignatureNeverAcceptsADurationValue(unittest.TestCase):
    def test_second_parameter_is_audio_file_path_not_a_duration(self):
        params = list(inspect.signature(verify_module.verify_render).parameters)
        self.assertEqual(params, ["mp4_path", "audio_file_path", "fmt"])

    def test_check_ffprobe_second_parameter_is_audio_file_path(self):
        params = list(inspect.signature(verify_module._check_ffprobe).parameters)
        self.assertEqual(params, ["mp4_path", "audio_file_path", "fmt"])


class TestCheckFfprobeMeasuresAudioFileNotDbValue(unittest.TestCase):
    """Mocked-subprocess proof, same convention as test_block_6_render.py:
    the audio-file duration is a real subprocess.run(["ffprobe", ...,
    audio_file_path]) call, not a value read off any object."""

    def test_audio_file_path_is_the_actual_ffprobe_target(self):
        good_mp4_json = json.dumps({
            "format": {"duration": "120.5"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"},
            ],
        })
        captured_cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            if cmd[0] == "ffprobe" and "-show_streams" in cmd:
                return _ffprobe_result(good_mp4_json)
            if cmd[0] == "ffprobe":
                return _ffprobe_result("120.5\n")
            return _ffmpeg_result("")

        audio_path = "/media/audio/some-content-id/en.mp3"
        with patch("subprocess.run", side_effect=fake_run):
            issues = verify_module._check_ffprobe("/media/video/x.mp4", audio_path, "main")

        self.assertEqual(issues, [])
        audio_probe_cmds = [c for c in captured_cmds if audio_path in c]
        self.assertEqual(len(audio_probe_cmds), 1, captured_cmds)

    def test_audio_probe_failure_is_a_blocking_issue_not_silently_skipped(self):
        good_mp4_json = json.dumps({
            "format": {"duration": "120.5"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio"},
            ],
        })

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe" and "-show_streams" in cmd:
                return _ffprobe_result(good_mp4_json)
            if cmd[0] == "ffprobe":
                return _ffprobe_result("")  # ffprobe returns nothing usable
            return _ffmpeg_result("")

        with patch("subprocess.run", side_effect=fake_run):
            issues = verify_module._check_ffprobe(
                "/media/video/x.mp4", "/media/audio/missing/en.mp3", "main",
            )

        self.assertTrue(any("audio_duration_probe_failed" in i for i in issues), issues)


@unittest.skipUnless(_FFMPEG_AVAILABLE, "ffmpeg/ffprobe not installed in this environment")
class TestRealFfprobeMeasuresRealAudioFile(unittest.TestCase):
    """No mocking — _probe_duration_sec() (the helper _check_ffprobe() now
    reuses for the audio side) shells out to a real, local ffprobe on a real,
    ffmpeg-generated audio file. This is the literal "ffprobe(audio file)"
    ground truth the roadmap item names."""

    def test_real_audio_file_duration_measured_correctly(self):
        with TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "real_audio.mp3"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "3.0",
                    "-c:a", "libmp3lame", "-b:a", "128k",
                    str(audio_path),
                ],
                capture_output=True, check=True,
            )
            measured_sec = verify_module._probe_duration_sec(str(audio_path))

        self.assertAlmostEqual(measured_sec, 3.0, delta=0.2)


class TestShortRenderNowPassesRealAudioFilePath(unittest.TestCase):
    """P2-6 regression proof: _run_short_render() must call verify_render()
    with a real audio_file_path — not None — so Shorts get the same
    duration-drift check parent renders always got."""

    def test_run_short_render_passes_audio_file_path_not_none(self):
        content_id = uuid.uuid4()
        audio = SimpleNamespace(duration_ms=66_000, file_path="/media/audio/child-id/en.mp3")
        db = MagicMock()

        captured_kwargs = {}

        def fake_verify_render(**kwargs):
            captured_kwargs.update(kwargs)
            return []  # no issues

        with (
            patch.object(video_module, "ensure_bundle", return_value=None),
            patch.object(
                video_module, "render_short",
                return_value={"file_path": "/media/video/child-id/en_short_0.mp4",
                               "duration_seconds": 66.0, "render_time_seconds": 5.0},
            ),
            patch.object(video_module.settings, "verify_renders", True),
            patch.object(video_module, "verify_render", side_effect=fake_verify_render),
        ):
            video_module._run_short_render(
                content_id=content_id, language="en", cid_str=str(content_id),
                audio=audio, short_order=0, props_path="/media/props/child_en_short_0.json",
                db=db,
            )

        self.assertEqual(captured_kwargs.get("audio_file_path"), audio.file_path)
        self.assertIsNotNone(captured_kwargs.get("audio_file_path"))
        self.assertNotIn("expected_duration_ms", captured_kwargs)


if __name__ == "__main__":
    unittest.main()
