"""Runtime proof for the stale-visuals guard (audit V-6b).

The shared `__visual__` beats are keyed only by content_id — nothing ties
them to the source-language narration they were built from. If the parent's
source script is regenerated after the visual pass already ran (operator
`--force-scripts`, or any retry producing a different script), the OLD beats
must never be silently reused against the NEW narration.

Unit tests exercise the three new pure functions directly. The integration
test drives the REAL `_run_parent_visuals()` against a fake (in-memory,
no SQL) DB session built from real `VideoSection` ORM instances — proving the
real `_beat_extras()`/`_save_video_sections()`/`load_video_sections()`
(shared loader, roadmap 6.5) JSON round trip, with only `_run_visual_pass()`
(which would call paid Claude/fal.ai) stubbed. Per this codebase's own precedent
(`tests/test_agent4_media_validation.py`'s `_FakeDb`), swapping real SQL
execution for an in-memory Python list is not "stubbing internal logic" —
it stubs infrastructure so the actual business-logic functions run for real.
"""

import hashlib
import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.models import VideoSection
from app.agents.agent4_visuals.services.visual_orchestrator import (
    _check_shared_beats_staleness,
    _run_parent_visuals,
    _source_script_hash,
    _tag_beats_with_script_hash,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _beat(order: int, **overrides) -> dict:
    beat = {
        "beat_order": order,
        "section_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "script_text": f"narration {order}",
        "visual_intent": f"intent {order}",
        "visual_type": "b-roll",
        "visual_category": "object",
        "environment": "indoor_office",
        "flux_prompt": f"concrete subject {order}, wide shot, photorealistic",
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "object",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "cache/parent-id/abc.jpg",
        "media_type": "image",
    }
    beat.update(overrides)
    return beat


class TestSourceScriptHash(unittest.TestCase):
    def test_returns_sha256_of_source_language_script(self):
        content = SimpleNamespace(source_language="en")
        scripts_by_lang = {
            "en": SimpleNamespace(voice_script="Hello world narration"),
            "fr": SimpleNamespace(voice_script="Bonjour le monde"),
        }
        result = _source_script_hash(content, scripts_by_lang)
        self.assertEqual(result, _sha("Hello world narration"))

    def test_none_when_source_language_missing(self):
        content = SimpleNamespace(source_language="en")
        result = _source_script_hash(content, {"fr": SimpleNamespace(voice_script="x")})
        self.assertIsNone(result)

    def test_none_when_voice_script_empty(self):
        content = SimpleNamespace(source_language="en")
        result = _source_script_hash(content, {"en": SimpleNamespace(voice_script="")})
        self.assertIsNone(result)

    def test_different_text_yields_different_hash(self):
        content = SimpleNamespace(source_language="en")
        h1 = _source_script_hash(content, {"en": SimpleNamespace(voice_script="Version one")})
        h2 = _source_script_hash(content, {"en": SimpleNamespace(voice_script="Version two")})
        self.assertNotEqual(h1, h2)


class TestTagBeatsWithScriptHash(unittest.TestCase):
    def test_stamps_every_beat(self):
        beats = [_beat(0), _beat(1), _beat(2)]
        _tag_beats_with_script_hash(beats, "deadbeef")
        self.assertTrue(all(b["source_script_sha256"] == "deadbeef" for b in beats))

    def test_overwrites_existing_hash(self):
        beats = [_beat(0, source_script_sha256="old")]
        _tag_beats_with_script_hash(beats, "new")
        self.assertEqual(beats[0]["source_script_sha256"], "new")


class TestCheckSharedBeatsStaleness(unittest.TestCase):
    def test_matching_hash_is_fresh(self):
        h = _sha("narration")
        beats = [_beat(0, source_script_sha256=h), _beat(1, source_script_sha256=h)]
        self.assertEqual(_check_shared_beats_staleness(uuid.uuid4(), beats, h), "fresh")

    def test_no_current_hash_is_fresh_fail_open(self):
        beats = [_beat(0, source_script_sha256=_sha("narration"))]
        self.assertEqual(_check_shared_beats_staleness(uuid.uuid4(), beats, None), "fresh")

    def test_missing_stored_hash_is_backfill(self):
        beats = [_beat(0, source_script_sha256="")]
        self.assertEqual(
            _check_shared_beats_staleness(uuid.uuid4(), beats, _sha("narration")), "backfill"
        )

    def test_differing_hash_is_stale_and_logged(self):
        beats = [_beat(0, source_script_sha256=_sha("OLD narration"))]
        with self.assertLogs(
            "app.agents.agent4_visuals.services.visual_orchestrator", level="WARNING"
        ) as logs:
            result = _check_shared_beats_staleness(uuid.uuid4(), beats, _sha("NEW narration"))
        self.assertEqual(result, "stale")
        self.assertTrue(any("PARENT_VISUALS_STALE_SCRIPT_HASH" in m for m in logs.output))


# ── Fake DB (in-memory, no SQL) — same precedent as test_agent4_media_validation.py ──

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
    """In-memory VideoSection table — real ORM instances, no SQL execution."""

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

    def get(self, model, obj_id):
        return None


def _seed_visual_beats(db: _FakeDb, content_id, beats: list[dict]) -> None:
    """Persist beats via the REAL _beat_extras()/JSON path, bypassing
    _save_video_sections() only to avoid importing it circularly — same
    shape it produces."""
    from app.agents.agent4_visuals.services.visual_orchestrator import _beat_extras
    for beat in beats:
        db.add(VideoSection(
            content_id=content_id,
            language="__visual__",
            section_order=beat["section_order"],
            script_text=beat.get("script_text", ""),
            audio_start_ms=beat.get("audio_start_ms", 0),
            audio_end_ms=beat.get("audio_end_ms", 0),
            flux_prompt=beat.get("flux_prompt", ""),
            effect=beat.get("effect"),
            color_grade=beat.get("color_grade"),
            generation_prompt=json.dumps(_beat_extras(beat), ensure_ascii=False),
            beat_intensity=beat.get("beat_intensity"),
            suggested_duration_sec=beat.get("suggested_duration_sec"),
            media_strategy=beat.get("media_strategy"),
            text_card_style=beat.get("text_card_style"),
        ))


class TestRunParentVisualsIntegration(unittest.TestCase):
    """Drives the real _run_parent_visuals() with only _run_visual_pass()
    stubbed (the paid-API boundary)."""

    def _content_channel(self, content_id):
        content = SimpleNamespace(id=content_id, source_language="en")
        channel = SimpleNamespace(id=uuid.uuid4(), niche="mystery", tone="tense")
        return content, channel

    def test_fresh_hash_match_reuses_beats_without_regeneration(self):
        content_id = uuid.uuid4()
        db = _FakeDb()
        matching_hash = _sha("Original narration text")
        _seed_visual_beats(db, content_id, [
            _beat(0, source_script_sha256=matching_hash),
            _beat(1, source_script_sha256=matching_hash),
        ])
        content, channel = self._content_channel(content_id)
        scripts_by_lang = {"en": SimpleNamespace(voice_script="Original narration text")}
        audio_by_lang = {"en": SimpleNamespace(duration_ms=6000, whisper_transcript=[])}

        with patch(
            "app.agents.agent4_visuals.services.visual_orchestrator._run_visual_pass",
            side_effect=AssertionError("must not regenerate on a hash match"),
        ):
            result = _run_parent_visuals(
                content_id=content_id, content=content, scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang, channel=channel, script_format="youtube_long",
                allow_legacy_fallback=True, db=db,
            )

        self.assertEqual(result["status"], "PARENT_VISUALS_DONE")
        self.assertEqual(len(result["beats_by_lang"]["en"]), 2)

    def test_stale_hash_mismatch_triggers_regeneration_with_new_hash(self):
        content_id = uuid.uuid4()
        db = _FakeDb()
        _seed_visual_beats(db, content_id, [
            _beat(0, source_script_sha256=_sha("OLD narration text")),
        ])
        content, channel = self._content_channel(content_id)
        new_text = "COMPLETELY DIFFERENT narration after --force-scripts"
        scripts_by_lang = {"en": SimpleNamespace(voice_script=new_text)}
        audio_by_lang = {"en": SimpleNamespace(duration_ms=9000, whisper_transcript=[])}

        fresh_beats = [_beat(0, script_text="fresh"), _beat(1, script_text="fresh")]
        calls = []

        def fake_run_visual_pass(**kwargs):
            calls.append(kwargs)
            return fresh_beats, 9000

        with patch(
            "app.agents.agent4_visuals.services.visual_orchestrator._run_visual_pass",
            side_effect=fake_run_visual_pass,
        ):
            result = _run_parent_visuals(
                content_id=content_id, content=content, scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang, channel=channel, script_format="youtube_long",
                allow_legacy_fallback=True, db=db,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["script_hash"], _sha(new_text))
        self.assertEqual(result["status"], "PARENT_VISUALS_DONE")
        self.assertEqual(len(result["beats_by_lang"]["en"]), 2)

    def test_legacy_beats_without_hash_are_backfilled_not_regenerated(self):
        content_id = uuid.uuid4()
        db = _FakeDb()
        # No source_script_sha256 key at all — simulates a row saved before
        # this guard existed.
        legacy_beat = _beat(0)
        del legacy_beat["media_url"]
        legacy_beat["media_url"] = "cache/parent-id/legacy.jpg"
        _seed_visual_beats(db, content_id, [legacy_beat])
        content, channel = self._content_channel(content_id)
        current_text = "Whatever the current script is"
        scripts_by_lang = {"en": SimpleNamespace(voice_script=current_text)}
        audio_by_lang = {"en": SimpleNamespace(duration_ms=3000, whisper_transcript=[])}

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

        # Backfill really persisted: reloading the __visual__ rows now shows
        # the current hash, proving the real save/load round trip.
        from app.agents.agent4_visuals.services.visual_orchestrator import _load_shared_beats
        reloaded = _load_shared_beats(content_id, db)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["source_script_sha256"], _sha(current_text))


if __name__ == "__main__":
    unittest.main()
