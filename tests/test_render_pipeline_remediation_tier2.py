"""Runtime proof for render pipeline remediation roadmap Tier 2 (R5, R7-R10).

code_report/render_pipeline_remediation_roadmap.md Tier 2 — reliability and
operational hardening of app/agents/agent5_render/services/renderer.py:

  R5  explicit -crf on the chunk-concat libx264 fallback re-encode
  R7  pinned final encode settings (--crf/--pixel-format/--audio-bitrate)
  R8  chunked-render intermediate artifact cleanup (stale-orphan clear at
      start, success-only rmtree at the end)
  R9  ensure_bundle() advisory-lock serialization against the concurrent
      cold-cache race
  R10 beat-density concurrency cap (>40 sections halves concurrency, per
      render/per chunk)

R6 (subprocess.run() timeouts) was implemented and then REVERTED per explicit
operator instruction: a real long parent render (content 2704ad21, ~12.7 min
audio, rendered non-chunked) was killed at exactly 600s while still
legitimately in progress. TestNoSubprocessTimeoutEnforced below is the
regression guard for that reversal — it proves no `timeout=` kwarg reaches
`subprocess.run()` at any of the four call sites, so this class of premature
kill can't silently come back.

Per CLAUDE.md Sec19.1 and explicit operator instruction: no live API or
external render calls anywhere in this file. Every subprocess.run() call is
mocked; only the real renderer.py code paths (boundary math, concurrency
capping, chunk-directory lifecycle, the bundle lock) run unmodified.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.agents.agent5_render.services import renderer


def _make_sections(spans: list[tuple[int, int, str]]) -> list[dict]:
    sections = []
    for i, (start, end, transition) in enumerate(spans):
        sections.append({
            "order": i,
            "audio_start_ms": start,
            "audio_end_ms": end,
            "transition_to_next": transition,
            "media_url": f"cache/beat_{i}.jpg",
            "media_type": "image",
            "effect": "slow_zoom",
            "color_grade": "neutral",
        })
    return sections


# ── R10: beat-density concurrency cap ───────────────────────────────────────

class TestCappedConcurrency(unittest.TestCase):
    def test_below_threshold_unchanged(self):
        self.assertEqual(renderer._capped_concurrency(4, 40), 4)
        self.assertEqual(renderer._capped_concurrency(4, 1), 4)

    def test_above_threshold_halved(self):
        self.assertEqual(renderer._capped_concurrency(4, 41), 2)
        self.assertEqual(renderer._capped_concurrency(8, 100), 4)

    def test_floor_is_one(self):
        self.assertEqual(renderer._capped_concurrency(1, 41), 1)


class TestCountPropsSections(unittest.TestCase):
    def test_reads_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            props_path = str(Path(tmp) / "props.json")
            with open(props_path, "w", encoding="utf-8") as fh:
                json.dump({"sections": _make_sections([(0, 1000, "cut"), (1000, 2000, "cut")])}, fh)
            self.assertEqual(renderer._count_props_sections(props_path), 2)

    def test_missing_file_returns_zero(self):
        self.assertEqual(renderer._count_props_sections("/nonexistent/props.json"), 0)


class TestConcurrencyCapWiredIntoRenders(unittest.TestCase):
    """Full-chain proof (CLAUDE.md Sec19.4): the real render_main_video()/
    render_main_video_chunked() must actually pass the capped value to
    _run_remotion() — not just that the helper function computes correctly
    in isolation."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tier2_concurrency_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        p = patch.object(renderer.settings, "media_path", self.tmp_dir)
        p.start()
        self.addCleanup(p.stop)

    def test_render_main_video_caps_dense_props(self):
        dense_sections = _make_sections([(i * 1000, (i + 1) * 1000, "cut") for i in range(45)])
        props_path = str(Path(self.tmp_dir) / "dense_props.json")
        with open(props_path, "w", encoding="utf-8") as fh:
            json.dump({"sections": dense_sections}, fh)

        calls = []

        def _record(composition, output_path, props_path_arg, concurrency=4, **kw):
            calls.append(concurrency)
            Path(output_path).write_bytes(b"fake-mp4")
            return 0.1

        with patch.object(renderer, "_run_remotion", side_effect=_record):
            renderer.render_main_video("cid", "en", props_path, 45000, concurrency=8)

        self.assertEqual(calls, [8 // 2])

    def test_render_main_video_leaves_sparse_props_unchanged(self):
        sparse_sections = _make_sections([(0, 1000, "cut"), (1000, 2000, "cut")])
        props_path = str(Path(self.tmp_dir) / "sparse_props.json")
        with open(props_path, "w", encoding="utf-8") as fh:
            json.dump({"sections": sparse_sections}, fh)

        calls = []

        def _record(composition, output_path, props_path_arg, concurrency=4, **kw):
            calls.append(concurrency)
            Path(output_path).write_bytes(b"fake-mp4")
            return 0.1

        with patch.object(renderer, "_run_remotion", side_effect=_record):
            renderer.render_main_video("cid", "en", props_path, 2000, concurrency=8)

        self.assertEqual(calls, [8])

    def test_render_main_video_chunked_caps_per_chunk_not_whole_video(self):
        """A beat-dense chunk must be capped even when a sibling chunk in the
        SAME render is sparse — the cap must be per-chunk density, not a
        whole-render section count. _compute_chunk_boundaries() is stubbed
        here only to pin the exact two-chunk split (it has its own dedicated
        proof in tests/test_chunked_render_boundary_fix.py); everything else
        — _render_chunk's real per-chunk section counting and concurrency
        capping — runs unmodified.
        """
        dense_spans = [(i * 100, (i + 1) * 100, "cut") for i in range(45)]  # 45 beats, 0-4500ms
        sparse_spans = [(4500, 34500, "cut"), (34500, 64500, "cut"), (64500, 94500, "cut")]  # 3 beats
        spans = dense_spans + sparse_spans
        sections = _make_sections(spans)
        total_ms = spans[-1][1]

        props = {
            "content_id": "cid", "language": "en", "audio_file": "audio/cid/en.mp3",
            "duration_ms": total_ms, "sections": sections,
            "subtitles": {"style": "standard", "captions": []},
        }
        props_path = str(Path(self.tmp_dir) / "props.json")
        with open(props_path, "w", encoding="utf-8") as fh:
            json.dump(props, fh)
        audio_path = str(Path(self.tmp_dir) / "en.mp3")
        Path(audio_path).write_bytes(b"fake-audio")

        calls = []

        def _record(composition, output_path, props_path_arg, concurrency=4, **kw):
            calls.append(concurrency)
            Path(output_path).write_bytes(b"fake-mp4")
            return 0.1

        with patch.object(renderer, "_compute_chunk_boundaries", return_value=[(0, 4500), (4500, 94500)]), \
             patch.object(renderer, "_slice_audio_for_chunk", return_value=True), \
             patch.object(renderer, "_run_remotion", side_effect=_record), \
             patch.object(renderer, "_concatenate_chunks") as mock_concat:
            renderer.render_main_video_chunked(
                content_id="cid", language="en", props_path=props_path,
                duration_ms=total_ms, audio_file_path=audio_path, concurrency=8,
            )

        mock_concat.assert_called_once()
        self.assertEqual(calls, [4, 8], "dense chunk (45 beats) halved, sparse chunk (3 beats) unchanged")


# ── R7: pinned encode flags on _run_remotion ────────────────────────────────

class TestRunRemotionEncodeFlags(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tier2_run_remotion_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.media_dir = Path(self.tmp_dir) / "media"
        self.media_dir.mkdir()
        for name, value in (
            ("remotion_path", self.tmp_dir),
            ("media_path", str(self.media_dir)),
            ("node_bin", "node"),
        ):
            p = patch.object(renderer.settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _output_and_props(self):
        output_path = Path(self.tmp_dir) / "out.mp4"
        props_path = str(Path(self.tmp_dir) / "props.json")
        Path(props_path).write_text("{}")
        return output_path, props_path

    def test_cmd_includes_pinned_encode_flags(self):
        output_path, props_path = self._output_and_props()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            renderer._run_remotion("MainVideo", output_path, props_path, concurrency=4)

        cmd = mock_run.call_args.args[0]
        self.assertIn("--crf", cmd)
        self.assertEqual(cmd[cmd.index("--crf") + 1], str(renderer._TARGET_CRF))
        self.assertIn("--pixel-format", cmd)
        self.assertEqual(cmd[cmd.index("--pixel-format") + 1], "yuv420p")
        self.assertIn("--audio-bitrate", cmd)
        self.assertEqual(cmd[cmd.index("--audio-bitrate") + 1], "320k")


# ── R5: explicit CRF on the concat fallback ─────────────────────────────────

class TestConcatFallbackCRF(unittest.TestCase):
    def test_fallback_includes_explicit_crf(self):
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "copy" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="mismatched codec parameters")
            return MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.mp4")
            with patch("subprocess.run", side_effect=_fake_run):
                renderer._concatenate_chunks(["/fake/chunk0.mp4", "/fake/chunk1.mp4"], out)

        self.assertEqual(len(calls), 2, "stream-copy attempt then the re-encode fallback")
        fallback_cmd = calls[1]
        self.assertIn("-crf", fallback_cmd)
        self.assertEqual(fallback_cmd[fallback_cmd.index("-crf") + 1], str(renderer._TARGET_CRF))


# ── R6 reversion guard: no subprocess.run() call may carry a timeout ───────
#
# A real long parent render (content 2704ad21, ~12.7 min audio) was killed at
# exactly 600s by the timeout R6 originally added, while still legitimately
# in progress — long videos can genuinely take longer than any fixed ceiling.
# Reverted per explicit operator instruction ("we don't need any timeout
# here it must take whatever time it needs"). These tests are the permanent
# regression guard against that specific premature-kill behavior returning.

class TestNoSubprocessTimeoutEnforced(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tier2_no_timeout_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_run_remotion_passes_no_timeout(self):
        media_dir = Path(self.tmp_dir) / "media"
        media_dir.mkdir()
        output_path = Path(self.tmp_dir) / "out.mp4"
        props_path = str(Path(self.tmp_dir) / "props.json")
        Path(props_path).write_text("{}")
        for name, value in (
            ("remotion_path", self.tmp_dir),
            ("media_path", str(media_dir)),
            ("node_bin", "node"),
        ):
            patcher = patch.object(renderer.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            renderer._run_remotion("MainVideo", output_path, props_path, concurrency=4)
        self.assertNotIn("timeout", mock_run.call_args.kwargs)

    def test_slice_audio_passes_no_timeout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            renderer._slice_audio_for_chunk("/fake/audio.mp3", 0.0, 10.0, "/fake/out.mp3")
        self.assertNotIn("timeout", mock_run.call_args.kwargs)

    def test_concatenate_chunks_passes_no_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.mp4")
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                renderer._concatenate_chunks(["/fake/chunk0.mp4"], out)
        self.assertNotIn("timeout", mock_run.call_args.kwargs)

    def test_bundle_build_passes_no_timeout(self):
        for name, value in (
            ("remotion_pre_bundle", True),
            ("remotion_path", self.tmp_dir),
            ("node_bin", "node"),
        ):
            patcher = patch.object(renderer.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        def _fake_bundle_subprocess(cmd, cwd=None, **kwargs):
            build_dir = Path(cwd) / "build"
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "index.js").write_text("bundled")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_fake_bundle_subprocess) as mock_run:
            renderer.ensure_bundle()
        self.assertNotIn("timeout", mock_run.call_args.kwargs)

    def test_a_genuinely_slow_render_is_not_killed(self):
        """Direct proof of the real production scenario: a subprocess.run()
        call that takes far longer than the old 600s ceiling must still
        succeed, not raise."""
        media_dir = Path(self.tmp_dir) / "media"
        media_dir.mkdir()
        output_path = Path(self.tmp_dir) / "out.mp4"
        props_path = str(Path(self.tmp_dir) / "props.json")
        Path(props_path).write_text("{}")
        for name, value in (
            ("remotion_path", self.tmp_dir),
            ("media_path", str(media_dir)),
            ("node_bin", "node"),
        ):
            patcher = patch.object(renderer.settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        def _slow_but_successful(*args, **kwargs):
            # A real subprocess.run(timeout=600) would have raised
            # TimeoutExpired for any call exceeding 600s; asserting no
            # `timeout` kwarg is present is what actually guarantees this
            # never happens for a real long-running Remotion process.
            self.assertNotIn("timeout", kwargs)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_slow_but_successful):
            elapsed = renderer._run_remotion("MainVideo", output_path, props_path, concurrency=4)
        self.assertIsInstance(elapsed, float)


# ── R8: chunk artifact lifecycle ────────────────────────────────────────────

class TestChunkArtifactCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tier2_chunk_cleanup_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        p = patch.object(renderer.settings, "media_path", self.tmp_dir)
        p.start()
        self.addCleanup(p.stop)
        cp = patch.object(renderer.settings, "chunk_duration_sec", 20)
        cp.start()
        self.addCleanup(cp.stop)

    def _build_props(self, spans):
        sections = _make_sections(spans)
        total_ms = spans[-1][1]
        props = {
            "content_id": "cid", "language": "en", "audio_file": "audio/cid/en.mp3",
            "duration_ms": total_ms, "sections": sections,
            "subtitles": {"style": "standard", "captions": []},
        }
        props_path = str(Path(self.tmp_dir) / "props.json")
        with open(props_path, "w", encoding="utf-8") as fh:
            json.dump(props, fh)
        return props_path, total_ms

    def _fake_run_remotion(self, composition, output_path, props_path_arg, concurrency=4, **kw):
        Path(output_path).write_bytes(b"fake-mp4")
        return 0.1

    def test_chunk_dir_removed_after_successful_render(self):
        spans = [(0, 8000, "cut"), (8000, 25000, "crossfade"), (25000, 45000, "cut")]
        props_path, total_ms = self._build_props(spans)
        audio_path = str(Path(self.tmp_dir) / "en.mp3")
        Path(audio_path).write_bytes(b"fake-audio")

        chunk_dir = Path(self.tmp_dir) / "video" / "cid" / "chunks" / "en"

        def _fake_concat(chunk_paths, output_path):
            Path(output_path).write_bytes(b"fake-final-mp4")

        with patch.object(renderer, "_slice_audio_for_chunk", return_value=True), \
             patch.object(renderer, "_run_remotion", side_effect=self._fake_run_remotion), \
             patch.object(renderer, "_concatenate_chunks", side_effect=_fake_concat):
            renderer.render_main_video_chunked(
                content_id="cid", language="en", props_path=props_path,
                duration_ms=total_ms, audio_file_path=audio_path,
            )

        self.assertFalse(chunk_dir.exists(), "chunk artifacts must be removed after a successful concat")

    def test_stale_orphan_cleared_at_start_but_dir_preserved_on_concat_failure(self):
        spans = [(0, 8000, "cut"), (8000, 25000, "crossfade"), (25000, 45000, "cut")]
        props_path, total_ms = self._build_props(spans)
        audio_path = str(Path(self.tmp_dir) / "en.mp3")
        Path(audio_path).write_bytes(b"fake-audio")

        chunk_dir = Path(self.tmp_dir) / "video" / "cid" / "chunks" / "en"
        chunk_dir.mkdir(parents=True)
        stale_marker = chunk_dir / "chunk_099.mp4"
        stale_marker.write_bytes(b"stale-leftover-from-a-prior-bigger-run")

        with patch.object(renderer, "_slice_audio_for_chunk", return_value=True), \
             patch.object(renderer, "_run_remotion", side_effect=self._fake_run_remotion), \
             patch.object(
                 renderer, "_concatenate_chunks",
                 side_effect=renderer.RemotionRenderError("simulated concat failure"),
             ):
            with self.assertRaises(renderer.RemotionRenderError):
                renderer.render_main_video_chunked(
                    content_id="cid", language="en", props_path=props_path,
                    duration_ms=total_ms, audio_file_path=audio_path,
                )

        self.assertTrue(chunk_dir.exists(), "chunk dir must survive a concat failure for post-mortem inspection")
        self.assertFalse(
            stale_marker.exists(),
            "the pre-existing stale file must be cleared at the START of the run, "
            "before this run's own artifacts are written",
        )
        real_chunk_files = list(chunk_dir.glob("chunk_*.mp4"))
        self.assertTrue(
            any(f.name != "chunk_099.mp4" for f in real_chunk_files),
            "this run's own real chunk mp4s must still be present after the start-of-run clear",
        )


# ── R9: ensure_bundle() concurrent-render lock ──────────────────────────────

class TestEnsureBundleLock(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tier2_bundle_lock_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        for name, value in (
            ("remotion_pre_bundle", True),
            ("remotion_path", self.tmp_dir),
            ("node_bin", "node"),
        ):
            p = patch.object(renderer.settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_concurrent_calls_serialize_and_bundle_exactly_once(self):
        build_call_count = {"n": 0}
        lock = threading.Lock()

        def _fake_bundle_subprocess(cmd, cwd=None, **kwargs):
            with lock:
                build_call_count["n"] += 1
            # Force real overlap between the two threads' lock-acquisition
            # attempts — the first thread should be mid-"build" while the
            # second is blocked waiting on the flock.
            time.sleep(0.2)
            build_dir = Path(cwd) / "build"
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "index.js").write_text("bundled")
            return MagicMock(returncode=0, stdout="", stderr="")

        results: list[str | None] = [None, None]

        def _call(idx: int) -> None:
            results[idx] = renderer.ensure_bundle()

        with patch("subprocess.run", side_effect=_fake_bundle_subprocess):
            t1 = threading.Thread(target=_call, args=(0,))
            t2 = threading.Thread(target=_call, args=(1,))
            t1.start()
            time.sleep(0.05)  # ensure t1 acquires the lock first
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

        self.assertEqual(
            build_call_count["n"], 1,
            "the second concurrent caller must reuse the first's completed "
            "bundle (via the double-checked lock) instead of racing into "
            "its own `npx remotion bundle` call",
        )
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertEqual(results[0], results[1], "both callers must resolve to the same bundle directory")


if __name__ == "__main__":
    unittest.main()
