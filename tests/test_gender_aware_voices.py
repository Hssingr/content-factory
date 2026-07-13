"""Runtime proof for roadmap Phase D1 — gender-aware voice configuration.

The operator's channels normally want a feminine narrating voice, but some
stories are centrally about (or narrated as) a male figure and should use a
masculine voice instead — automatically, not as a manual per-episode step.
`ChannelVoice` now allows one row per (channel, language, gender); the story
blueprint's new `protagonist_gender` field (Agent 2, generated once at
blueprint time, zero extra AI calls) drives which gender Agent 3 selects at
audio-generation time, with a logged fallback when only one gender is
configured for a language.

D2 (threading protagonist_gender into generate_native_script() for
translated-script grammar agreement) is explicitly out of scope for this
phase per operator instruction — not tested here.

Only the paid `call_claude_structured` boundary is stubbed. Everything else
(schema, real `generate_story_blueprint()`/`run_audio_generation()`
functions, the gender-selection helpers, the Pydantic schema, and
`replace_voices()`) runs for real, per CLAUDE.md §19.1/§19.4.
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

from app.agents.agent2_discovery import system_prompt as agent2_system_prompt
from app.agents.agent3_audio.services import audio as audio_module
from app.models import AudioFile, Channel, ChannelVoice, Content, Script
from app.schemas.channel import VoiceEntry, VoiceResponse


class _Story:
    url = "https://reddit.com/r/nosleep/comments/abc/x"
    title = "A story"
    body = "Belisarius marched into Constantinople under a grey sky."
    language = "en"


class _Channel:
    niche = "history"
    tone = "documentary"


# ── Blueprint schema/prompt gained protagonist_gender ────────────────────────

class BlueprintSchemaGainedProtagonistGenderTest(unittest.TestCase):
    def test_schema_requires_the_field_with_correct_enum(self):
        schema = agent2_system_prompt._STORY_BLUEPRINT_SCHEMA
        self.assertIn("protagonist_gender", schema["properties"])
        self.assertIn("protagonist_gender", schema["required"])
        self.assertEqual(
            set(schema["properties"]["protagonist_gender"]["enum"]),
            {"feminine", "masculine", "unspecified"},
        )

    def test_prompt_documents_the_field(self):
        self.assertIn("protagonist_gender", agent2_system_prompt._STORY_BLUEPRINT_SYSTEM_PROMPT)


class GenerateStoryBlueprintPropagatesGenderTest(unittest.TestCase):
    """Runtime proof: generate_story_blueprint() (real function) forwards
    protagonist_gender through untouched — only call_claude_structured stubbed."""

    def _fake_response(self, **overrides) -> dict:
        base = {
            "hook": "h", "central_question": "q",
            "major_turns": ["turn one", "turn two"],
            "final_payoff": "p", "comment_trigger": "c?",
            "midpoint_retention_trap": "trap",
            "suggested_section_count": 3, "suggested_title": "t",
            "character_descriptors": [], "era_setting": "",
            "protagonist_gender": "masculine",
        }
        base.update(overrides)
        return base

    def test_protagonist_gender_survives_the_real_function(self):
        with patch.object(
            agent2_system_prompt, "call_claude_structured",
            side_effect=lambda **kwargs: self._fake_response(),
        ):
            result = agent2_system_prompt.generate_story_blueprint(_Story(), _Channel())

        self.assertEqual(result["protagonist_gender"], "masculine")

    def test_unspecified_is_returned_untouched_normalization_happens_downstream(self):
        with patch.object(
            agent2_system_prompt, "call_claude_structured",
            side_effect=lambda **kwargs: self._fake_response(protagonist_gender="unspecified"),
        ):
            result = agent2_system_prompt.generate_story_blueprint(_Story(), _Channel())

        # generate_story_blueprint() itself does not normalize — that's
        # _resolve_target_gender()'s job (below), so a caller inspecting the
        # raw blueprint sees exactly what Claude returned.
        self.assertEqual(result["protagonist_gender"], "unspecified")


# ── _resolve_target_gender() ─────────────────────────────────────────────────

class _FakeContentDb:
    def __init__(self, content_by_id: dict):
        self._content_by_id = content_by_id

    def get(self, model, key):
        if model is Content:
            return self._content_by_id.get(key)
        return None


class ResolveTargetGenderTest(unittest.TestCase):
    def test_parent_with_masculine_protagonist(self):
        content = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint={"protagonist_gender": "masculine"},
            is_short_episode=False, parent_content_id=None,
        )
        db = _FakeContentDb({content.id: content})
        self.assertEqual(audio_module._resolve_target_gender(content, db), "masculine")

    def test_parent_with_feminine_protagonist(self):
        content = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint={"protagonist_gender": "feminine"},
            is_short_episode=False, parent_content_id=None,
        )
        db = _FakeContentDb({content.id: content})
        self.assertEqual(audio_module._resolve_target_gender(content, db), "feminine")

    def test_unspecified_normalizes_to_feminine(self):
        content = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint={"protagonist_gender": "unspecified"},
            is_short_episode=False, parent_content_id=None,
        )
        db = _FakeContentDb({content.id: content})
        self.assertEqual(audio_module._resolve_target_gender(content, db), "feminine")

    def test_missing_blueprint_normalizes_to_feminine(self):
        content = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint=None, is_short_episode=False, parent_content_id=None,
        )
        db = _FakeContentDb({content.id: content})
        self.assertEqual(audio_module._resolve_target_gender(content, db), "feminine")

    def test_blueprint_missing_the_key_entirely_normalizes_to_feminine(self):
        # A blueprint generated before this phase shipped simply lacks the key.
        content = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint={"hook": "h"}, is_short_episode=False, parent_content_id=None,
        )
        db = _FakeContentDb({content.id: content})
        self.assertEqual(audio_module._resolve_target_gender(content, db), "feminine")

    def test_child_with_no_own_blueprint_reads_parents(self):
        parent_id = uuid.uuid4()
        parent = SimpleNamespace(id=parent_id, story_blueprint={"protagonist_gender": "masculine"})
        child = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint=None, is_short_episode=True, parent_content_id=parent_id,
        )
        db = _FakeContentDb({parent_id: parent, child.id: child})
        self.assertEqual(audio_module._resolve_target_gender(child, db), "masculine")

    def test_child_with_missing_parent_row_normalizes_to_feminine(self):
        child = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint=None, is_short_episode=True, parent_content_id=uuid.uuid4(),
        )
        db = _FakeContentDb({child.id: child})
        self.assertEqual(audio_module._resolve_target_gender(child, db), "feminine")

    def test_child_with_its_own_blueprint_never_looks_up_parent(self):
        child = SimpleNamespace(
            id=uuid.uuid4(), story_blueprint={"protagonist_gender": "masculine"},
            is_short_episode=True, parent_content_id=uuid.uuid4(),
        )
        # No parent row registered at all — if the function tried to look it
        # up, db.get() would return None and silently swallow the child's
        # own real value; this proves it never tries.
        db = _FakeContentDb({child.id: child})
        self.assertEqual(audio_module._resolve_target_gender(child, db), "masculine")


# ── _select_channel_voice() ──────────────────────────────────────────────────

class SelectChannelVoiceTest(unittest.TestCase):
    def test_exact_match_wins(self):
        feminine = SimpleNamespace(gender="feminine")
        masculine = SimpleNamespace(gender="masculine")
        voices_by_lang = {"en": {"feminine": feminine, "masculine": masculine}}
        self.assertIs(
            audio_module._select_channel_voice(voices_by_lang, "en", "masculine"), masculine,
        )
        self.assertIs(
            audio_module._select_channel_voice(voices_by_lang, "en", "feminine"), feminine,
        )

    def test_falls_back_to_whichever_gender_is_configured_and_logs(self):
        feminine = SimpleNamespace(gender="feminine")
        voices_by_lang = {"en": {"feminine": feminine}}
        with patch.object(audio_module.logger, "warning") as mock_warn:
            result = audio_module._select_channel_voice(voices_by_lang, "en", "masculine")
        self.assertIs(result, feminine)
        mock_warn.assert_called_once()
        self.assertIn("VOICE_GENDER_FALLBACK", mock_warn.call_args[0][0])

    def test_no_voice_configured_for_language_returns_none(self):
        voices_by_lang = {"fr": {"feminine": SimpleNamespace(gender="feminine")}}
        self.assertIsNone(audio_module._select_channel_voice(voices_by_lang, "en", "feminine"))

    def test_empty_voices_by_lang_returns_none(self):
        self.assertIsNone(audio_module._select_channel_voice({}, "en", "feminine"))


# ── Full run_audio_generation() integration — real gender selection ─────────

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
    def __init__(self, *, content_by_id: dict, channel, voices, scripts_by_lang):
        self._content_by_id = content_by_id
        self.channel = channel
        self.voices = voices
        self.scripts = list(scripts_by_lang.values())
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


def _voice(language: str, gender: str, voice_id: str, channel_id) -> SimpleNamespace:
    return SimpleNamespace(language=language, gender=gender, voice_id=voice_id, channel_id=channel_id)


class RunAudioGenerationSelectsGenderMatchedVoiceTest(unittest.TestCase):
    def test_masculine_protagonist_selects_masculine_voice(self):
        content_id = uuid.uuid4()
        channel = SimpleNamespace(id=uuid.uuid4())
        content = SimpleNamespace(
            id=content_id, channel_id=channel.id, is_short_episode=False,
            parent_content_id=None, status="SCRIPTS_VALIDATED",
            story_blueprint={"protagonist_gender": "masculine"},
        )
        feminine_voice = _voice("en", "feminine", "voice-fem", channel.id)
        masculine_voice = _voice("en", "masculine", "voice-masc", channel.id)
        script = SimpleNamespace(
            content_id=content_id, language="en", version=1, validated=True,
            voice_script="Belisarius marched north through the snow and cold winds today.",
            estimated_duration_sec=None,
        )
        db = _FakeDb(
            content_by_id={content_id: content}, channel=channel,
            voices=[feminine_voice, masculine_voice], scripts_by_lang={"en": script},
        )

        captured_voice = {}

        def fake_generate_audio(voice_script, channel_voice, is_short_episode=False):
            captured_voice["voice"] = channel_voice
            return b"mp3", []

        transcript = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.3} for i, w in enumerate(
            "Belisarius marched north through the snow and cold winds today".split()
        )]

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", side_effect=fake_generate_audio),
            patch.object(audio_module, "save_audio", return_value=("audio/en.mp3", int(transcript[-1]["end"] * 1000))),
            patch.object(audio_module, "transcribe", return_value=transcript),
        ):
            ok = audio_module.run_audio_generation(content_id, db)

        self.assertTrue(ok)
        self.assertIs(captured_voice["voice"], masculine_voice)

    def test_no_blueprint_defaults_to_feminine_voice(self):
        content_id = uuid.uuid4()
        channel = SimpleNamespace(id=uuid.uuid4())
        content = SimpleNamespace(
            id=content_id, channel_id=channel.id, is_short_episode=False,
            parent_content_id=None, status="SCRIPTS_VALIDATED", story_blueprint=None,
        )
        feminine_voice = _voice("en", "feminine", "voice-fem", channel.id)
        masculine_voice = _voice("en", "masculine", "voice-masc", channel.id)
        script = SimpleNamespace(
            content_id=content_id, language="en", version=1, validated=True,
            voice_script="Some validated narration text for this test today please.",
            estimated_duration_sec=None,
        )
        db = _FakeDb(
            content_by_id={content_id: content}, channel=channel,
            voices=[feminine_voice, masculine_voice], scripts_by_lang={"en": script},
        )

        captured_voice = {}

        def fake_generate_audio(voice_script, channel_voice, is_short_episode=False):
            captured_voice["voice"] = channel_voice
            return b"mp3", []

        transcript = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.3} for i, w in enumerate(
            "Some validated narration text for this test today please".split()
        )]

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", side_effect=fake_generate_audio),
            patch.object(audio_module, "save_audio", return_value=("audio/en.mp3", int(transcript[-1]["end"] * 1000))),
            patch.object(audio_module, "transcribe", return_value=transcript),
        ):
            ok = audio_module.run_audio_generation(content_id, db)

        self.assertTrue(ok)
        self.assertIs(captured_voice["voice"], feminine_voice)

    def test_masculine_wanted_but_only_feminine_configured_falls_back(self):
        content_id = uuid.uuid4()
        channel = SimpleNamespace(id=uuid.uuid4())
        content = SimpleNamespace(
            id=content_id, channel_id=channel.id, is_short_episode=False,
            parent_content_id=None, status="SCRIPTS_VALIDATED",
            story_blueprint={"protagonist_gender": "masculine"},
        )
        feminine_voice = _voice("en", "feminine", "voice-fem", channel.id)
        script = SimpleNamespace(
            content_id=content_id, language="en", version=1, validated=True,
            voice_script="Some validated narration text for this test today please.",
            estimated_duration_sec=None,
        )
        db = _FakeDb(
            content_by_id={content_id: content}, channel=channel,
            voices=[feminine_voice], scripts_by_lang={"en": script},
        )

        captured_voice = {}

        def fake_generate_audio(voice_script, channel_voice, is_short_episode=False):
            captured_voice["voice"] = channel_voice
            return b"mp3", []

        transcript = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.3} for i, w in enumerate(
            "Some validated narration text for this test today please".split()
        )]

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", side_effect=fake_generate_audio),
            patch.object(audio_module, "save_audio", return_value=("audio/en.mp3", int(transcript[-1]["end"] * 1000))),
            patch.object(audio_module, "transcribe", return_value=transcript),
        ):
            ok = audio_module.run_audio_generation(content_id, db)

        self.assertTrue(ok)
        self.assertIs(captured_voice["voice"], feminine_voice)

    def test_child_inherits_parent_protagonist_gender_through_real_db_lookup(self):
        parent_id = uuid.uuid4()
        child_id = uuid.uuid4()
        channel = SimpleNamespace(id=uuid.uuid4())
        parent = SimpleNamespace(
            id=parent_id, story_blueprint={"protagonist_gender": "masculine"},
        )
        child = SimpleNamespace(
            id=child_id, channel_id=channel.id, is_short_episode=True,
            parent_content_id=parent_id, status="SCRIPTS_VALIDATED", story_blueprint=None,
        )
        feminine_voice = _voice("en", "feminine", "voice-fem", channel.id)
        masculine_voice = _voice("en", "masculine", "voice-masc", channel.id)
        script = SimpleNamespace(
            content_id=child_id, language="en", version=1, validated=True,
            voice_script="Belisarius marched north through the snow and the cold today.",
            estimated_duration_sec=None,
        )
        db = _FakeDb(
            content_by_id={parent_id: parent, child_id: child}, channel=channel,
            voices=[feminine_voice, masculine_voice], scripts_by_lang={"en": script},
        )

        captured_voice = {}

        def fake_generate_audio(voice_script, channel_voice, is_short_episode=False):
            captured_voice["voice"] = channel_voice
            return b"mp3", []

        transcript = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.3} for i, w in enumerate(
            "Belisarius marched north through the snow and the cold today".split()
        )]

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", side_effect=fake_generate_audio),
            patch.object(audio_module, "save_audio", return_value=("audio/en.mp3", int(transcript[-1]["end"] * 1000))),
            patch.object(audio_module, "transcribe", return_value=transcript),
            patch.object(audio_module, "_assert_short_audio_min_duration", return_value=True),
            patch.object(audio_module, "_assert_short_audio_has_transcript", return_value=True),
        ):
            ok = audio_module.run_audio_generation(child_id, db)

        self.assertTrue(ok)
        self.assertIs(captured_voice["voice"], masculine_voice)

    def test_neither_gender_configured_still_skips_language_as_before(self):
        content_id = uuid.uuid4()
        channel = SimpleNamespace(id=uuid.uuid4())
        content = SimpleNamespace(
            id=content_id, channel_id=channel.id, is_short_episode=False,
            parent_content_id=None, status="SCRIPTS_VALIDATED", story_blueprint=None,
        )
        script = SimpleNamespace(
            content_id=content_id, language="en", version=1, validated=True,
            voice_script="Some validated narration text.", estimated_duration_sec=None,
        )
        db = _FakeDb(
            content_by_id={content_id: content}, channel=channel,
            voices=[], scripts_by_lang={"en": script},
        )

        with patch.object(
            audio_module, "generate_audio",
            side_effect=AssertionError("must not attempt TTS with no voice configured"),
        ):
            ok = audio_module.run_audio_generation(content_id, db)

        self.assertFalse(ok)


# ── Agent 1: VoiceEntry/VoiceResponse schema ─────────────────────────────────

class VoiceSchemaGenderFieldTest(unittest.TestCase):
    def test_voice_entry_defaults_to_feminine(self):
        entry = VoiceEntry(language="en", voice_id="v1")
        self.assertEqual(entry.gender, "feminine")

    def test_voice_entry_accepts_masculine(self):
        entry = VoiceEntry(language="en", voice_id="v1", gender="masculine")
        self.assertEqual(entry.gender, "masculine")

    def test_voice_entry_rejects_invalid_gender(self):
        with self.assertRaises(Exception):
            VoiceEntry(language="en", voice_id="v1", gender="other")

    def test_voice_response_requires_gender_from_attributes(self):
        row = SimpleNamespace(
            id=uuid.uuid4(), language="en", provider="cartesia", tts_model="sonic-3.5",
            voice_id="v1", emotion=None, music_style=None, use_case=None, gender="masculine",
        )
        response = VoiceResponse.model_validate(row)
        self.assertEqual(response.gender, "masculine")


if __name__ == "__main__":
    unittest.main()
