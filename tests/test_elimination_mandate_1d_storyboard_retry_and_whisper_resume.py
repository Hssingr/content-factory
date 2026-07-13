"""Runtime proof for Elimination Mandate Phase 1d (D1.5, D1.6) —
code_report/forensic_output_audit_borrasca_run.md.

D1.5: parent storyboard MAJOR-finding segment regeneration
(``retry_segment_constraints`` path in ``split_into_beats()``) and child MAJOR
remediation (``remediate_child_major_storyboard_issues()``) are deleted — a
real production run showed MAJOR findings never actually blocked the
pipeline either way (a failed retry/remediation just logged and proceeded
with the original beats), so both mechanisms were paid/surgical
regeneration driven by advisory findings that never gated anything.

D1.6: Whisper re-transcription on resume is deleted — when an ``AudioFile``
row already has both a duration and a transcript for a (content, language)
pair whose audio file is already on disk (unchanged), ``run_audio_generation()``
now reuses the stored values instead of paying for a second transcription of
the same audio.

This file proves:
1. ``_run_storyboard_validation()`` never calls ``split_into_beats()`` (the
   real Claude boundary) even when the storyboard has MAJOR findings — it
   returns the beats unchanged and only logs.
2. ``_run_child_short_visuals()`` never calls a remediation function on
   MAJOR findings in the remapped child beats — the beat actually persisted
   to the (fake, in-memory) DB still carries its MAJOR-triggering
   ``flux_prompt`` verbatim.
3. ``remediate_child_major_storyboard_issues``, the batch-provenance
   machinery, and ``retry_segment_constraints``/``existing_beats`` are
   genuinely gone from the module surface, not just unused.
4. ``run_audio_generation()`` skips ``transcribe()`` entirely when an
   existing ``AudioFile`` row already has a transcript + duration for
   already-on-disk audio, and reuses the stored values verbatim.
"""

from __future__ import annotations

import inspect
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent4_visuals.services import visual_orchestrator as vo
from app.agents.agent4_visuals.subagents import storyboard as storyboard_module
from app.models import AudioFile, VideoSection


def _major_triggering_beat(order: int = 0) -> dict:
    """A beat whose flux_prompt trips validate_storyboard()'s
    forbidden_flux_word / ai_text_rendering_requested MAJOR checks."""
    return {
        "beat_order": order,
        "section_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": "narration",
        "visual_intent": "a mysterious hallway",
        "visual_type": "b-roll",
        "visual_category": "place",
        "environment": "corridor_interior",
        "flux_prompt": 'Mysterious cinematic hallway with a sign that reads "LEAVE NOW"',
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "doorway",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
        "media_type": "image",
        "start_hint": "a",
        "end_hint": "b",
    }


class TestParentStoryboardValidationIsTelemetryOnly(unittest.TestCase):
    def test_major_findings_never_trigger_split_into_beats_retry(self):
        beats = [_major_triggering_beat(0)]

        with patch.object(
            vo, "split_into_beats",
            side_effect=AssertionError("must never retry — D1.5 deleted the segment retry"),
        ):
            with self.assertLogs(
                "app.agents.agent4_visuals.services.visual_orchestrator", level="WARNING"
            ) as logs:
                result = vo._run_storyboard_validation(beats)

        self.assertIs(result, beats)
        self.assertEqual(result[0]["flux_prompt"], beats[0]["flux_prompt"])
        self.assertTrue(any("MAJOR" in m for m in logs.output))

    def test_clean_storyboard_returns_unchanged(self):
        beats = [{
            "beat_order": 0, "section_order": 0, "audio_start_ms": 0, "audio_end_ms": 3000,
            "script_text": "narration", "visual_intent": "a locked wooden door with brass hinges",
            "visual_type": "b-roll", "visual_category": "object", "environment": "corridor_interior",
            "flux_prompt": "A weathered brass door handle catching afternoon light, close-up, photorealistic",
            "effect": "cut", "color_grade": "desaturated", "transition_to_next": "cut",
            "motif": "doorway", "beat_intensity": "medium", "suggested_duration_sec": 3.0,
            "media_strategy": "flux_generated", "media_url": "", "media_type": "image",
            "start_hint": "a", "end_hint": "b",
        }]
        with patch.object(
            vo, "split_into_beats",
            side_effect=AssertionError("must never retry on a clean storyboard either"),
        ):
            result = vo._run_storyboard_validation(beats)
        self.assertIs(result, beats)


