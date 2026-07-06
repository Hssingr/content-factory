"""Runtime proof for roadmap Phase 2d (P0-3) —
code_report/forensic_output_audit_borrasca_run.md.

Two independent reuse mechanisms would otherwise silently resurrect
corrupted timeline data after the P0-1/P0-2 fixes land:

1. The stale-visuals guard (audit V-6b) previously keyed only on the source
   script's SHA-256. The 19 corrupt-timing `__visual__` beats in the audited
   run carried a matching `source_script_sha256`, so a re-run after fixing
   `duration_ms` (with the narration text untouched) would still classify
   them "fresh" and silently reuse the broken timeline. This phase adds a
   second, independent fingerprint — the source-language audio's
   `duration_ms` — checked alongside the script hash; either mismatching is
   enough to force regeneration.

2. Agent 5 logged "Props found on disk — skipping to render" and rendered a
   prior run's stale props file verbatim, even when the underlying
   VideoSection/AudioFile data had since changed. This phase adds
   `_props_are_stale()`, comparing the on-disk props file's own persisted
   duration/section-count/last-section-end against the current DB-backed
   values before ever trusting an existing props file.

This file proves both with real function calls — only the paid Claude/Flux
boundary (`_run_visual_pass`) is stubbed for the Agent 4 integration test;
everything else (hashing, staleness classification, JSON file I/O) is real.
"""

from __future__ import annotations

import hashlib
import json
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

from app.models import VideoSection
from app.agents.agent4_visuals.services.visual_orchestrator import (
    _check_audio_duration_staleness,
    _run_parent_visuals,
    _source_audio_duration_ms,
    _tag_beats_with_audio_duration,
)
from app.agents.agent5_render.services import video as video_module


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Agent 4: audio-duration fingerprint (unit tests) ──────────────────────────

class TestSourceAudioDurationMs(unittest.TestCase):
    def test_returns_source_language_duration(self):
        content = SimpleNamespace(source_language="en")
        audio_by_lang = {
            "en": SimpleNamespace(duration_ms=616_834),
            "fr": SimpleNamespace(duration_ms=650_000),
        }
        self.assertEqual(_source_audio_duration_ms(content, audio_by_lang), 616_834)

    def test_none_when_source_language_missing(self):
        content = SimpleNamespace(source_language="en")
        result = _source_audio_duration_ms(content, {"fr": SimpleNamespace(duration_ms=1000)})
        self.assertIsNone(result)

    def test_none_when_duration_zero_or_missing(self):
        content = SimpleNamespace(source_language="en")
        result = _source_audio_duration_ms(content, {"en": SimpleNamespace(duration_ms=0)})
        self.assertIsNone(result)


class TestTagBeatsWithAudioDuration(unittest.TestCase):
    def test_stamps_every_beat(self):
        beats = [{"beat_order": 0}, {"beat_order": 1}]
        _tag_beats_with_audio_duration(beats, 616_834)
        self.assertTrue(all(b["source_audio_duration_ms"] == 616_834 for b in beats))

    def test_overwrites_existing_value(self):
        beats = [{"source_audio_duration_ms": 161_724}]
        _tag_beats_with_audio_duration(beats, 616_834)
        self.assertEqual(beats[0]["source_audio_duration_ms"], 616_834)


class TestCheckAudioDurationStaleness(unittest.TestCase):
    def test_matching_duration_is_fresh(self):
        beats = [{"source_audio_duration_ms": 616_834}]
        self.assertEqual(_check_audio_duration_staleness(uuid.uuid4(), beats, 616_834), "fresh")

    def test_no_current_duration_is_fresh_fail_open(self):
        beats = [{"source_audio_duration_ms": 161_724}]
        self.assertEqual(_check_audio_duration_staleness(uuid.uuid4(), beats, None), "fresh")

    def test_missing_stored_duration_is_backfill(self):
        beats = [{"source_audio_duration_ms": 0}]
        self.assertEqual(
            _check_audio_duration_staleness(uuid.uuid4(), beats, 616_834), "backfill",
        )

    def test_differing_duration_is_stale_and_logged(self):
        # The exact audited scenario: a corrupted duration_ms corrected in
        # place (161,724 -> 616,834) with the narration text untouched.
        beats = [{"source_audio_duration_ms": 161_724}]
        with self.assertLogs(
            "app.agents.agent4_visuals.services.visual_orchestrator", level="WARNING"
        ) as logs:
            result = _check_audio_duration_staleness(uuid.uuid4(), beats, 616_834)
        self.assertEqual(result, "stale")
        self.assertTrue(any("PARENT_VISUALS_STALE_AUDIO_DURATION" in m for m in logs.output))


