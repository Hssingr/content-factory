"""Runtime proof for the _mark_script_validated bypass fix (roadmap 4.4 /
audit S-4, exec-7).

Before this fix, `generate_multilingual_scripts()` blindly force-validated
ANY pre-existing Script row for a required language on retry
(`_mark_script_validated(existing)`), including a row that never reached
`validated=True` — "one failed run + one retry = an unvalidated translation
flowing to TTS."

This drives the REAL `generate_multilingual_scripts()` against a small,
SQLAlchemy-expression-aware fake DB (no live SQL connection, but real
`.filter()` conditions built by the real ORM columns are actually evaluated
— a stricter fidelity bar than a "return everything, ignore filters" fake,
appropriate here since two different queries hit the same `Script` table
with different filters in the same function call). Only the paid-API-adjacent
`_generate_validated_translated_parent_script()` is stubbed.
"""

from __future__ import annotations

import operator as _operator
import unittest
import uuid
from unittest.mock import patch

from app.agents.agent2_discovery.services import scripts
from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelVoice, Content, Script


def _condition_value(condition):
    right = getattr(condition, "right", None)
    if hasattr(right, "value"):
        return right.value
    type_name = type(right).__name__
    if type_name == "True_":
        return True
    if type_name == "False_":
        return False
    return right


def _row_matches(row, conditions) -> bool:
    for cond in conditions:
        attr = cond.left.key
        expected = _condition_value(cond)
        actual = getattr(row, attr, None)
        if cond.operator not in (_operator.eq,) and cond.operator.__name__ not in ("eq", "is_"):
            raise NotImplementedError(f"Unsupported operator in fake query: {cond.operator}")
        if actual != expected:
            return False
    return True


class _FakeQuery:
    def __init__(self, rows: list, conditions: tuple = ()):
        self._rows = rows
        self._conditions = conditions

    def join(self, *a, **k):
        return self

    def filter(self, *conditions):
        return _FakeQuery(self._rows, self._conditions + conditions)

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _FakeQuery(self._rows[:n], self._conditions)

    def all(self) -> list:
        return [r for r in self._rows if _row_matches(r, self._conditions)]

    def first(self):
        matched = self.all()
        return matched[0] if matched else None


class _FakeDb:
    """Generic per-model table store with real SQLAlchemy filter evaluation."""

    def __init__(self):
        self.tables: dict[type, list] = {
            Script: [], ChannelConfig: [], ChannelLanguage: [], ChannelVoice: [],
        }
        self.commits = 0
        self.added: list = []

    def query(self, model):
        return _FakeQuery(self.tables.get(model, []))

    def add(self, row):
        self.tables.setdefault(type(row), []).append(row)
        self.added.append(row)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1


def _fake_adapted(voice_script: str) -> dict:
    return {"voice_script": voice_script}


