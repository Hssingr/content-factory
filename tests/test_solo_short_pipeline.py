"""Runtime proofs for the Solo Short build (Phase D of code_report/
output_mode_shorts_only_and_youtube_long_only_roadmap.md).

Only the paid Claude/Flux boundaries are stubbed — every internal function
under test runs for real. Covers:

1. Agent 2: `run_script_workflow()`'s top-level dispatch reaches
   `_run_solo_short_script_workflow()` for a Solo Short row, which reaches
   `SCRIPTS_VALIDATED` without ever touching `run_shorts_planner()` or the
   long-form blueprint->sections->quality-gate pipeline.
2. Agent 4: `_run_solo_short_visuals()` calls the shared `_run_visual_pass()`
   with the Solo Short's shape-correct parameters (portrait size, the short
   script_format), and `_run_visual_pass()` itself correctly threads
   width/height through to `generate_all_beat_images()`.
3. Agent 3: `run_audio_generation()` on a Solo Short row resolves
   `protagonist_gender` from its OWN `story_blueprint` (never a parent
   lookup, since `parent_content_id=None`), and the 61s Short audio floor
   applies exactly as it does for a child-of-parent Short.
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

from app.config import settings
from app.agents.agent2_discovery.services import script_workflow
from app.agents.agent2_discovery.services import scripts as agent2_scripts
from app.agents.agent2_discovery import system_prompt as agent2_system_prompt
from app.agents.agent3_audio.services import audio as audio_module
from app.agents.agent4_visuals.services import visual_orchestrator
from app.agents.agent5_render.services import video as video_module
from app.models import (
    AudioFile, Channel, ChannelConfig, ChannelLanguage, ChannelVoice, Content,
    Script, VideoSection,
)
from app.services.script_checks import SOLO_SHORT_SCRIPT_FORMAT


# ── Shared fake DB (same shape as test_post_roadmap_audit_cleanup.py) ────────

class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def join(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        return _FakeQuery(self.rows[:n])

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def count(self):
        return len(self.rows)


class _FakeDb:
    def __init__(self):
        self.tables: dict = {}

    def get(self, model, key):
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key or getattr(row, "channel_id", None) == key:
                return row
        return None

    def query(self, model):
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def flush(self):
        pass

    def refresh(self, row):
        pass

    def commit(self):
        pass


# A source excerpt long enough to clear the Solo Short's 420-word floor but
# well under the long-form 900-word floor — a targeted proof that Check 5's
# SOLO_SHORT_SCRIPT_FORMAT constant, not the channel's long-form
# script_format, is what actually gates this content.
_SOLO_SHORT_SOURCE_EXCERPT = " ".join(["word"] * 450)


class TestSoloShortScriptWorkflow(unittest.TestCase):
    """Agent 2: real run_script_workflow() -> _run_solo_short_script_workflow()
    chain for a Solo Short row."""

    def _fixtures(self):
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        db = _FakeDb()
        db.add(Channel(id=channel_id, niche="horror", tone="suspenseful"))
        db.add(ChannelConfig(
            channel_id=channel_id, script_format="youtube_long",
            output_mode="shorts_only", script_source="reddit",
            audio_tags_enabled=False, visual_style="story_driven",
            image_style="photorealistic",
        ))
        db.add(ChannelVoice(
            id=uuid.uuid4(), channel_id=channel_id, language="en",
            provider="cartesia", voice_id="v1", tts_model="sonic-3.5",
        ))
        db.add(ChannelLanguage(
            id=uuid.uuid4(), channel_id=channel_id, language="fr", channel_name="Le Show",
        ))
        db.add(ChannelVoice(
            id=uuid.uuid4(), channel_id=channel_id, language="fr",
            provider="cartesia", voice_id="v2", tts_model="sonic-3.5",
        ))
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=True,
            parent_content_id=None, short_part_number=1, short_total_parts=1,
            source_language="en", status="APPROVED", title="T",
            source_url="https://example.com/x",
            source_excerpt=_SOLO_SHORT_SOURCE_EXCERPT,
        )
        db.add(content)
        return db, content

    def test_solo_short_reaches_scripts_validated_without_shorts_planner(self):
        db, content = self._fixtures()

        blueprint_calls: list = []

        def fake_blueprint(*a, **k):
            blueprint_calls.append(k)
            return {
                "hook": "A sound echoes from the mountain every night.",
                "major_turns": ["turn one", "turn two"],
                "final_payoff": "the truth", "comment_trigger": "Would you have looked?",
                "suggested_section_count": 3, "suggested_title": "T",
                "character_descriptors": [], "era_setting": "present day",
                "protagonist_gender": "unspecified", "midpoint_retention_trap": "",
                "central_question": "what is up there?",
            }

        solo_script_calls: list = []

        def fake_solo_short_script(*a, **k):
            solo_script_calls.append(k)
            return {
                "title": "The Sound on the Mountain",
                "voice_script": " ".join(["word"] * 220),
            }

        native_calls: list = []

        def fake_native(**kwargs):
            native_calls.append(kwargs)
            return {"voice_script": " ".join(["mot"] * 220)}

        with (
            patch.object(script_workflow, "generate_story_blueprint", side_effect=fake_blueprint),
            patch.object(script_workflow, "generate_solo_short_script", side_effect=fake_solo_short_script),
            patch.object(agent2_scripts, "generate_native_script", side_effect=fake_native),
            patch.object(script_workflow, "run_shorts_planner",
                         side_effect=AssertionError("must never be called for a Solo Short")),
            # Poison the long-form-only pipeline too — a Solo Short must
            # never reach any of it.
            patch.object(script_workflow, "generate_parent_source_script",
                         side_effect=AssertionError("must never be called for a Solo Short")),
            self.assertLogs(
                "app.agents.agent2_discovery.services.script_workflow", level="INFO"
            ) as log_ctx,
        ):
            script_workflow.run_script_workflow(content, db)

        joined = " ".join(log_ctx.output)

        # 1. Reached SCRIPTS_VALIDATED via the Solo Short path's own logs.
        self.assertEqual(content.status, "SCRIPTS_VALIDATED")
        self.assertIn("SOLO_SHORT_SCRIPT_START", joined)
        self.assertIn("SOLO_SHORT_SCRIPT_DONE", joined)

        # 2. Blueprint generated exactly once, with the Solo Short's own
        #    source material (not a parent's) as the story body.
        self.assertEqual(len(blueprint_calls), 1)
        self.assertEqual(content.story_blueprint["hook"], "A sound echoes from the mountain every night.")

        # 3. generate_solo_short_script() called exactly once (no word-floor
        #    regen needed — the fake draft already clears the 190-word floor)
        #    with the short-appropriate script_format threaded through the
        #    source-material-floor check (proven indirectly: the 450-word
        #    excerpt would have failed the 900-word long-form floor and
        #    returned early before ever reaching this call).
        self.assertEqual(len(solo_script_calls), 1)

        # 4. Real Script rows persisted for both languages, both validated.
        scripts = db.tables.get(Script, [])
        by_lang = {s.language: s for s in scripts if s.content_id == content.id}
        self.assertEqual(set(by_lang), {"en", "fr"})
        self.assertTrue(by_lang["en"].validated)
        self.assertTrue(by_lang["fr"].validated)
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(native_calls[0]["target_language"], "fr")

        # 5. No child Content rows were ever created.
        all_content = db.tables.get(Content, [])
        self.assertEqual(len(all_content), 1)
        self.assertIs(all_content[0], content)

    def test_word_floor_regen_fires_and_keeps_longer_draft(self):
        db, content = self._fixtures()

        drafts = [
            {"title": "T", "voice_script": " ".join(["word"] * 100)},  # under floor
            {"title": "T", "voice_script": " ".join(["word"] * 230)},  # cleared on retry
        ]
        calls: list = []

        def fake_solo_short_script(*a, **k):
            calls.append(k)
            return drafts[len(calls) - 1]

        with (
            patch.object(script_workflow, "generate_story_blueprint", return_value={
                "hook": "x", "major_turns": [], "final_payoff": "", "comment_trigger": "",
                "suggested_section_count": 3, "suggested_title": "T",
                "character_descriptors": [], "era_setting": "", "protagonist_gender": "unspecified",
                "midpoint_retention_trap": "", "central_question": "",
            }),
            patch.object(script_workflow, "generate_solo_short_script", side_effect=fake_solo_short_script),
            patch.object(agent2_scripts, "generate_native_script",
                         return_value={"voice_script": " ".join(["mot"] * 230)}),
            patch.object(script_workflow, "run_shorts_planner",
                         side_effect=AssertionError("must never be called for a Solo Short")),
            self.assertLogs(
                "app.agents.agent2_discovery.services.script_workflow", level="WARNING"
            ) as log_ctx,
        ):
            script_workflow.run_script_workflow(content, db)

        self.assertEqual(len(calls), 2)
        self.assertIn("SOLO_SHORT_WORD_FLOOR_REGEN", " ".join(log_ctx.output))
        self.assertEqual(content.status, "SCRIPTS_VALIDATED")

        source_script = next(
            s for s in db.tables.get(Script, [])
            if s.content_id == content.id and s.language == "en"
        )
        self.assertEqual(len(source_script.voice_script.split()), 230)


class TestSoloShortVisualDispatch(unittest.TestCase):
    """Agent 4: _run_solo_short_visuals() threads the Solo Short's shape into
    the shared _run_visual_pass() generation sequence."""

    def test_solo_short_visuals_calls_run_visual_pass_with_shape_correct_params(self):
        content = Content(
            id=uuid.uuid4(), channel_id=uuid.uuid4(), is_short_episode=True,
            parent_content_id=None, source_language="en", status="GENERATING_VISUALS",
            title="T", source_url="https://example.com/x", story_blueprint={"hook": "x"},
        )
        channel = Channel(id=content.channel_id, niche="horror", tone="suspenseful")
        scripts_by_lang = {"en": SimpleNamespace(voice_script="Hello world.", language="en")}
        audio_by_lang = {"en": SimpleNamespace(
            duration_ms=70000, whisper_transcript=[{"word": "hi", "start": 0.0, "end": 0.5}],
            section_boundaries=None,
        )}
        db = _FakeDb()

        pass_calls: list = []

        def fake_run_visual_pass(**kwargs):
            pass_calls.append(kwargs)
            beat = {"section_order": 0, "audio_start_ms": 0, "audio_end_ms": 70000,
                    "media_url": "cache/x/0.jpg"}
            return [beat], 70000

        with (
            patch.object(visual_orchestrator, "_load_shared_beats", return_value=[]),
            patch.object(visual_orchestrator, "_run_visual_pass", side_effect=fake_run_visual_pass),
            patch.object(visual_orchestrator, "_save_video_sections", return_value=None),
            patch.object(visual_orchestrator, "save_beat_review_metadata", return_value=None),
        ):
            result = visual_orchestrator._run_solo_short_visuals(
                content, channel, scripts_by_lang, audio_by_lang, db,
                visual_style="story_driven", image_style="photorealistic",
            )

        self.assertEqual(len(pass_calls), 1)
        kwargs = pass_calls[0]
        self.assertTrue(kwargs["is_short_episode"])
        self.assertEqual(kwargs["width"], 1080)
        self.assertEqual(kwargs["height"], 1920)
        self.assertEqual(kwargs["script_format"], SOLO_SHORT_SCRIPT_FORMAT)
        self.assertFalse(kwargs["allow_legacy_fallback"])
        self.assertEqual(result["status"], "CHILD_SHORT_VISUALS_DONE")
        self.assertIn("en", result["beats_by_lang"])

    def test_solo_short_visuals_reuses_complete_persisted_beats(self):
        content = Content(
            id=uuid.uuid4(), channel_id=uuid.uuid4(), is_short_episode=True,
            parent_content_id=None, source_language="en", status="GENERATING_VISUALS",
            title="T", source_url="https://example.com/x",
        )
        channel = Channel(id=content.channel_id, niche="horror", tone="suspenseful")
        scripts_by_lang = {"en": SimpleNamespace(voice_script="Hello world.", language="en")}
        audio_by_lang = {"en": SimpleNamespace(
            duration_ms=70000, whisper_transcript=[{"word": "hi", "start": 0.0, "end": 0.5}],
            section_boundaries=None,
        )}
        db = _FakeDb()
        existing = [{"section_order": 0, "audio_start_ms": 0, "audio_end_ms": 70000,
                     "media_url": "cache/x/0.jpg"}]

        with (
            patch.object(visual_orchestrator, "_load_shared_beats", return_value=existing),
            patch.object(visual_orchestrator, "_run_visual_pass",
                         side_effect=AssertionError("must not regenerate when beats are complete")),
            patch.object(visual_orchestrator, "_save_video_sections", return_value=None),
            patch.object(visual_orchestrator, "save_beat_review_metadata", return_value=None),
        ):
            result = visual_orchestrator._run_solo_short_visuals(
                content, channel, scripts_by_lang, audio_by_lang, db,
            )

        self.assertEqual(result["status"], "CHILD_SHORT_VISUALS_DONE")


class TestRunVisualPassPortraitThreading(unittest.TestCase):
    """_run_visual_pass() itself correctly threads width/height through to
    generate_all_beat_images() — the specific new plumbing this phase added,
    tested independently of _run_solo_short_visuals()'s own dispatch logic."""

    def test_width_height_reach_generate_all_beat_images(self):
        content_id = uuid.uuid4()
        channel = Channel(id=uuid.uuid4(), niche="horror", tone="suspenseful")
        scripts_by_lang = {"en": SimpleNamespace(
            voice_script="Hello world, this is a short story.", language="en",
        )}
        audio_by_lang = {"en": SimpleNamespace(
            duration_ms=9000, whisper_transcript=[{"word": "hi", "start": 0.0, "end": 0.5}],
            section_boundaries=None,
        )}
        db = _FakeDb()

        fake_beats = [{
            "section_order": 0, "beat_order": 0, "audio_start_ms": 0, "audio_end_ms": 9000,
            "flux_prompt": "a concrete subject", "media_url": "", "environment": "other",
        }]
        flux_calls: list = []

        def fake_generate_all_beat_images(beats, cid_str, width=1920, height=1080):
            flux_calls.append((width, height))
            for b in beats:
                b["media_url"] = "cache/x/0.jpg"
            return beats

        with (
            patch.object(visual_orchestrator, "split_into_beats", return_value=fake_beats),
            patch.object(visual_orchestrator, "_save_shared_beats", return_value=None),
            patch.object(visual_orchestrator, "generate_all_beat_images",
                         side_effect=fake_generate_all_beat_images),
        ):
            beats, duration_ms = visual_orchestrator._run_visual_pass(
                content_id=content_id,
                scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang,
                channel=channel,
                script_format=SOLO_SHORT_SCRIPT_FORMAT,
                allow_legacy_fallback=False,
                db=db,
                is_short_episode=True,
                width=1080,
                height=1920,
            )

        self.assertEqual(flux_calls, [(1080, 1920)])
        self.assertEqual(duration_ms, 9000)
        self.assertEqual(beats[0]["media_url"], "cache/x/0.jpg")

    def test_parent_defaults_unchanged(self):
        # Regression: a parent call with no width/height override still gets
        # the original landscape default — the new parameters must not
        # change existing behavior for the existing caller.
        content_id = uuid.uuid4()
        channel = Channel(id=uuid.uuid4(), niche="horror", tone="suspenseful")
        scripts_by_lang = {"en": SimpleNamespace(
            voice_script="[INTRO]\nHello world.\n[OUTRO]\nGoodbye.\n", language="en",
        )}
        audio_by_lang = {"en": SimpleNamespace(
            duration_ms=9000, whisper_transcript=[{"word": "hi", "start": 0.0, "end": 0.5}],
            section_boundaries=None,
        )}
        db = _FakeDb()
        fake_beats = [{
            "section_order": 0, "beat_order": 0, "audio_start_ms": 0, "audio_end_ms": 9000,
            "flux_prompt": "a concrete subject", "media_url": "", "environment": "other",
        }]
        flux_calls: list = []

        def fake_generate_all_beat_images(beats, cid_str, width=1920, height=1080):
            flux_calls.append((width, height))
            return beats

        with (
            patch.object(visual_orchestrator, "split_into_beats", return_value=fake_beats),
            patch.object(visual_orchestrator, "_save_shared_beats", return_value=None),
            patch.object(visual_orchestrator, "generate_all_beat_images",
                         side_effect=fake_generate_all_beat_images),
        ):
            visual_orchestrator._run_visual_pass(
                content_id=content_id,
                scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang,
                channel=channel,
                script_format="youtube_long",
                allow_legacy_fallback=False,
                db=db,
            )

        self.assertEqual(flux_calls, [(1920, 1080)])


