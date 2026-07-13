"""Runtime proof for Short minimum script/audio length gates.

Paid/runtime IO boundaries are stubbed only for the Agent 3 audio pipeline
(TTS generation, storage, Whisper). The script validators and audio duration
assertion path run as real project logic.
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

from app.agents.agent2_discovery.services import scripts
from app.agents.agent3_audio.services import audio
from app.models import AudioFile, Channel, ChannelVoice, Content, Script


def words(count: int) -> str:
    return " ".join(f"detail{i}" for i in range(count)) + "."


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


class ShortMinimumLengthTest(unittest.TestCase):
    def test_source_short_under_minimum_is_major(self) -> None:
        issues = scripts._collect_short_script_major_issues(
            ep_voice_script=words(scripts._MIN_SHORT_WORDS - 1),
            source_language="en",
            part_n=1,
            correction_round=1,
        )
        categories = {issue["category"] for issue in issues}
        self.assertIn("script_too_short", categories)
        self.assertTrue(all(issue["severity"] == "MAJOR" for issue in issues))

    def test_translated_short_under_minimum_is_major(self) -> None:
        issues = scripts._collect_translated_short_script_issues(
            voice_script=words(scripts._MIN_SHORT_WORDS - 1),
            language="es",
            source_word_count=scripts._MIN_SHORT_WORDS,
        )
        categories = {issue["category"] for issue in issues}
        self.assertIn("translated_short_too_short", categories)
        self.assertTrue(all(issue["severity"] == "MAJOR" for issue in issues))

    def test_short_at_minimum_has_no_minimum_length_issue(self) -> None:
        source_issues = scripts._collect_short_script_major_issues(
            ep_voice_script=words(scripts._MIN_SHORT_WORDS),
            source_language="en",
            part_n=1,
            correction_round=1,
        )
        translated_issues = scripts._collect_translated_short_script_issues(
            voice_script=words(scripts._MIN_SHORT_WORDS),
            language="es",
            source_word_count=scripts._MIN_SHORT_WORDS,
        )
        self.assertNotIn("script_too_short", {issue["category"] for issue in source_issues})
        self.assertNotIn("translated_short_too_short", {issue["category"] for issue in translated_issues})

    def test_child_audio_under_61_seconds_fails_before_whisper_or_persist(self) -> None:
        content_id = uuid.uuid4()
        content = SimpleNamespace(
            id=content_id,
            channel_id=uuid.uuid4(),
            is_short_episode=True,
            parent_content_id=uuid.uuid4(),
            status="SCRIPTS_VALIDATED",
        )
        channel = SimpleNamespace(id=content.channel_id)
        voice = SimpleNamespace(language="en", voice_id="voice_en", channel_id=channel.id)
        script = SimpleNamespace(
            content_id=content_id,
            language="en",
            version=1,
            validated=True,
            voice_script=words(scripts._MIN_SHORT_WORDS),
            estimated_duration_sec=None,
        )
        db = FakeDb(content=content, channel=channel, voices=[voice], scripts_by_lang={"en": script})

        with (
            patch.object(audio, "audio_path", return_value=MissingAudioPath()),
            patch.object(audio, "generate_audio", return_value=(b"mp3", [])) as generate_audio,
            patch.object(
                audio,
                "save_audio",
                return_value=("cache/audio/en.mp3", audio._MIN_SHORT_AUDIO_DURATION_MS - 1),
            ) as save_audio,
            patch.object(audio, "transcribe", return_value=[]) as transcribe,
        ):
            ok = audio.run_audio_generation(content_id, db)

        self.assertFalse(ok)
        self.assertEqual(content.status, "FAILED")
        generate_audio.assert_called_once()
        save_audio.assert_called_once()
        transcribe.assert_not_called()
        self.assertEqual(db.added, [])
        self.assertGreaterEqual(db.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