class TestScriptValidationBypassFix(unittest.TestCase):
    def _base_fixtures(self):
        channel_id = uuid.uuid4()
        content_id = uuid.uuid4()
        channel = Channel(id=channel_id, niche="horror", tone="tense")
        content = Content(
            id=content_id, channel_id=channel_id, is_short_episode=False,
            source_language="en", status="GENERATING_SCRIPTS",
        )
        db = _FakeDb()
        db.add(ChannelConfig(channel_id=channel_id, script_format="youtube_long"))
        db.add(ChannelLanguage(id=uuid.uuid4(), channel_id=channel_id, language="fr", channel_name="Chaine"))
        db.add(ChannelVoice(
            id=uuid.uuid4(), channel_id=channel_id, language="fr",
            provider="cartesia", voice_id="v1", tts_model="sonic-3",
        ))
        source_script = Script(
            id=uuid.uuid4(), content_id=content_id, language="en",
            voice_script="The source narration.", version=1, validated=True,
        )
        db.add(source_script)
        return db, channel, content, source_script

    def test_already_validated_existing_row_is_reused_without_regeneration(self):
        db, channel, content, source_script = self._base_fixtures()
        existing_fr = Script(
            id=uuid.uuid4(), content_id=content.id, language="fr",
            voice_script="Narration francaise existante.", version=1, validated=True,
        )
        db.add(existing_fr)

        with patch.object(
            scripts, "_generate_validated_translated_parent_script",
            side_effect=AssertionError("must not regenerate an already-validated row"),
        ):
            result = scripts.generate_multilingual_scripts(content, channel, db)

        self.assertEqual(len(result), 2)
        fr_result = next(s for s in result if s.language == "fr")
        self.assertIs(fr_result, existing_fr)
        self.assertTrue(fr_result.validated)
        self.assertEqual(fr_result.voice_script, "Narration francaise existante.")

    def test_unvalidated_existing_row_is_regenerated_not_blindly_marked_valid(self):
        """The core S-4 fix: a row that never reached validated=True must be
        regenerated, not force-marked valid and reused as-is."""
        db, channel, content, source_script = self._base_fixtures()
        stale_fr = Script(
            id=uuid.uuid4(), content_id=content.id, language="fr",
            voice_script="STALE unvalidated narration from a failed run.",
            version=1, validated=False,
        )
        db.add(stale_fr)

        def fake_generate(**kwargs):
            self.assertEqual(kwargs["target_language"], "fr")
            return _fake_adapted("Bonne narration francaise regeneree.")

        with patch.object(
            scripts, "_generate_validated_translated_parent_script", side_effect=fake_generate,
        ) as mock_generate:
            result = scripts.generate_multilingual_scripts(content, channel, db)

        mock_generate.assert_called_once()
        self.assertEqual(len(result), 2)
        fr_result = next(s for s in result if s.language == "fr")
        # Updated IN PLACE (same row/id) — never a second Script row for the
        # same content_id+language+version (would violate the unique constraint).
        self.assertIs(fr_result, stale_fr)
        self.assertTrue(fr_result.validated)
        self.assertEqual(fr_result.voice_script, "Bonne narration francaise regeneree.")
        # No second Script row was inserted for lang=fr.
        fr_rows_in_db = [
            s for s in db.tables[Script] if s.language == "fr" and s.content_id == content.id
        ]
        self.assertEqual(len(fr_rows_in_db), 1)

    def test_unvalidated_existing_row_failing_regeneration_stays_unvalidated_and_fails_content(self):
        db, channel, content, source_script = self._base_fixtures()
        stale_fr = Script(
            id=uuid.uuid4(), content_id=content.id, language="fr",
            voice_script="STALE unvalidated narration.", version=1, validated=False,
        )
        db.add(stale_fr)

        with patch.object(
            scripts, "_generate_validated_translated_parent_script",
            side_effect=lambda **kwargs: None,  # simulates exhausted retries
        ):
            result = scripts.generate_multilingual_scripts(content, channel, db)

        self.assertEqual(result, [])
        self.assertEqual(content.status, "FAILED")
        # The stale row must remain unvalidated — never force-marked valid.
        self.assertFalse(stale_fr.validated)

    def test_missing_language_still_generates_a_new_row_as_before(self):
        """No existing row at all for the language — unaffected by this fix,
        behaves exactly as before (new row created, validated=True)."""
        db, channel, content, source_script = self._base_fixtures()

        def fake_generate(**kwargs):
            return _fake_adapted("Toute nouvelle narration.")

        with patch.object(
            scripts, "_generate_validated_translated_parent_script", side_effect=fake_generate,
        ):
            result = scripts.generate_multilingual_scripts(content, channel, db)

        self.assertEqual(len(result), 2)
        fr_result = next(s for s in result if s.language == "fr")
        self.assertTrue(fr_result.validated)
        self.assertEqual(fr_result.voice_script, "Toute nouvelle narration.")
        self.assertIn(fr_result, db.added)


if __name__ == "__main__":
    unittest.main()