# ── Agent 3: gender resolution + 61s floor on a real Solo Short row ──────────

class _AudioFakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _AudioFakeDb:
    def __init__(self, *, content, channel, voices, script):
        self._content = content
        self._channel = channel
        self.voices = voices
        self.scripts = [script]
        self.commits = 0
        self.rollbacks = 0
        self.added: list = []

    def get(self, model, key):
        if model is Content:
            return self._content if key == self._content.id else None
        if model is Channel:
            return self._channel if key == self._channel.id else None
        return None

    def query(self, model):
        if model is ChannelVoice:
            return _AudioFakeQuery(self.voices)
        if model is Script:
            return _AudioFakeQuery(self.scripts)
        if model is AudioFile:
            return _AudioFakeQuery([])
        return _AudioFakeQuery([])

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


def _make_transcript(words: list[str], seconds_per_word: float) -> list[dict]:
    return [
        {"word": w, "start": i * seconds_per_word, "end": (i + 1) * seconds_per_word}
        for i, w in enumerate(words)
    ]


class TestSoloShortAudioGeneration(unittest.TestCase):
    """Real run_audio_generation() on a Solo Short row (is_short_episode=True,
    parent_content_id=None) — only the TTS/Whisper paid boundaries stubbed."""

    def _fixtures(self, *, protagonist_gender: str | None, voices: list, seconds_per_word: float):
        content_id = uuid.uuid4()
        channel = SimpleNamespace(id=uuid.uuid4())
        blueprint = {"protagonist_gender": protagonist_gender} if protagonist_gender else None
        content = SimpleNamespace(
            id=content_id, channel_id=channel.id, is_short_episode=True,
            parent_content_id=None, status="SCRIPTS_VALIDATED",
            story_blueprint=blueprint,
        )
        words = ("A sound echoes from the mountain every single night without "
                 "fail and nobody in town will explain why it happens there").split()
        script = SimpleNamespace(
            content_id=content_id, language="en", version=1, validated=True,
            voice_script=" ".join(words), estimated_duration_sec=None,
        )
        db = _AudioFakeDb(content=content, channel=channel, voices=voices, script=script)
        transcript = _make_transcript(words, seconds_per_word)
        return content_id, db, transcript

    def test_gender_resolves_from_own_blueprint_never_a_parent_lookup(self):
        content_id, db, transcript = self._fixtures(
            protagonist_gender="masculine", voices=[], seconds_per_word=3.5,
        )
        # _fixtures() doesn't know the channel_id it will generate ahead of
        # time, so the real voice rows are built and attached here instead.
        channel_id = db._channel.id
        feminine_voice = SimpleNamespace(language="en", gender="feminine", voice_id="v-fem", channel_id=channel_id)
        masculine_voice = SimpleNamespace(language="en", gender="masculine", voice_id="v-masc", channel_id=channel_id)
        db.voices = [feminine_voice, masculine_voice]

        captured: dict = {}

        def fake_generate_audio(voice_script, channel_voice, is_short_episode=False):
            captured["voice"] = channel_voice
            captured["is_short_episode"] = is_short_episode
            return b"mp3-bytes", []

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", side_effect=fake_generate_audio),
            patch.object(audio_module, "save_audio",
                         return_value=("audio/en.mp3", int(transcript[-1]["end"] * 1000))),
            patch.object(audio_module, "transcribe", return_value=transcript),
        ):
            ok = audio_module.run_audio_generation(content_id, db)

        self.assertTrue(ok)
        # parent_content_id is None — the guarded parent-blueprint fallback in
        # _resolve_target_gender() must never even attempt a lookup; gender
        # comes from this content's own story_blueprint directly.
        self.assertIs(captured["voice"], masculine_voice)
        self.assertTrue(captured["is_short_episode"])
        # Real duration (28 words * 3.5s = 98s) cleared the 61s floor — a
        # real AudioFile row was persisted, not rolled back.
        self.assertGreaterEqual(db.commits, 1)
        self.assertTrue(any(isinstance(r, AudioFile) for r in db.added))

    def test_61s_floor_rejects_a_too_short_solo_short(self):
        # 28 words at 1.0s/word = 28s — well under the 61s hard floor.
        content_id, db, transcript = self._fixtures(
            protagonist_gender="unspecified", voices=[], seconds_per_word=1.0,
        )
        channel_id = db._channel.id
        db.voices = [SimpleNamespace(language="en", gender="feminine", voice_id="v-fem", channel_id=channel_id)]

        with (
            patch.object(audio_module, "audio_path", return_value=_MissingAudioPath()),
            patch.object(audio_module, "generate_audio", return_value=(b"mp3-bytes", [])),
            patch.object(audio_module, "save_audio",
                         return_value=("audio/en.mp3", int(transcript[-1]["end"] * 1000))),
            patch.object(audio_module, "transcribe", return_value=transcript),
        ):
            ok = audio_module.run_audio_generation(content_id, db)

        # Every language failed the floor -> no successful language -> False,
        # and no AudioFile row was persisted for the rejected language.
        self.assertFalse(ok)
        self.assertFalse(any(isinstance(r, AudioFile) for r in db.added))
        self.assertGreaterEqual(db.rollbacks, 1)


