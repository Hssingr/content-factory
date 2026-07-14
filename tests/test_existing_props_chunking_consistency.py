"""Runtime proof: the "props already on disk" resume path must make the same
chunked-vs-single render decision the fresh-build path makes.

Real incident: content 2704ad21-853c-47ff-b496-c92d147b9339's first render
attempt went through the fresh-build path (_run_renders()), correctly
chunking a ~12.7 minute parent video. The process was interrupted before all
languages finished. On resume, _process_language() found en_main.json
already on disk and not stale, so it took the
_render_from_existing_props() shortcut instead — which used to call
render_main_video() directly and unconditionally, never checking
duration_ms against settings.chunk_duration_sec, so the same video rendered
as one giant unchunked Chromium composition on resume.

Fix: _render_from_existing_props() now delegates its main-video branch to
_run_renders() (the same function the fresh-build path already uses)
instead of duplicating render+verify+persist logic that had drifted out of
sync with the chunking decision.

Per CLAUDE.md Sec19.1 and explicit operator instruction: no live API/render
calls. Only the paid Remotion/ffmpeg boundary (render_main_video /
render_main_video_chunked themselves) is mocked; _render_from_existing_props()
and _run_renders() run unmodified.
"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agents.agent5_render.services import video as video_module


class TestExistingPropsPathMatchesFreshBuildChunkingDecision(unittest.TestCase):
    def _run(self, *, duration_ms: int, chunk_duration_sec: int, chunked_render_enabled: bool = True):
        content_id = uuid.uuid4()
        db = MagicMock()
        # _render_exists() -> False (not already rendered) on every query.
        db.query.return_value.filter.return_value.first.return_value = None

        audio = SimpleNamespace(duration_ms=duration_ms, file_path="audio/en.mp3")

        chunked_calls: list[int] = []
        single_calls: list[int] = []

        def _fake_chunked(**kwargs):
            chunked_calls.append(kwargs["duration_ms"])
            return {"file_path": "/fake/en_main.mp4", "duration_seconds": duration_ms / 1000,
                    "render_time_seconds": 1.0}

        def _fake_single(**kwargs):
            single_calls.append(kwargs["duration_ms"])
            return {"file_path": "/fake/en_main.mp4", "duration_seconds": duration_ms / 1000,
                    "render_time_seconds": 1.0}

        with TemporaryDirectory() as tmp:
            props_dir = Path(tmp) / "remotion_props"
            props_dir.mkdir(parents=True, exist_ok=True)
            props_path = props_dir / f"{content_id}_en_main.json"
            props_path.write_text(json.dumps({"duration_ms": duration_ms, "sections": []}))

            with (
                patch.object(video_module.settings, "chunk_duration_sec", chunk_duration_sec),
                patch.object(video_module.settings, "chunked_render_enabled", chunked_render_enabled),
                patch.object(video_module.settings, "verify_renders", False),
                patch.object(video_module, "ensure_bundle", return_value=None),
                patch.object(video_module, "render_main_video_chunked", side_effect=_fake_chunked),
                patch.object(video_module, "render_main_video", side_effect=_fake_single),
            ):
                result = video_module._render_from_existing_props(
                    content_id=content_id, language="en", audio=audio,
                    cid_str=str(content_id), props_dir=props_dir, db=db,
                    is_short_episode=False,
                )

        self.assertTrue(result)
        return chunked_calls, single_calls

    def test_long_video_uses_chunked_render_on_resume(self):
        """The exact regression: a ~12.7 min video (759_864 ms, the real
        audited duration) resuming through the existing-props path must
        still chunk, matching what the original fresh render did."""
        chunked_calls, single_calls = self._run(duration_ms=759_864, chunk_duration_sec=90)
        self.assertEqual(len(chunked_calls), 1, "long video must render via render_main_video_chunked")
        self.assertEqual(len(single_calls), 0, "must NOT fall back to the single non-chunked render")

    def test_short_video_still_uses_single_render_on_resume(self):
        """A video under the chunk threshold must still render single-pass
        (chunking a short video would be pointless overhead) — proves the
        fix didn't just hardcode chunked=True."""
        chunked_calls, single_calls = self._run(duration_ms=30_000, chunk_duration_sec=90)
        self.assertEqual(len(chunked_calls), 0)
        self.assertEqual(len(single_calls), 1)

    def test_chunking_disabled_falls_back_to_single_even_for_long_video(self):
        """settings.chunked_render_enabled=False must still be honored on
        the resume path, exactly like the fresh-build path already does."""
        chunked_calls, single_calls = self._run(
            duration_ms=759_864, chunk_duration_sec=90, chunked_render_enabled=False,
        )
        self.assertEqual(len(chunked_calls), 0)
        self.assertEqual(len(single_calls), 1)

    def test_persists_video_render_row(self):
        """_run_renders() must still persist the VideoRender row exactly as
        before — the delegation must not silently drop persistence."""
        content_id = uuid.uuid4()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        audio = SimpleNamespace(duration_ms=759_864, file_path="audio/en.mp3")

        with TemporaryDirectory() as tmp:
            props_dir = Path(tmp) / "remotion_props"
            props_dir.mkdir(parents=True, exist_ok=True)
            (props_dir / f"{content_id}_en_main.json").write_text(
                json.dumps({"duration_ms": 759_864, "sections": []})
            )

            with (
                patch.object(video_module.settings, "chunk_duration_sec", 90),
                patch.object(video_module.settings, "chunked_render_enabled", True),
                patch.object(video_module.settings, "verify_renders", False),
                patch.object(video_module, "ensure_bundle", return_value=None),
                patch.object(
                    video_module, "render_main_video_chunked",
                    return_value={"file_path": "/fake/en_main.mp4", "duration_seconds": 759.864,
                                  "render_time_seconds": 1.0},
                ),
            ):
                video_module._render_from_existing_props(
                    content_id=content_id, language="en", audio=audio,
                    cid_str=str(content_id), props_dir=props_dir, db=db,
                    is_short_episode=False,
                )

        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        self.assertIsInstance(added, video_module.VideoRender)
        self.assertEqual(added.content_id, content_id)
        self.assertEqual(added.language, "en")
        self.assertEqual(added.format, "main")
        db.commit.assert_called_once()

    def test_already_rendered_is_skipped_without_calling_either_renderer(self):
        """Unchanged regression guard: the pre-existing _render_exists()
        short-circuit must still work after the refactor."""
        content_id = uuid.uuid4()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = object()  # row exists
        audio = SimpleNamespace(duration_ms=759_864, file_path="audio/en.mp3")

        with TemporaryDirectory() as tmp:
            props_dir = Path(tmp) / "remotion_props"
            props_dir.mkdir(parents=True, exist_ok=True)
            (props_dir / f"{content_id}_en_main.json").write_text(
                json.dumps({"duration_ms": 759_864, "sections": []})
            )

            with (
                patch.object(video_module, "render_main_video_chunked") as mock_chunked,
                patch.object(video_module, "render_main_video") as mock_single,
            ):
                result = video_module._render_from_existing_props(
                    content_id=content_id, language="en", audio=audio,
                    cid_str=str(content_id), props_dir=props_dir, db=db,
                    is_short_episode=False,
                )

        self.assertTrue(result)
        mock_chunked.assert_not_called()
        mock_single.assert_not_called()
        db.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