# ── Agent 4: combined guard integration (real _run_parent_visuals(), only the
# paid _run_visual_pass() boundary stubbed) ───────────────────────────────────

class _FakeQuery:
    def __init__(self, table: list, predicate=None):
        self._table = table
        self._predicate = predicate or (lambda row: True)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return [row for row in self._table if self._predicate(row)]

    def delete(self):
        matched = self.all()
        for row in matched:
            self._table.remove(row)
        return len(matched)


class _FakeDb:
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


def _beat(order: int, **overrides) -> dict:
    beat = {
        "beat_order": order, "section_order": order,
        "audio_start_ms": order * 3000, "audio_end_ms": (order + 1) * 3000,
        "script_text": f"narration {order}", "visual_intent": f"intent {order}",
        "visual_type": "b-roll", "visual_category": "object",
        "environment": "indoor_office",
        "flux_prompt": f"concrete subject {order}, wide shot, photorealistic",
        "effect": "cut", "color_grade": "desaturated", "transition_to_next": "cut",
        "motif": "object", "beat_intensity": "medium", "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated", "media_url": "cache/parent-id/abc.jpg",
        "media_type": "image",
    }
    beat.update(overrides)
    return beat


def _seed_visual_beats(db: _FakeDb, content_id, beats: list[dict]) -> None:
    from app.agents.agent4_visuals.services.visual_orchestrator import _beat_extras
    for beat in beats:
        db.add(VideoSection(
            content_id=content_id, language="__visual__", section_order=beat["section_order"],
            script_text=beat.get("script_text", ""), audio_start_ms=beat.get("audio_start_ms", 0),
            audio_end_ms=beat.get("audio_end_ms", 0), flux_prompt=beat.get("flux_prompt", ""),
            generation_prompt=json.dumps(_beat_extras(beat), ensure_ascii=False),
            effect=beat.get("effect"), color_grade=beat.get("color_grade"),
            beat_intensity=beat.get("beat_intensity"),
            suggested_duration_sec=beat.get("suggested_duration_sec"),
            media_strategy=beat.get("media_strategy"), text_card_style=beat.get("text_card_style"),
        ))