# ── Agent 5: real run_video_generation() end-to-end, including Finding E ────

class _RenderFakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _RenderFakeDb:
    def __init__(self):
        self.tables: dict = {}

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def get(self, model, key):
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key:
                return row
        return None

    def query(self, model):
        return _RenderFakeQuery(self.tables.get(model, []))

    def commit(self):
        pass

    def rollback(self):
        pass


class TestSoloShortRenderEndToEnd(unittest.TestCase):
    """Real run_video_generation() -> _process_language() chain for a Solo
    Short — proves short_order/short_total_parts computed from the Content
    row itself (not a caller-supplied override) reach the written props
    file, including Finding E's part_label suppression end-to-end (Phase B
    only proved this calling _process_language() directly)."""

    def test_render_end_to_end_suppresses_part_label_and_writes_props(self):
        content_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        db = _RenderFakeDb()
        db.add(Content(
            id=content_id, channel_id=channel_id, is_short_episode=True,
            parent_content_id=None, short_part_number=1, short_total_parts=1,
            source_language="en", status="CHILD_SHORT_VISUALS_DONE", title="T",
            source_url="https://example.com/x",
        ))
        db.add(Channel(id=channel_id, niche="horror", tone="suspenseful"))
        db.add(ChannelConfig(channel_id=channel_id, subtitle_karaoke_active_color="#FFD700"))
        db.add(Script(
            id=uuid.uuid4(), content_id=content_id, language="en",
            voice_script="Hello world, this is a short story.", version=1, validated=True,
        ))
        db.add(AudioFile(
            id=uuid.uuid4(), content_id=content_id, language="en",
            file_path="audio/x/en.mp3", duration_ms=70000, whisper_transcript=[],
        ))

        fixed_beats = [{
            "section_order": 0, "audio_start_ms": 0, "audio_end_ms": 70000,
            "media_url": "cache/x/0.jpg",
        }]

        with TemporaryDirectory() as tmp:
            with (
                patch.object(settings, "media_path", tmp),
                patch.object(settings, "short_part_label_enabled", True),
                patch.object(video_module, "load_video_sections", return_value=fixed_beats),
                patch.object(video_module, "build_standard_subtitles", return_value=[]),
                patch.object(video_module, "build_karaoke_subtitles", return_value=[]),
                patch.object(video_module, "_collect_technical_blockers", return_value=[]),
                patch.object(video_module, "_check_props_sanity", return_value=(True, "")),
                patch.object(video_module, "_run_short_render", return_value={
                    "file_path": f"{tmp}/video/{content_id}/en_short_0.mp4",
                    "duration_seconds": 70.0, "render_time_seconds": 1.0,
                }),
            ):
                ok = video_module.run_video_generation(content_id, db)

            props_path = Path(tmp) / "remotion_props" / f"{content_id}_en_short_0.json"
            props = json.loads(props_path.read_text())

        self.assertTrue(ok)
        content = db.get(Content, content_id)
        self.assertEqual(content.status, "RENDERED")

        # Finding E, proven end-to-end: a Solo Short's short_total_parts=1
        # (read from the Content row itself, not a test-supplied override)
        # must never render "Part 1 of 1", even with the label setting on.
        self.assertEqual(props["total_parts"], 1)
        self.assertEqual(props["part_label"], "")