class TestRetryMachineryFullyRemoved(unittest.TestCase):
    def test_orchestrator_helpers_gone(self):
        for name in (
            "_storyboard_retry_constraints_by_batch",
            "_storyboard_batch_labels",
            "_documentish_batch_labels",
            "_issue_batch_labels",
            "_SEGMENT_RETRY_SUPPORT_CHECKS",
            "remediate_child_major_storyboard_issues",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(vo, name))

    def test_storyboard_module_helpers_gone(self):
        for name in (
            "remediate_child_major_storyboard_issues",
            "_regenerate_child_prompt_from_visual_intent",
            "_child_environment_phrase",
            "_strip_child_prompt_repair_text",
            "_tag_batch_provenance",
            "_group_existing_beats_by_batch",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(storyboard_module, name))

    def test_split_into_beats_no_longer_accepts_retry_params(self):
        params = inspect.signature(storyboard_module.split_into_beats).parameters
        self.assertNotIn("retry_segment_constraints", params)
        self.assertNotIn("existing_beats", params)

    def test_run_storyboard_validation_no_longer_accepts_retry_params(self):
        params = inspect.signature(vo._run_storyboard_validation).parameters
        self.assertEqual(list(params), ["beats"])


# ── Fake DB (in-memory, no SQL) — same precedent as test_stale_visuals_guard.py ──

class _FakeQuery:
    def __init__(self, table: list, predicate=None):
        self._table = table
        self._predicate = predicate or (lambda row: True)

    def filter(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        matched = self.all()
        return matched[0] if matched else None

    def all(self):
        return [row for row in self._table if self._predicate(row)]

    def delete(self):
        matched = self.all()
        for row in matched:
            self._table.remove(row)
        return len(matched)


class _FakeDb:
    """In-memory VideoSection table, scoped per query by content_id+language
    since the fake ignores real SQLAlchemy filter clauses — the test only
    ever needs simple content_id/language partitioning, matched by hand."""

    def __init__(self):
        self.video_sections: list[VideoSection] = []
        self.commit_count = 0

    def query(self, model):
        if model is VideoSection:
            return _FakeQuery(self.video_sections)
        return _FakeQuery([])

    def add(self, row):
        self.video_sections.append(row)

    def flush(self):
        pass

    def commit(self):
        self.commit_count += 1


class TestChildShortVisualsSkipsRemediation(unittest.TestCase):
    def test_major_findings_in_remap_persist_unchanged_and_are_logged(self):
        content_id = uuid.uuid4()
        parent_content_id = uuid.uuid4()
        db = _FakeDb()
        # Parent visual readiness gate needs a __visual__ row for parent_content_id.
        db.video_sections.append(VideoSection(
            content_id=parent_content_id, language="__visual__", section_order=0,
            script_text="", audio_start_ms=0, audio_end_ms=0,
        ))

        content = SimpleNamespace(id=content_id, parent_content_id=parent_content_id)
        channel = SimpleNamespace(niche="true crime", tone="suspenseful")
        script = SimpleNamespace(voice_script="Sam finds a sign in the hallway.")
        scripts_by_lang = {"en": script}
        audio = SimpleNamespace(duration_ms=6000, whisper_transcript=[])
        audio_by_lang = {"en": audio}

        remap_beats = [_major_triggering_beat(0)]

        with (
            patch.object(vo, "remap_beats_for_short", return_value=remap_beats),
            patch.object(vo, "generate_pending_beat_images", side_effect=lambda beats, cid: beats),
            patch.object(vo, "save_beat_review_metadata", return_value=None),
        ):
            with self.assertLogs(
                "app.agents.agent4_visuals.services.visual_orchestrator", level="ERROR"
            ) as logs:
                result = vo._run_child_short_visuals(content, channel, scripts_by_lang, audio_by_lang, db)

        self.assertEqual(result["status"], "CHILD_SHORT_VISUALS_DONE")
        self.assertTrue(
            any("telemetry only" in m and "no remediation attempted" in m for m in logs.output),
            logs.output,
        )

        # Persisted beat still carries the MAJOR-triggering prompt verbatim —
        # no remediation function ever rewrote it.
        persisted = [row for row in db.video_sections if row.language == "en"]
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].flux_prompt, remap_beats[0]["flux_prompt"])