class TestCombinedStalenessGuardIntegration(unittest.TestCase):
    def _content_channel(self, content_id):
        content = SimpleNamespace(id=content_id, source_language="en", story_blueprint=None)
        channel = SimpleNamespace(id=uuid.uuid4(), niche="mystery", tone="tense")
        return content, channel

    def test_same_script_but_corrected_duration_triggers_regeneration(self):
        """The exact P0-3 repair scenario: script unchanged, duration_ms
        corrected from a corrupted value to the real one — the script-hash
        fingerprint alone would call this 'fresh' and reuse the broken
        timeline; the audio-duration fingerprint must catch it."""
        content_id = uuid.uuid4()
        db = _FakeDb()
        matching_script_hash = _sha("Original narration text")
        _seed_visual_beats(db, content_id, [
            _beat(0, source_script_sha256=matching_script_hash, source_audio_duration_ms=161_724),
            _beat(1, source_script_sha256=matching_script_hash, source_audio_duration_ms=161_724),
        ])
        content, channel = self._content_channel(content_id)
        # Same script text as before (so the script hash matches)...
        scripts_by_lang = {"en": SimpleNamespace(voice_script="Original narration text")}
        # ...but the AudioFile row's duration has since been corrected.
        audio_by_lang = {"en": SimpleNamespace(duration_ms=616_834, whisper_transcript=[])}

        fresh_beats = [_beat(0, script_text="regenerated"), _beat(1, script_text="regenerated")]
        calls = []

        def fake_run_visual_pass(**kwargs):
            calls.append(kwargs)
            return fresh_beats, 616_834

        with patch(
            "app.agents.agent4_visuals.services.visual_orchestrator._run_visual_pass",
            side_effect=fake_run_visual_pass,
        ):
            result = _run_parent_visuals(
                content_id=content_id, content=content, scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang, channel=channel, script_format="youtube_long",
                allow_legacy_fallback=True, db=db,
            )

        self.assertEqual(len(calls), 1, "must regenerate despite a matching script hash")
        self.assertEqual(result["status"], "PARENT_VISUALS_DONE")
        self.assertEqual(len(result["beats_by_lang"]["en"]), 2)

    def test_matching_script_and_duration_reuses_without_regeneration(self):
        content_id = uuid.uuid4()
        db = _FakeDb()
        matching_script_hash = _sha("Original narration text")
        _seed_visual_beats(db, content_id, [
            _beat(0, source_script_sha256=matching_script_hash, source_audio_duration_ms=616_834),
            _beat(1, source_script_sha256=matching_script_hash, source_audio_duration_ms=616_834),
        ])
        content, channel = self._content_channel(content_id)
        scripts_by_lang = {"en": SimpleNamespace(voice_script="Original narration text")}
        audio_by_lang = {"en": SimpleNamespace(duration_ms=616_834, whisper_transcript=[])}

        with patch(
            "app.agents.agent4_visuals.services.visual_orchestrator._run_visual_pass",
            side_effect=AssertionError("must not regenerate when both fingerprints match"),
        ):
            result = _run_parent_visuals(
                content_id=content_id, content=content, scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang, channel=channel, script_format="youtube_long",
                allow_legacy_fallback=True, db=db,
            )

        self.assertEqual(result["status"], "PARENT_VISUALS_DONE")
        self.assertEqual(len(result["beats_by_lang"]["en"]), 2)

    def test_legacy_beats_without_audio_fingerprint_are_backfilled_not_regenerated(self):
        content_id = uuid.uuid4()
        db = _FakeDb()
        matching_script_hash = _sha("Original narration text")
        # Legacy row: has the script hash (from before this phase) but no
        # source_audio_duration_ms key at all.
        legacy_beat = _beat(0, source_script_sha256=matching_script_hash)
        _seed_visual_beats(db, content_id, [legacy_beat])
        content, channel = self._content_channel(content_id)
        scripts_by_lang = {"en": SimpleNamespace(voice_script="Original narration text")}
        audio_by_lang = {"en": SimpleNamespace(duration_ms=616_834, whisper_transcript=[])}

        with patch(
            "app.agents.agent4_visuals.services.visual_orchestrator._run_visual_pass",
            side_effect=AssertionError("must not regenerate on legacy backfill"),
        ):
            result = _run_parent_visuals(
                content_id=content_id, content=content, scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang, channel=channel, script_format="youtube_long",
                allow_legacy_fallback=True, db=db,
            )

        self.assertEqual(result["status"], "PARENT_VISUALS_DONE")

        from app.agents.agent4_visuals.services.visual_orchestrator import _load_shared_beats
        reloaded = _load_shared_beats(content_id, db)
        self.assertEqual(reloaded[0]["source_audio_duration_ms"], 616_834)


# ── Agent 5: _props_are_stale() ────────────────────────────────────────────────

