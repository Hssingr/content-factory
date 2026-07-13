"""Runtime proof for roadmap Phase 2b (P0-2) —
code_report/forensic_output_audit_borrasca_run.md.

Three independent timelines flow into a render: (a) ``AudioFile.duration_ms``,
(b) Whisper word timestamps, (c) ``VideoSection``/caption spans built from
them. A real production run shipped with (a)=161.7s, (b)=616.6s, (c)=161.7s
and zero checks comparing them — the corrupted duration silently poisoned
every downstream timeline.

This phase adds two mirrored 2%-tolerance invariants:
  1. Agent 3 (``audio._assert_duration_transcript_alignment()``) — fails
     (rolls back, does not persist) a language whose ``duration_ms`` and
     Whisper transcript's last word end disagree by more than 2%.
  2. The props builder (``remotion_builder._assert_timeline_alignment()``) —
     raises before writing a props file whose last caption end or last
     section end disagrees from ``duration_ms`` by more than 2%.

This file proves both with real function calls — only the paid/external
generation calls (TTS, Whisper, Flux) are stubbed; the assertion functions
and the real ``build_main_props()``/``build_short_props()`` writers run for
real (local file I/O only, no external API).
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent3_audio.services import audio as audio_module
from app.agents.agent5_render.services import remotion_builder
from app.config import settings
from app.models import AudioFile, Channel, ChannelVoice, Content, Script


# ── Agent 3: _assert_duration_transcript_alignment() ─────────────────────────

class TestAssertDurationTranscriptAlignment(unittest.TestCase):
    def test_aligned_within_tolerance_passes(self):
        ok = audio_module._assert_duration_transcript_alignment(
            content_id=uuid.uuid4(), language="en",
            duration_ms=61_000,
            transcript=[{"word": "x", "start": 0.0, "end": 60.9}],
        )
        self.assertTrue(ok)

    def test_drift_over_tolerance_fails_and_logs(self):
        with self.assertLogs("app.agents.agent3_audio.services.audio", level="ERROR") as logs:
            ok = audio_module._assert_duration_transcript_alignment(
                content_id=uuid.uuid4(), language="en",
                duration_ms=161_724,
                transcript=[{"word": "x", "start": 0.0, "end": 616.58}],
            )
        self.assertFalse(ok)
        self.assertTrue(any("AUDIO_DURATION_TRANSCRIPT_MISMATCH" in m for m in logs.output))

    def test_empty_transcript_is_a_no_op_pass(self):
        # Parent long-form content tolerates a missing transcript entirely —
        # this check must not invent a failure where none existed before.
        ok = audio_module._assert_duration_transcript_alignment(
            content_id=uuid.uuid4(), language="en", duration_ms=161_724, transcript=[],
        )
        self.assertTrue(ok)

    def test_exactly_at_boundary_passes(self):
        # 2.0% drift is the tolerance itself — must not be stricter than documented.
        duration_ms = 100_000
        last_word_end_sec = (duration_ms * 1.02) / 1000
        ok = audio_module._assert_duration_transcript_alignment(
            content_id=uuid.uuid4(), language="en", duration_ms=duration_ms,
            transcript=[{"word": "x", "start": 0.0, "end": last_word_end_sec}],
        )
        self.assertTrue(ok)

    def test_just_over_boundary_fails(self):
        duration_ms = 100_000
        last_word_end_sec = (duration_ms * 1.03) / 1000
        ok = audio_module._assert_duration_transcript_alignment(
            content_id=uuid.uuid4(), language="en", duration_ms=duration_ms,
            transcript=[{"word": "x", "start": 0.0, "end": last_word_end_sec}],
        )
        self.assertFalse(ok)


# ── Agent 3: real run_audio_generation() chain, fixture pattern shared with
# test_short_empty_whisper_blocker.py ─────────────────────────────────────────

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
    def __init__(self, *, content, channel, voices, scripts_by_lang):
        self.content = content
        self.channel = channel
        self.voices = voices
        self.scripts = list(scripts_by_lang.values())
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def get(self, model, key):
        if model is Content:
            return self.content if key == self.content.id else None
        if model is Channel:
            return self.channel if key == self.channel.id else None
        return None

    def query(self, model):
        if model is ChannelVoice:
            return _FakeQuery(self.voices)
        if model is Script:
            return _FakeQuery(self.scripts)
        if model is AudioFile:
            return _FakeQuery([])
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


def _build_content_channel_script(*, is_short_episode: bool):
    content_id = uuid.uuid4()
    content = SimpleNamespace(
        id=content_id, channel_id=uuid.uuid4(), is_short_episode=is_short_episode,
        parent_content_id=uuid.uuid4() if is_short_episode else None,
        status="SCRIPTS_VALIDATED", story_blueprint=None,
    )
    channel = SimpleNamespace(id=content.channel_id)
    voice = SimpleNamespace(language="en", voice_id="voice_en", channel_id=channel.id, gender="feminine")
    script = SimpleNamespace(
        content_id=content_id, language="en", version=1, validated=True,
        voice_script="Some validated narration text for this test.",
        estimated_duration_sec=None,
    )
    db = _FakeDb(content=content, channel=channel, voices=[voice], scripts_by_lang={"en": script})
    return content, db


class TestRunAudioGenerationEnforcesAlignment(unittest.TestCase):
    def test_mismatched_duration_and_transcript_fails_content(self):
        content, db = _build_content_channel_script(is_short_episode=False)
        mismatched_transcript = [{"word": "hello", "start": 0.0, "end": 616.58}]

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", return_value=(b"mp3", [])),
            patch.object(audio_module, "save_audio", return_value=("audio/en.mp3", 161_724)),
            patch.object(audio_module, "transcribe", return_value=mismatched_transcript),
        ):
            ok = audio_module.run_audio_generation(content.id, db)

        self.assertFalse(ok)
        self.assertEqual(content.status, "FAILED")
        self.assertEqual(db.added, [])  # never persisted — corrupted row must not ship

    def test_aligned_duration_and_transcript_succeeds(self):
        content, db = _build_content_channel_script(is_short_episode=False)
        aligned_transcript = [{"word": "hello", "start": 0.0, "end": 616.4}]

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", return_value=(b"mp3", [])),
            patch.object(audio_module, "save_audio", return_value=("audio/en.mp3", 616_835)),
            patch.object(audio_module, "transcribe", return_value=aligned_transcript),
        ):
            ok = audio_module.run_audio_generation(content.id, db)

        self.assertTrue(ok)
        self.assertEqual(content.status, "AUDIO_DONE")
        self.assertEqual(len(db.added), 1)


# ── Agent 5 props builder: _assert_timeline_alignment() ──────────────────────

class TestAssertTimelineAlignmentUnit(unittest.TestCase):
    def test_aligned_sections_and_captions_pass(self):
        remotion_builder._assert_timeline_alignment(
            duration_ms=9000,
            sections=[{"audio_end_ms": 9000}],
            captions=[{"end_ms": 8950}],
            context="test",
        )  # must not raise

    def test_section_drift_raises(self):
        with self.assertRaises(ValueError) as ctx:
            remotion_builder._assert_timeline_alignment(
                duration_ms=161_724,
                sections=[{"audio_end_ms": 616_835}],
                captions=[],
                context="test-context",
            )
        self.assertIn("last_section_end_ms", str(ctx.exception))
        self.assertIn("test-context", str(ctx.exception))

    def test_caption_drift_raises(self):
        with self.assertRaises(ValueError) as ctx:
            remotion_builder._assert_timeline_alignment(
                duration_ms=161_724,
                sections=[{"audio_end_ms": 161_724}],
                captions=[{"end_ms": 616_580}],
                context="test-context",
            )
        self.assertIn("last_caption_end_ms", str(ctx.exception))

    def test_zero_duration_is_a_no_op(self):
        remotion_builder._assert_timeline_alignment(
            duration_ms=0, sections=[{"audio_end_ms": 999_999}],
            captions=[{"end_ms": 999_999}], context="test",
        )  # must not raise — nothing to compare against


class TestPropsBuilderRealCallsEnforceAlignment(unittest.TestCase):
    """Drives the REAL build_main_props()/build_short_props() — pure local
    file I/O, no external API — proving the invariant fires through the
    actual props-writing entrypoints, not just the helper in isolation."""

    def test_build_main_props_raises_on_mismatch(self):
        with TemporaryDirectory() as tmp:
            with patch.object(settings, "media_path", tmp):
                with self.assertRaises(ValueError):
                    remotion_builder.build_main_props(
                        content_id="cid", language="en",
                        audio_file_path=f"{tmp}/audio.mp3", duration_ms=161_724,
                        sections=[{"section_order": 0, "audio_end_ms": 161_724, "media_url": ""}],
                        standard_subtitles=[{"text": "hi", "start_ms": 0, "end_ms": 616_580}],
                        karaoke_subtitles=[],
                    )
            # No partial props file left behind on failure.
            self.assertFalse((Path(tmp) / "remotion_props" / "cid_en_main.json").exists())

    def test_build_main_props_succeeds_when_aligned(self):
        with TemporaryDirectory() as tmp:
            with patch.object(settings, "media_path", tmp):
                path = remotion_builder.build_main_props(
                    content_id="cid", language="en",
                    audio_file_path=f"{tmp}/audio.mp3", duration_ms=9000,
                    sections=[{"section_order": 0, "audio_end_ms": 9000, "media_url": ""}],
                    standard_subtitles=[{"text": "hi", "start_ms": 0, "end_ms": 8950}],
                    karaoke_subtitles=[],
                )
            props = json.loads(Path(path).read_text())
        self.assertEqual(props["duration_ms"], 9000)

    def test_build_short_props_raises_on_mismatch(self):
        with TemporaryDirectory() as tmp:
            with patch.object(settings, "media_path", tmp):
                with self.assertRaises(ValueError):
                    remotion_builder.build_short_props(
                        content_id="cid", language="en",
                        audio_file_path=f"{tmp}/audio.mp3",
                        short={
                            "short_index": 0, "start_ms": 0, "end_ms": 66_000,
                            "sections": [{"section_order": 0, "audio_end_ms": 66_000, "media_url": ""}],
                            "part_label": "", "total_parts": 1,
                        },
                        karaoke_subtitles=[{"start_ms": 0, "end_ms": 400}],
                    )


if __name__ == "__main__":
    unittest.main()
