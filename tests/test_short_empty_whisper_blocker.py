"""Runtime proof for blocking Short render/audio on an empty Whisper transcript
(roadmap 3.9 / audit A-3, exec-10).

Two independent halves, matching the roadmap's file scope:
  1. agent5/services/video.py's _collect_technical_blockers() — blocks a
     Short render when audio.whisper_transcript is empty. Parent long-form
     content is unaffected (it tolerates missing transcript, as before).
  2. agent3/services/audio.py's run_audio_generation() — fails (rolls back,
     does not persist) a child Short language when transcribe() returns []
     (both Whisper engines exhausted). Parent long-form content still
     tolerates an empty transcript exactly as before.

Only paid/external IO boundaries are stubbed (TTS generation, audio storage,
Whisper transcription) — everything else is real project logic, following
the same FakeDb/FakeQuery precedent as test_short_minimum_length.py.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery.services import scripts as agent2_scripts
from app.agents.agent3_audio.services import audio
from app.agents.agent5_render.services.video import _collect_technical_blockers
from app.models import AudioFile, Channel, ChannelVoice, Content, Script


def words(count: int) -> str:
    return " ".join(f"detail{i}" for i in range(count)) + "."


# ── Agent 5: render-side blocker ────────────────────────────────────────────

def _beat(order: int) -> dict:
    return {
        "section_order": order,
        "media_url": f"cache/content-1/{order}.jpg",
    }


class TestCollectTechnicalBlockersEmptyTranscript(unittest.TestCase):
    def test_short_with_empty_whisper_is_blocked(self):
        audio_row = SimpleNamespace(whisper_transcript=[])
        blockers = _collect_technical_blockers(
            sections=[_beat(0), _beat(1)],
            standard_subs=[],
            audio=audio_row,
            is_short_episode=True,
        )
        self.assertTrue(any("empty_whisper_transcript_short" in b for b in blockers))

    def test_short_with_none_whisper_is_blocked(self):
        audio_row = SimpleNamespace(whisper_transcript=None)
        blockers = _collect_technical_blockers(
            sections=[_beat(0)],
            standard_subs=[],
            audio=audio_row,
            is_short_episode=True,
        )
        self.assertTrue(any("empty_whisper_transcript_short" in b for b in blockers))

    def test_short_with_real_whisper_is_not_blocked(self):
        audio_row = SimpleNamespace(whisper_transcript=[{"word": "hi", "start": 0.0, "end": 0.4}])
        blockers = _collect_technical_blockers(
            sections=[_beat(0)],
            standard_subs=[{"text": "hi", "start_ms": 0, "end_ms": 400}],
            audio=audio_row,
            is_short_episode=True,
        )
        self.assertFalse(any("empty_whisper_transcript_short" in b for b in blockers))

    def test_parent_with_empty_whisper_is_not_blocked(self):
        """Parent long-form content is unaffected — matches pre-existing
        tolerant behavior; only Shorts are blocked by this check."""
        audio_row = SimpleNamespace(whisper_transcript=[])
        blockers = _collect_technical_blockers(
            sections=[_beat(0)],
            standard_subs=[],
            audio=audio_row,
            is_short_episode=False,
        )
        self.assertFalse(any("empty_whisper_transcript_short" in b for b in blockers))

    def test_default_is_short_episode_false_preserves_prior_signature(self):
        """Backward compatibility: omitting is_short_episode entirely must not
        introduce the new blocker for any untouched caller."""
        audio_row = SimpleNamespace(whisper_transcript=[])
        blockers = _collect_technical_blockers(
            sections=[_beat(0)],
            standard_subs=[],
            audio=audio_row,
        )
        self.assertFalse(any("empty_whisper_transcript_short" in b for b in blockers))


# ── Agent 3: audio-side failure ─────────────────────────────────────────────

class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)


class FakeDb:
    def __init__(self, *, content, channel, voices, scripts_by_lang):
        self.content = content
        self.channel = channel
        self.voices = voices
        self.scripts = list(scripts_by_lang.values())
        self.commits = 0
        self.rollbacks = 0
        self.added = []

    def get(self, model, key):
        if model is Content:
            return self.content if key == self.content.id else None
        if model is Channel:
            return self.channel if key == self.channel.id else None
        return None

    def query(self, model):
        if model is ChannelVoice:
            return FakeQuery(self.voices)
        if model is Script:
            return FakeQuery(self.scripts)
        if model is AudioFile:
            return FakeQuery([])
        return FakeQuery([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        pass

    def add(self, row):
        self.added.append(row)


class MissingAudioPath:
    def exists(self):
        return False


def _build_content_channel_script(*, is_short_episode: bool):
    content_id = uuid.uuid4()
    content = SimpleNamespace(
        id=content_id,
        channel_id=uuid.uuid4(),
        is_short_episode=is_short_episode,
        parent_content_id=uuid.uuid4() if is_short_episode else None,
        status="SCRIPTS_VALIDATED",
    )
    channel = SimpleNamespace(id=content.channel_id)
    voice = SimpleNamespace(language="en", voice_id="voice_en", channel_id=channel.id)
    min_words = agent2_scripts._MIN_SHORT_WORDS if is_short_episode else 1300
    script = SimpleNamespace(
        content_id=content_id,
        language="en",
        version=1,
        validated=True,
        voice_script=words(min_words),
        estimated_duration_sec=None,
    )
    db = FakeDb(content=content, channel=channel, voices=[voice], scripts_by_lang={"en": script})
    return content, db


class TestRunAudioGenerationEmptyTranscript(unittest.TestCase):
    def test_child_short_both_engines_fail_rolls_back_and_fails_content(self) -> None:
        content, db = _build_content_channel_script(is_short_episode=True)

        with (
            patch.object(audio, "audio_path", return_value=MissingAudioPath()),
            patch.object(audio, "generate_audio", return_value=b"mp3"),
            patch.object(
                audio, "save_audio",
                return_value=("cache/audio/en.mp3", audio._MIN_SHORT_AUDIO_DURATION_MS + 5_000),
            ),
            patch.object(audio, "transcribe", return_value=[]) as transcribe,
        ):
            ok = audio.run_audio_generation(content.id, db)

        self.assertFalse(ok)
        self.assertEqual(content.status, "FAILED")
        transcribe.assert_called_once()
        self.assertEqual(db.added, [])
        self.assertGreaterEqual(db.rollbacks, 1)

    def test_child_short_with_real_transcript_succeeds(self) -> None:
        content, db = _build_content_channel_script(is_short_episode=True)
        real_transcript = [{"word": "hello", "start": 0.0, "end": 0.4}]

        with (
            patch.object(audio, "audio_path", return_value=MissingAudioPath()),
            patch.object(audio, "generate_audio", return_value=b"mp3"),
            patch.object(
                audio, "save_audio",
                return_value=("cache/audio/en.mp3", audio._MIN_SHORT_AUDIO_DURATION_MS + 5_000),
            ),
            patch.object(audio, "transcribe", return_value=real_transcript),
        ):
            ok = audio.run_audio_generation(content.id, db)

        self.assertTrue(ok)
        self.assertEqual(content.status, "AUDIO_DONE")
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].whisper_transcript, real_transcript)

    def test_parent_content_both_engines_fail_still_tolerated(self) -> None:
        """Parent long-form content preserves the pre-existing soft-tolerance
        behavior — only Shorts are hardened by this fix."""
        content, db = _build_content_channel_script(is_short_episode=False)

        with (
            patch.object(audio, "audio_path", return_value=MissingAudioPath()),
            patch.object(audio, "generate_audio", return_value=b"mp3"),
            patch.object(audio, "save_audio", return_value=("cache/audio/en.mp3", 500_000)),
            patch.object(audio, "transcribe", return_value=[]) as transcribe,
        ):
            ok = audio.run_audio_generation(content.id, db)

        self.assertTrue(ok)
        self.assertEqual(content.status, "AUDIO_DONE")
        transcribe.assert_called_once()
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].whisper_transcript, [])


if __name__ == "__main__":
    unittest.main()
