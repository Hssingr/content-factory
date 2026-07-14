"""Runtime proof for the chunked-render boundary fix (live-canary, content
2704ad21-853c-47ff-b496-c92d147b9339).

Root cause: render_main_video_chunked() used to split VideoSection beats at
a fixed chunk_idx * chunk_duration_sec timestamp regardless of whether a
beat's own span crossed that exact millisecond. Real production data showed
this happened at every single internal chunk boundary of a 9-chunk render
(8/8): the straddling beat was independently clipped into BOTH adjacent
chunks — full playback at the tail of one chunk, then an immediate replay
from its own progress=0 for a short fragment (as little as 240ms) at the
head of the next — and because each chunk is rendered as its own,
completely independent Remotion composition, MainVideo.tsx's own idx===0
first-section case always treated that fragment's arrival as having no
incoming transition, discarding whatever crossfade/whip_pan/match_cut was
actually authored there.

Only the paid/external boundaries (ffmpeg audio slicing, the Remotion CLI
subprocess, ffmpeg concat) are stubbed. _compute_chunk_boundaries() and the
real section-filtering / leading_transition logic inside
render_main_video_chunked() run unmodified, and this test reads back the
real chunk_XXX.json prop files render_main_video_chunked() writes to disk.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.agent5_render.services import renderer


def _make_sections(spans: list[tuple[int, int, str]]) -> list[dict]:
    """Build a contiguous, ordered VideoSection-shaped list from
    (start_ms, end_ms, transition_to_next) tuples."""
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


class TestComputeChunkBoundaries(unittest.TestCase):
    def test_short_duration_returns_single_chunk(self):
        sections = _make_sections([(0, 5000, "cut")])
        boundaries = renderer._compute_chunk_boundaries(sections, 5000, chunk_sec=90)
        self.assertEqual(boundaries, [(0, 5000)])

    def test_no_section_ever_straddles_a_boundary(self):
        """The exact defect class: every returned internal boundary must
        equal some section's real audio_end_ms, so no section's own
        [start, end) span can ever cross it."""
        spans = []
        t = 0
        durations = [1000, 2000, 9000, 3000, 4500, 1200, 8800, 2200, 3300,
                     6100, 1000, 7000, 2900, 3400, 5200, 1800, 9000, 2600]
        for d in durations:
            spans.append((t, t + d, "cut"))
            t += d
        total_ms = t
        sections = _make_sections(spans)

        boundaries = renderer._compute_chunk_boundaries(sections, total_ms, chunk_sec=20)

        self.assertGreater(len(boundaries), 1, "test setup should produce multiple chunks")
        # Boundaries must exactly cover [0, total_ms) with no gap/overlap.
        self.assertEqual(boundaries[0][0], 0)
        self.assertEqual(boundaries[-1][1], total_ms)
        for (a_start, a_end), (b_start, b_end) in zip(boundaries, boundaries[1:]):
            self.assertEqual(a_end, b_start, "chunk boundaries must be contiguous")

        section_edges = {s["audio_end_ms"] for s in sections}
        internal_boundaries = [end for (_, end) in boundaries[:-1]]
        for edge in internal_boundaries:
            self.assertIn(
                edge, section_edges,
                "every internal chunk boundary must land exactly on a real "
                "section edge — this is what prevents a beat from being "
                "split (and duplicated) across two chunks",
            )

        # No section may straddle any internal boundary.
        for s in sections:
            for edge in internal_boundaries:
                straddles = s["audio_start_ms"] < edge < s["audio_end_ms"]
                self.assertFalse(
                    straddles,
                    f"section {s['order']} [{s['audio_start_ms']},{s['audio_end_ms']}) "
                    f"straddles boundary {edge} — reproduces the exact production defect",
                )

    def test_falls_back_to_single_chunk_when_no_candidates(self):
        # One giant section spanning the whole duration — no internal edge exists.
        sections = _make_sections([(0, 500_000, "cut")])
        boundaries = renderer._compute_chunk_boundaries(sections, 500_000, chunk_sec=90)
        self.assertEqual(boundaries, [(0, 500_000)])


class TestRenderMainVideoChunkedBoundaryHandling(unittest.TestCase):
    """Full-chain proof through the real render_main_video_chunked() —
    only ffmpeg/Remotion subprocess calls are stubbed."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="chunked_render_test_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._settings_patch = patch.object(renderer.settings, "media_path", self.tmp_dir)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)
        chunk_sec_patch = patch.object(renderer.settings, "chunk_duration_sec", 20)
        chunk_sec_patch.start()
        self.addCleanup(chunk_sec_patch.stop)

    def _build_props(self, spans: list[tuple[int, int, str]]) -> tuple[str, int]:
        sections = _make_sections(spans)
        total_ms = spans[-1][1]
        props = {
            "content_id": "test-content",
            "language": "en",
            "audio_file": "audio/test-content/en.mp3",
            "duration_ms": total_ms,
            "sections": sections,
            "subtitles": {"style": "standard", "captions": []},
        }
        props_path = str(Path(self.tmp_dir) / "props.json")
        with open(props_path, "w", encoding="utf-8") as fh:
            json.dump(props, fh)
        return props_path, total_ms

    def test_no_duplicate_beats_and_transitions_preserved_across_chunks(self):
        spans = [
            (0, 2200, "cut"),
            (2200, 6200, "crossfade"),
            (6200, 15200, "cut"),          # long beat straddles the naive 20s mark
            (15200, 19100, "whip_pan"),    # real transition into the beat that would
            (19100, 23400, "cut"),         # otherwise land right at a 20s chunk seam
            (23400, 31000, "match_cut"),
            (31000, 34000, "cut"),
            (34000, 42500, "crossfade"),
            (42500, 45000, "cut"),
        ]
        props_path, total_ms = self._build_props(spans)
        audio_path = str(Path(self.tmp_dir) / "en.mp3")
        Path(audio_path).write_bytes(b"fake-audio")

        written_chunk_props: list[dict] = []

        def _fake_run_remotion(composition, output_path, props_path_arg, concurrency=4,
                                chrome_flags="", bundle_dir=None):
            with open(props_path_arg, encoding="utf-8") as fh:
                written_chunk_props.append(json.load(fh))
            Path(output_path).write_bytes(b"fake-mp4")
            return 0.1

        with patch.object(renderer, "_slice_audio_for_chunk", return_value=True), \
             patch.object(renderer, "_run_remotion", side_effect=_fake_run_remotion), \
             patch.object(renderer, "_concatenate_chunks") as mock_concat:
            result = renderer.render_main_video_chunked(
                content_id="test-content",
                language="en",
                props_path=props_path,
                duration_ms=total_ms,
                audio_file_path=audio_path,
            )

        self.assertTrue(result["file_path"])
        self.assertGreater(len(written_chunk_props), 1, "test setup should produce multiple chunks")
        mock_concat.assert_called_once()

        # Reconstruct the global timeline from every chunk's re-offset sections
        # and confirm every original beat appears EXACTLY once, at its exact
        # original global span — the direct fix proof for the duplication bug.
        seen_orders: dict[int, tuple[int, int]] = {}
        chunk_start_cursor = 0
        for chunk_props in written_chunk_props:
            for sec in chunk_props["sections"]:
                global_start = sec["audio_start_ms"] + chunk_start_cursor
                global_end = sec["audio_end_ms"] + chunk_start_cursor
                self.assertNotIn(
                    sec["order"], seen_orders,
                    f"beat {sec['order']} appeared in more than one chunk — "
                    f"reproduces the exact production duplication defect",
                )
                seen_orders[sec["order"]] = (global_start, global_end)
            chunk_start_cursor += chunk_props["duration_ms"]

        expected = {i: (start, end) for i, (start, end, _t) in enumerate(spans)}
        self.assertEqual(seen_orders, expected)

        # leading_transition on every chunk after the first must equal the
        # real transition_to_next of the section that truly precedes it —
        # the direct fix proof for the lost-transition bug.
        for i in range(1, len(written_chunk_props)):
            first_sec = written_chunk_props[i]["sections"][0]
            preceding_order = first_sec["order"] - 1
            expected_transition = spans[preceding_order][2]
            self.assertEqual(
                written_chunk_props[i]["leading_transition"], expected_transition,
                f"chunk {i}'s leading_transition should carry the real "
                f"transition_to_next of beat {preceding_order}, not be discarded",
            )

        self.assertIsNone(written_chunk_props[0].get("leading_transition"))


if __name__ == "__main__":
    unittest.main()