class TestPropsAreStale(unittest.TestCase):
    def _write_props(self, path: Path, *, duration_ms: int, sections: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"duration_ms": duration_ms, "sections": sections}))

    def test_matching_props_are_not_stale(self):
        with TemporaryDirectory() as tmp:
            props_path = Path(tmp) / "props.json"
            beats = [{"audio_end_ms": 3000}, {"audio_end_ms": 6000}]
            self._write_props(props_path, duration_ms=6000, sections=beats)
            self.assertFalse(video_module._props_are_stale(props_path, beats, 6000))

    def test_duration_mismatch_is_stale(self):
        # The exact audited scenario: the DB's AudioFile.duration_ms was
        # corrected (161,724 -> 616,834) but the props file on disk still
        # carries the old, corrupted value.
        with TemporaryDirectory() as tmp:
            props_path = Path(tmp) / "props.json"
            beats = [{"audio_end_ms": 161_724}]
            self._write_props(props_path, duration_ms=161_724, sections=beats)
            self.assertTrue(video_module._props_are_stale(props_path, beats, 616_834))

    def test_section_count_mismatch_is_stale(self):
        with TemporaryDirectory() as tmp:
            props_path = Path(tmp) / "props.json"
            old_beats = [{"audio_end_ms": 3000}]
            self._write_props(props_path, duration_ms=6000, sections=old_beats)
            new_beats = [{"audio_end_ms": 3000}, {"audio_end_ms": 6000}]
            self.assertTrue(video_module._props_are_stale(props_path, new_beats, 6000))

    def test_last_section_end_mismatch_is_stale(self):
        with TemporaryDirectory() as tmp:
            props_path = Path(tmp) / "props.json"
            old_beats = [{"audio_end_ms": 5000}]
            self._write_props(props_path, duration_ms=6000, sections=old_beats)
            new_beats = [{"audio_end_ms": 6000}]
            self.assertTrue(video_module._props_are_stale(props_path, new_beats, 6000))

    def test_missing_file_is_stale(self):
        with TemporaryDirectory() as tmp:
            props_path = Path(tmp) / "does_not_exist.json"
            self.assertTrue(video_module._props_are_stale(props_path, [{"audio_end_ms": 1000}], 1000))

    def test_corrupt_json_is_stale(self):
        with TemporaryDirectory() as tmp:
            props_path = Path(tmp) / "corrupt.json"
            props_path.write_text("{not valid json")
            self.assertTrue(video_module._props_are_stale(props_path, [{"audio_end_ms": 1000}], 1000))


class TestProcessLanguageRebuildsWhenPropsAreStale(unittest.TestCase):
    """Wiring proof: _process_language() must not take the
    _render_from_existing_props() shortcut when _props_are_stale() says the
    on-disk props no longer match the current DB beats — it must fall
    through toward the normal rebuild path instead."""

    def _common_patches(self, tmp: str, *, is_stale: bool):
        content_id = uuid.uuid4()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None  # not yet rendered

        props_dir = Path(tmp) / "remotion_props"
        props_dir.mkdir(parents=True, exist_ok=True)
        props_path = props_dir / f"{content_id}_en_main.json"
        props_path.write_text(json.dumps({"duration_ms": 1, "sections": []}))

        audio = SimpleNamespace(duration_ms=9000, whisper_transcript=[], file_path="audio/en.mp3")
        channel = SimpleNamespace(id=uuid.uuid4())

        return content_id, db, props_dir, audio, channel

    def test_stale_props_skip_the_existing_props_shortcut(self):
        with TemporaryDirectory() as tmp:
            content_id, db, props_dir, audio, channel = self._common_patches(tmp, is_stale=True)

            with (
                patch.object(video_module.settings, "media_path", tmp),
                patch.object(video_module, "_props_are_stale", return_value=True),
                patch.object(
                    video_module, "_render_from_existing_props",
                    side_effect=AssertionError("must not reuse stale props"),
                ),
                patch.object(video_module, "build_standard_subtitles", return_value=[]),
                patch.object(video_module, "build_karaoke_subtitles", return_value=[]),
            ):
                # An empty beats list trips the no_beats technical blocker
                # immediately after the existing-props phase check — enough
                # to observe that we fell through past it without needing to
                # mock the entire render pipeline.
                result = video_module._process_language(
                    content_id=content_id, language="en", script=SimpleNamespace(),
                    audio=audio, beats=[], channel=channel,
                    channel_style="documentary", channel_color_grade="desaturated",
                    karaoke_color="#fff", db=db,
                )

        self.assertFalse(result)  # no_beats blocker fires on the rebuild path

    def test_fresh_props_take_the_existing_props_shortcut(self):
        with TemporaryDirectory() as tmp:
            content_id, db, props_dir, audio, channel = self._common_patches(tmp, is_stale=False)

            with (
                patch.object(video_module.settings, "media_path", tmp),
                patch.object(video_module, "_props_are_stale", return_value=False),
                patch.object(video_module, "_render_from_existing_props", return_value=True),
            ):
                result = video_module._process_language(
                    content_id=content_id, language="en", script=SimpleNamespace(),
                    audio=audio, beats=[{"audio_end_ms": 9000}], channel=channel,
                    channel_style="documentary", channel_color_grade="desaturated",
                    karaoke_color="#fff", db=db,
                )

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
