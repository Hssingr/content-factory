"""Runtime proof for the fr_main / short_1 loss fixes (never-block follow-up).

Real incident (content 41f7eeb8): one ElevenLabs read timeout on FR section
6/6 killed the whole FR language (5 already-generated sections discarded,
`1/2 language(s) succeeded → AUDIO_DONE`, FR silently gone forever) — and one
fal Schnell call returned a 1072x1536 image for a 1080x1920 portrait request,
whose structural-blocking aspect finding FAILED an entire child Short.

Covers:
- F1: the ElevenLabs client is constructed with an explicit generous timeout.
- F2: `tts._call_tts_with_transport_retry()` — bounded transport retry
  (TypeError propagates immediately — the Cartesia SDK-shape signal).
- F3: `run_audio_generation()`'s language second chance — a TTS-failed
  language is re-queued once at the end of the run; a language that fails
  both attempts logs AUDIO_LANGUAGE_MISSING loudly and the content still
  proceeds (never-block).
- F4: `flux_generator._regenerate_if_wrong_size_once()` — one fresh-cache-key
  re-call when fal returns off-aspect dimensions.

No live API calls (CLAUDE.md §19.1) — only the paid provider boundaries are
stubbed; the internal chains under test run unmodified.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent3_audio.services import audio as audio_module
from app.agents.agent3_audio.services import tts
from app.agents.agent4_visuals.services import flux_generator
from app.config import settings
from app.models import AudioFile, Channel, ChannelVoice, Content, Script


# ── F2: transport-retry helper ───────────────────────────────────────────────

class TransportRetryHelperTest(unittest.TestCase):
    def test_transient_timeouts_are_retried_then_succeed(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("The read operation timed out")
            return b"audio"

        with patch.object(tts.time, "sleep") as mock_sleep:
            out = tts._call_tts_with_transport_retry(
                flaky, provider="elevenlabs", unit_label="SECTION 6",
            )
        self.assertEqual(out, b"audio")
        self.assertEqual(len(calls), 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_type_error_propagates_immediately_no_retry(self):
        """TypeError is the Cartesia SDK request-shape signal — retrying an
        identical malformed call can never succeed."""
        calls = []

        def wrong_shape():
            calls.append(1)
            raise TypeError("unexpected keyword argument 'voice'")

        with self.assertRaises(TypeError):
            tts._call_tts_with_transport_retry(
                wrong_shape, provider="cartesia", unit_label="INTRO",
            )
        self.assertEqual(len(calls), 1)

    def test_exhausted_attempts_reraise_the_last_error(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise ConnectionError("reset by peer")

        with patch.object(tts.time, "sleep"):
            with self.assertRaises(ConnectionError):
                tts._call_tts_with_transport_retry(
                    always_fails, provider="elevenlabs", unit_label="OUTRO",
                )
        self.assertEqual(len(calls), tts._TTS_TRANSPORT_ATTEMPTS)


# ── F1: client timeout ───────────────────────────────────────────────────────

class ElevenLabsClientTimeoutTest(unittest.TestCase):
    def test_client_constructed_with_generous_timeout(self):
        from app.services import elevenlabs_client as ec

        captured = {}

        class _FakeSdk:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        old = ec._client
        try:
            ec._client = None
            with (
                patch.object(ec, "ElevenLabs", _FakeSdk),
                patch.object(settings, "elevenlabs_api_key", "test-key"),
            ):
                ec.get_client()
        finally:
            ec._client = old

        self.assertGreaterEqual(captured.get("timeout", 0), 120.0,
                                "SDK default (~60s) loses long v3 sections — "
                                "a generous explicit timeout is required")


# ── F3: language second chance in run_audio_generation() ────────────────────

class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _FakeDb:
    def __init__(self, *, content_by_id, channel, voices, scripts):
        self._content_by_id = content_by_id
        self.channel = channel
        self.voices = voices
        self.scripts = scripts
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def get(self, model, key):
        if model is Content:
            return self._content_by_id.get(key)
        if model is Channel:
            return self.channel if key == self.channel.id else None
        return None

    def query(self, model):
        if model is ChannelVoice:
            return _FakeQuery(self.voices)
        if model is Script:
            return _FakeQuery(self.scripts)
        return _FakeQuery([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        pass

    def add(self, row):
        self.added.append(row)


class _MissingAudioPath:
    def exists(self):
        return False


def _fixture(content_id, channel):
    content = SimpleNamespace(
        id=content_id, channel_id=channel.id, is_short_episode=False,
        parent_content_id=None, status="SCRIPTS_VALIDATED",
        story_blueprint={"protagonist_gender": "feminine"},
    )
    voices = [
        SimpleNamespace(language=lang, gender="feminine", voice_id=f"v-{lang}",
                        channel_id=channel.id)
        for lang in ("en", "fr")
    ]
    scripts = [
        SimpleNamespace(content_id=content_id, language=lang, version=1, validated=True,
                        voice_script=f"Narration for {lang}.", estimated_duration_sec=None)
        for lang in ("en", "fr")
    ]
    transcript = [{"word": "w", "start": 0.0, "end": 69.5}]
    return content, voices, scripts, transcript


class LanguageSecondChanceTest(unittest.TestCase):
    def _run(self, generate_side_effect, transcript):
        content_id = uuid.uuid4()
        channel = SimpleNamespace(id=uuid.uuid4())
        content, voices, scripts, _ = _fixture(content_id, channel)
        db = _FakeDb(content_by_id={content_id: content}, channel=channel,
                     voices=voices, scripts=scripts)
        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", side_effect=generate_side_effect) as mock_gen,
            patch.object(audio_module, "save_audio", return_value=("audio/x.mp3", 70000)),
            patch.object(audio_module, "transcribe", return_value=transcript),
        ):
            ok = audio_module.run_audio_generation(content_id, db)
        return ok, content, mock_gen

    def test_language_that_fails_once_is_retried_and_succeeds(self):
        """The audited incident shape: FR times out once — the second full
        attempt must recover it, ending 2/2 instead of 1/2."""
        transcript = [{"word": "w", "start": 0.0, "end": 69.5}]
        fr_calls = []

        def flaky(voice_script, voice, is_short_episode=False):
            if voice.voice_id == "v-fr":
                fr_calls.append(1)
                if len(fr_calls) == 1:
                    raise TimeoutError("The read operation timed out")
            return b"mp3", []

        ok, content, mock_gen = self._run(flaky, transcript)
        self.assertTrue(ok)
        self.assertEqual(content.status, "AUDIO_DONE")
        self.assertEqual(len(fr_calls), 2, "FR must get a full second attempt")
        self.assertEqual(mock_gen.call_count, 3)  # en once, fr twice

    def test_language_that_fails_both_attempts_logs_missing_and_proceeds(self):
        transcript = [{"word": "w", "start": 0.0, "end": 69.5}]

        def fr_always_fails(voice_script, voice, is_short_episode=False):
            if voice.voice_id == "v-fr":
                raise TimeoutError("The read operation timed out")
            return b"mp3", []

        with self.assertLogs("app.agents.agent3_audio.services.audio", level="ERROR") as logs:
            ok, content, mock_gen = self._run(fr_always_fails, transcript)

        self.assertTrue(ok, "one missing language must never block the flow")
        self.assertEqual(content.status, "AUDIO_DONE")
        self.assertEqual(mock_gen.call_count, 3)  # en once, fr twice (both failed)
        missing_lines = [l for l in logs.output if "AUDIO_LANGUAGE_MISSING" in l]
        self.assertEqual(len(missing_lines), 1)
        self.assertIn("language=fr", missing_lines[0])


# ── F4: generation-time size gate ────────────────────────────────────────────

def _write_sized_image(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (140, 140, 140)).save(path, "JPEG", quality=90)


class WrongSizeHealthGateTest(unittest.TestCase):
    def _beat(self):
        return {
            "beat_order": 10, "flux_prompt": "a narrow mountain gorge",
            "visual_intent": "a narrow snow-filled mountain gorge from above",
            "environment": "open_landscape", "media_url": "",
        }

    def test_correct_size_passes_without_regeneration(self):
        with TemporaryDirectory() as tmp:
            _write_sized_image(Path(tmp) / "cache" / "ok.jpg", 1080, 1920)
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(flux_generator, "generate_beat_image") as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    self._beat(), "cache/ok.jpg", "cid", width=1080, height=1920,
                )
        self.assertEqual(out, "cache/ok.jpg")
        mock_gen.assert_not_called()

    def test_off_aspect_image_regenerated_with_fresh_cache_key(self):
        """The exact audited anomaly: 1072x1536 (aspect 0.698) returned for a
        1080x1920 (0.562) portrait request. The size defect keeps the prompt
        unchanged on attempt 1 — the fresh cache key alone forces a new call
        (the identical prompt+size would hash back to the cached wrong file)."""
        with TemporaryDirectory() as tmp:
            _write_sized_image(Path(tmp) / "cache" / "wrong.jpg", 1072, 1536)
            _write_sized_image(Path(tmp) / "cache" / "fixed.jpg", 1080, 1920)
            beat = self._beat()
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(
                    flux_generator, "generate_beat_image", return_value="cache/fixed.jpg",
                ) as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    beat, "cache/wrong.jpg", "cid", width=1080, height=1920,
                )
        self.assertEqual(out, "cache/fixed.jpg")
        kwargs = mock_gen.call_args.kwargs
        self.assertIn("heal1:local_image_aspect_mismatch", kwargs["cache_key_extra"],
                      "identical prompt+size would hash back to the same cached "
                      "wrong artifact — a fresh cache key is mandatory")
        self.assertEqual(kwargs["width"], 1080)
        self.assertEqual(kwargs["height"], 1920)
        # size defect → attempt 1 keeps the beat's own prompt unchanged
        self.assertEqual(mock_gen.call_args.args[0], "a narrow mountain gorge")

    def test_still_wrong_after_both_attempts_hands_off_to_neighbor_fill(self):
        with TemporaryDirectory() as tmp:
            _write_sized_image(Path(tmp) / "cache" / "wrong.jpg", 1072, 1536)
            _write_sized_image(Path(tmp) / "cache" / "wrong2.jpg", 1024, 1024)
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(
                    flux_generator, "generate_beat_image", return_value="cache/wrong2.jpg",
                ) as mock_gen,
            ):
                out = flux_generator._ensure_beat_image_healthy(
                    self._beat(), "cache/wrong.jpg", "cid", width=1080, height=1920,
                )
        self.assertEqual(out, "")
        self.assertEqual(mock_gen.call_count, 2,
                         "corrective attempt + rewrite attempt, then neighbor-fill")

    def test_aspect_mismatch_is_now_a_repairable_code(self):
        from app.agents.agent4_visuals.services.media_validation import REPAIRABLE_MEDIA_CODES
        self.assertIn("local_image_aspect_mismatch", REPAIRABLE_MEDIA_CODES)


if __name__ == "__main__":
    unittest.main()