# ── Scheduler handoff: a Solo Short through every real pickup query ─────────

class _SchedulerFakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def limit(self, n):
        return _SchedulerFakeQuery(self.rows[:n])

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _SchedulerFakeDb:
    def __init__(self):
        self.tables: dict = {}

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)

    def get(self, model, key):
        for row in self.tables.get(model, []):
            if getattr(row, "id", None) == key:
                return row
        return None

    def query(self, model):
        return _SchedulerFakeQuery(self.tables.get(model, []))

    def commit(self):
        pass

    def close(self):
        pass


class TestSoloShortSchedulerHandoff(unittest.TestCase):
    """Extended scheduler handoff proof (section 11 — the authoritative
    scope; Phase E's own deliverables bullet previously under-described this
    as just pickup_audio_done->pickup_visual_ready). A Solo Short flows
    through every real, unmodified `tasks.py` pickup query from APPROVED
    through visual-ready, each correctly dispatching the next stage's task —
    confirming both section 3's early-pickup verification (pickup_
    approved_content/run_agent2_scripts_for_content/pickup_scripts_validated/
    run_agent3_audio_for_content are purely status-driven, no is_short_episode
    filter) and section 7 step 6's status-string reuse decision
    (CHILD_SHORT_VISUALS_DONE) actually avoid needing any tasks.py changes,
    rather than just asserting it in prose. Only each stage's `.delay()` call
    is stubbed (never enqueue to a real broker); every query and gate runs
    for real against the fake DB.
    """

    def test_solo_short_flows_through_every_pickup_stage(self):
        from app.scheduler import tasks
        from app import database as database_module

        content_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=True,
            parent_content_id=None, short_part_number=1, short_total_parts=1,
            source_language="en", status="APPROVED", title="T",
            source_url="https://example.com/x",
        )
        db = _SchedulerFakeDb()
        db.add(content)

        with (
            patch.object(database_module, "_get_session_factory", return_value=(lambda: db)),
            patch.object(tasks, "_stale_in_progress", return_value=[]),
        ):
            # Stage 1: APPROVED -> Agent 2 script generation.
            with patch.object(tasks.run_agent2_scripts_for_content, "delay") as d1:
                dispatched = tasks.pickup_approved_content()
            self.assertEqual(dispatched, 1)
            d1.assert_called_once_with(str(content_id))

            # Stage 2: SCRIPTS_VALIDATED -> Agent 3 audio generation.
            content.status = "SCRIPTS_VALIDATED"
            with patch.object(tasks.run_agent3_audio_for_content, "delay") as d2:
                dispatched = tasks.pickup_scripts_validated()
            self.assertEqual(dispatched, 1)
            d2.assert_called_once_with(str(content_id))

            # Stage 3: AUDIO_DONE (+ a real AudioFile row) -> Agent 4 visuals.
            content.status = "AUDIO_DONE"
            db.add(AudioFile(
                id=uuid.uuid4(), content_id=content_id, language="en",
                file_path="audio/x/en.mp3", duration_ms=70000,
            ))
            with patch.object(tasks.run_agent4_visual_generation_for_content, "delay") as d3:
                dispatched = tasks.pickup_audio_done()
            self.assertEqual(dispatched, 1)
            d3.assert_called_once_with(str(content_id))

            # Stage 4: CHILD_SHORT_VISUALS_DONE (+ a real, non-"__visual__"
            # VideoSection row — section 7 step 6's reused status string,
            # proven here to already satisfy pickup_visual_ready()'s own
            # defensive VideoSection check with zero tasks.py changes) ->
            # Agent 5 render.
            content.status = "CHILD_SHORT_VISUALS_DONE"
            db.add(VideoSection(
                id=uuid.uuid4(), content_id=content_id, language="en",
                section_order=0, script_text="Hello world.",
                audio_start_ms=0, audio_end_ms=70000,
            ))
            with patch.object(tasks.run_agent5_render_for_content, "delay") as d4:
                dispatched = tasks.pickup_visual_ready()
            self.assertEqual(dispatched, 1)
            d4.assert_called_once_with(str(content_id))


if __name__ == "__main__":
    unittest.main()
