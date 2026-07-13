"""Runtime proof that roadmap Phase B2's caption punctuation restoration is
actually wired into Agent 5's render path, not just implemented in isolation
(CLAUDE.md §19.4 — "field set in function A, read in function C").

Chain under test:
  Content.story_blueprint (parent) / parent's story_blueprint (child)
    -> _resolve_proper_nouns_for_content()
    -> _process_language()'s build_standard_subtitles()/build_karaoke_subtitles() calls
    -> voice_script=script.voice_script, proper_nouns=<the resolved list>

Only the paid external boundaries (fal_client/elevenlabs/openai) are
stubbed, matching the existing convention in
test_stale_visuals_audio_fingerprint_and_props_rebuild.py — the functions
under test here run for real.
"""

from __future__ import annotations

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

from app.agents.agent5_render.services import video as video_module
from app.models import Content


class ResolveProperNounsForContentTest(unittest.TestCase):
    def test_parent_content_uses_its_own_blueprint(self) -> None:
        blueprint = {
            "hook": "Theodora rose from nothing. Theodora ruled beside Justinian.",
            "final_payoff": "Justinian never forgot Theodora's warning.",
        }
        content = SimpleNamespace(
            story_blueprint=blueprint, is_short_episode=False, parent_content_id=None,
        )
        db = MagicMock()
        result = video_module._resolve_proper_nouns_for_content(content, db)
        self.assertIn("Theodora", result)
        self.assertIn("Justinian", result)
        db.get.assert_not_called()

    def test_child_with_no_own_blueprint_reads_parents(self) -> None:
        parent_id = uuid.uuid4()
        parent_blueprint = {
            "hook": "Belisarius marched north. Belisarius never retreated.",
        }
        content = SimpleNamespace(
            story_blueprint=None, is_short_episode=True, parent_content_id=parent_id,
        )
        db = MagicMock()
        db.get.return_value = SimpleNamespace(story_blueprint=parent_blueprint)

        result = video_module._resolve_proper_nouns_for_content(content, db)

        db.get.assert_called_once_with(Content, parent_id)
        self.assertIn("Belisarius", result)

    def test_child_with_missing_parent_row_returns_empty(self) -> None:
        content = SimpleNamespace(
            story_blueprint=None, is_short_episode=True, parent_content_id=uuid.uuid4(),
        )
        db = MagicMock()
        db.get.return_value = None
        self.assertEqual(video_module._resolve_proper_nouns_for_content(content, db), [])

    def test_parent_content_with_no_blueprint_returns_empty(self) -> None:
        content = SimpleNamespace(
            story_blueprint=None, is_short_episode=False, parent_content_id=None,
        )
        db = MagicMock()
        self.assertEqual(video_module._resolve_proper_nouns_for_content(content, db), [])
        db.get.assert_not_called()

    def test_child_with_its_own_blueprint_does_not_look_up_parent(self) -> None:
        # Defensive case: if a child ever does carry its own blueprint,
        # prefer it and never touch the parent lookup.
        own_blueprint = {"hook": "Marcus arrived. Marcus left again."}
        content = SimpleNamespace(
            story_blueprint=own_blueprint, is_short_episode=True, parent_content_id=uuid.uuid4(),
        )
        db = MagicMock()
        result = video_module._resolve_proper_nouns_for_content(content, db)
        self.assertIn("Marcus", result)
        db.get.assert_not_called()


class ProcessLanguagePassesScriptAndEntitiesToSubtitlesTest(unittest.TestCase):
    """Wiring proof, same MagicMock-db + patch.object pattern as
    test_stale_visuals_audio_fingerprint_and_props_rebuild.py's
    TestProcessLanguageRebuildsWhenPropsAreStale."""

    def test_voice_script_and_proper_nouns_reach_both_subtitle_builders(self) -> None:
        content_id = uuid.uuid4()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None  # not yet rendered

        script = SimpleNamespace(voice_script="Belisarius marched north.")
        audio = SimpleNamespace(duration_ms=9000, whisper_transcript=[], file_path="audio/en.mp3")
        channel = SimpleNamespace(id=uuid.uuid4())
        proper_nouns = ["Belisarius"]

        with TemporaryDirectory() as tmp:
            standard_calls: list[dict] = []
            karaoke_calls: list[dict] = []

            def fake_standard(*args, **kwargs):
                standard_calls.append(kwargs)
                return []

            def fake_karaoke(*args, **kwargs):
                karaoke_calls.append(kwargs)
                return []

            with (
                patch.object(video_module.settings, "media_path", tmp),
                patch.object(video_module, "build_standard_subtitles", side_effect=fake_standard),
                patch.object(video_module, "build_karaoke_subtitles", side_effect=fake_karaoke),
            ):
                # Empty beats trips the no_beats technical blocker right
                # after subtitle building — enough to observe the calls
                # without mocking the entire render/props/verify pipeline.
                video_module._process_language(
                    content_id=content_id, language="en", script=script,
                    audio=audio, beats=[], channel=channel,
                    karaoke_color="#fff", db=db, proper_nouns=proper_nouns,
                )

        self.assertEqual(len(standard_calls), 1)
        self.assertEqual(len(karaoke_calls), 1)
        self.assertEqual(standard_calls[0]["voice_script"], "Belisarius marched north.")
        self.assertEqual(standard_calls[0]["proper_nouns"], ["Belisarius"])
        self.assertEqual(karaoke_calls[0]["voice_script"], "Belisarius marched north.")
        self.assertEqual(karaoke_calls[0]["proper_nouns"], ["Belisarius"])

    def test_missing_proper_nouns_defaults_to_none_not_a_crash(self) -> None:
        content_id = uuid.uuid4()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        script = SimpleNamespace(voice_script="Plain narration.")
        audio = SimpleNamespace(duration_ms=9000, whisper_transcript=[], file_path="audio/en.mp3")
        channel = SimpleNamespace(id=uuid.uuid4())

        with TemporaryDirectory() as tmp:
            standard_calls: list[dict] = []

            def fake_standard(*args, **kwargs):
                standard_calls.append(kwargs)
                return []

            with (
                patch.object(video_module.settings, "media_path", tmp),
                patch.object(video_module, "build_standard_subtitles", side_effect=fake_standard),
                patch.object(video_module, "build_karaoke_subtitles", return_value=[]),
            ):
                video_module._process_language(
                    content_id=content_id, language="en", script=script,
                    audio=audio, beats=[], channel=channel,
                    karaoke_color="#fff", db=db,
                )

        self.assertIsNone(standard_calls[0]["proper_nouns"])


if __name__ == "__main__":
    unittest.main()