class TestWhisperResumeReusesExistingTranscript(unittest.TestCase):
    """D1.6: run_audio_generation() must not re-transcribe unchanged,
    already-on-disk audio when a real transcript + duration is already
    persisted for that (content, language)."""

    def _run_with_fixture(self, *, existing_audio_file_kwargs: dict):
        from app.agents.agent3_audio.services import audio as audio_module

        content_id = uuid.uuid4()
        content = SimpleNamespace(
            id=content_id, channel_id=uuid.uuid4(), is_short_episode=False, story_blueprint=None,
        )
        channel = SimpleNamespace(id=content.channel_id)
        voice = SimpleNamespace(language="en", voice_id="voice-1", provider="cartesia", gender="feminine")
        script = SimpleNamespace(voice_script="Some validated narration.", estimated_duration_sec=None)

        existing_audio_file = AudioFile(
            id=uuid.uuid4(), content_id=content_id, language="en",
            file_path="audio/en.mp3", **existing_audio_file_kwargs,
        )

        class _Query:
            def __init__(self, rows):
                self._rows = rows

            def filter(self, *a, **k):
                return self

            def all(self):
                return list(self._rows)

            def first(self):
                return self._rows[0] if self._rows else None

            def order_by(self, *a, **k):
                return self

        class _Db:
            def get(self, model, key):
                if model.__name__ == "Content" and key == content_id:
                    return content
                if model.__name__ == "Channel" and key == content.channel_id:
                    return channel
                return None

            def query(self, model):
                if model.__name__ == "ChannelVoice":
                    return _Query([voice])
                if model.__name__ == "AudioFile":
                    return _Query([existing_audio_file])
                return _Query([])

            def commit(self):
                pass

            def rollback(self):
                pass

            def flush(self):
                pass

        db = _Db()

        transcribe_calls = []

        def fake_transcribe(*args, **kwargs):
            transcribe_calls.append((args, kwargs))
            return [{"word": "should", "start": 0.0, "end": 60.9}]

        with (
            patch.object(audio_module, "_load_latest_scripts", return_value={"en": script}),
            patch.object(audio_module, "ensure_run_dirs", return_value=None),
            patch.object(
                audio_module, "audio_path",
                return_value=SimpleNamespace(exists=lambda: True),
            ),
            patch.object(audio_module, "transcribe", side_effect=fake_transcribe),
            patch.object(audio_module, "_assert_short_audio_min_duration", return_value=True),
            patch.object(audio_module, "_assert_short_audio_has_transcript", return_value=True),
            patch.object(audio_module, "measure_audio_duration_ms", return_value=61_000),
        ):
            result = audio_module.run_audio_generation(content_id, db)

        return result, transcribe_calls, content

    def test_existing_transcript_and_duration_skip_whisper(self):
        result, transcribe_calls, content = self._run_with_fixture(
            existing_audio_file_kwargs={
                "duration_ms": 61_000,
                "whisper_transcript": [{"word": "hello", "start": 0.0, "end": 60.9}],
            },
        )
        self.assertTrue(result)
        self.assertEqual(transcribe_calls, [])
        self.assertEqual(content.status, "AUDIO_DONE")

    def test_missing_existing_transcript_falls_through_to_whisper(self):
        result, transcribe_calls, content = self._run_with_fixture(
            existing_audio_file_kwargs={"duration_ms": 61_000, "whisper_transcript": []},
        )
        self.assertTrue(result)
        self.assertEqual(len(transcribe_calls), 1)


if __name__ == "__main__":
    unittest.main()
