"""Runtime proof for shared source-script persistence versioning.

No external APIs are called. The tests exercise the real script_workflow
persistence helper and duration estimator against a tiny in-memory session.
"""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.agents.agent2_discovery.services import script_workflow
from app.models import AudioFile, Script


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)
        self._limit = None

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        self.rows = sorted(
            self.rows,
            key=lambda row: getattr(row, "version", getattr(row, "id", 0)) or 0,
            reverse=True,
        )
        return self

    def limit(self, value):
        self._limit = int(value)
        return self

    def all(self):
        return self.rows[: self._limit] if self._limit is not None else self.rows

    def first(self):
        rows = self.all()
        return rows[0] if rows else None


class FakeDb:
    def __init__(self, scripts=None, audio_files=None):
        self.scripts = list(scripts or [])
        self.audio_files = list(audio_files or [])
        self.added = []
        self.commits = 0

    def query(self, model):
        if model is Script:
            return FakeQuery(self.scripts)
        if model is AudioFile:
            return FakeQuery(self.audio_files)
        return FakeQuery([])

    def add(self, row):
        self.added.append(row)
        if isinstance(row, Script):
            self.scripts.append(row)

    def commit(self):
        self.commits += 1


class SourceScriptPersistenceTest(unittest.TestCase):
    def test_persist_source_script_uses_next_version(self) -> None:
        content_id = uuid.uuid4()
        content = SimpleNamespace(
            id=content_id,
            title="Old title",
            source_language="en",
        )
        existing = Script(
            content_id=content_id,
            language="en",
            voice_script="old source script",
            version=2,
            validated=True,
            estimated_duration_sec=1.0,
        )
        db = FakeDb(scripts=[existing])

        voice_script = script_workflow._persist_source_script(
            content,
            {"title": "New title", "voice_script": "one two three four five"},
            db,
        )

        self.assertEqual(voice_script, "one two three four five")
        self.assertEqual(content.title, "New title")
        self.assertEqual(len(db.added), 1)
        saved = db.added[0]
        self.assertEqual(saved.version, 3)
        self.assertTrue(saved.validated)
        self.assertGreater(saved.estimated_duration_sec, 0)
        self.assertEqual(db.commits, 1)

    def test_persist_source_script_starts_at_version_one_when_missing(self) -> None:
        content = SimpleNamespace(id=uuid.uuid4(), title="Title", source_language="en")
        db = FakeDb()

        script_workflow._persist_source_script(
            content,
            {"voice_script": "one two three"},
            db,
        )

        self.assertEqual(db.added[0].version, 1)

    def test_harness_delegates_source_persistence_to_workflow_helper(self) -> None:
        # Since the roadmap 4.7 close-out, the harness delegates the WHOLE
        # blueprint→sections→gate→persist sequence to the shared
        # generate_parent_source_script() (which itself calls
        # _persist_source_script + _merge_visual_intent_history) — the harness
        # no longer references either private helper directly.
        src = Path("test_pipeline/test_full_pipeline.py").read_text()

        self.assertIn("generate_parent_source_script", src)
        self.assertNotIn("_persist_source_script", src)
        self.assertNotIn("next_version = 1", src)
        self.assertNotIn("version=next_version", src)


if __name__ == "__main__":
    unittest.main()
