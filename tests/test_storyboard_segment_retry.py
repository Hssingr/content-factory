"""Runtime proof for segment-level storyboard retry.

The paid Claude boundary is stubbed. The test exercises the real
``_run_storyboard_validation`` -> ``split_into_beats`` retry path, including
batch provenance, unaffected-segment reuse, merge, timestamp remap, and
post-retry validation.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.services.visual_orchestrator import (
    _run_storyboard_validation,
    _storyboard_retry_constraints_by_batch,
)


def section_beat(order: int, label: str, *, prompt: str, start_hint: str, end_hint: str) -> dict:
    return {
        "beat_order": order,
        "section_order": order,
        "audio_start_ms": order * 3000,
        "audio_end_ms": (order + 1) * 3000,
        "duration_sec": 3.0,
        "script_text": "The narration points to a physical clue.",
        "visual_intent": "specific physical clue with visible detail",
        "visual_type": "action",
        "visual_category": "object",
        "environment": "indoor_office",
        "flux_prompt": prompt,
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "object",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
        "media_strategy": "flux_generated",
        "media_url": "",
        "media_type": "image",
        "start_hint": start_hint,
        "end_hint": end_hint,
        "storyboard_segment_label": label,
        "storyboard_batch_label": label,
        "storyboard_segment_index": 1 if label == "[SECTION 1]" else 2,
        "storyboard_batch_local_order": 0,
    }


def raw_retry_beat() -> dict:
    return {
        "beat_order": 0,
        "start_hint": "Mara steps onto the porch",
        "end_hint": "checks the quiet driveway",
        "visual_intent": "specific porch clue with visible driveway detail",
        "visual_type": "action",
        "visual_category": "object",
        "environment": "indoor_office",
        "flux_prompt": (
            "Interior office desk clue beside a practical lamp, brass handle, "
            "paper folder turned away from camera, documentary photograph, "
            "sharp focus, no readable text"
        ),
        "effect": "cut",
        "color_grade": "desaturated",
        "transition_to_next": "cut",
        "motif": "object",
        "beat_intensity": "medium",
        "suggested_duration_sec": 3.0,
    }


class StoryboardSegmentRetryTest(unittest.TestCase):
    def test_validation_retry_regenerates_only_offending_segment(self) -> None:
        script = (
            "[SECTION 1]\n"
            "Eli opens the office drawer and studies the brass handle.\n"
            "[SECTION 2]\n"
            "Mara steps onto the porch and checks the quiet driveway."
        )
        transcript_words = (
            "Eli opens the office drawer and studies the brass handle "
            "Mara steps onto the porch and checks the quiet driveway"
        ).split()
        transcript = [
            {"word": word, "start": index * 0.3, "end": (index + 1) * 0.3}
            for index, word in enumerate(transcript_words)
        ]
        initial_beats = [
            section_beat(
                0,
                "[SECTION 1]",
                prompt=(
                    "Interior office drawer clue beside a practical lamp, brass handle, "
                    "documentary photograph, sharp focus, no readable text"
                ),
                start_hint="Eli opens the office drawer",
                end_hint="studies the brass handle",
            ),
            section_beat(
                1,
                "[SECTION 2]",
                prompt="ominous mood around a porch",
                start_hint="Mara steps onto the porch",
                end_hint="checks the quiet driveway",
            ),
        ]
        calls: list[dict] = []

        def fake_claude(**kwargs):
            calls.append(kwargs)
            return (
                {
                    "storyboard_status": "APPROVED",
                    "overall_style": "documentary continuity",
                    "global_notes": [],
                    "beats": [raw_retry_beat()],
                },
                {"input_tokens": 100, "output_tokens": 200},
            )

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_claude):
            result = _run_storyboard_validation(
                beats=initial_beats,
                voice_script=script,
                source_audio=SimpleNamespace(duration_ms=6000, whisper_transcript=transcript),
                channel=SimpleNamespace(niche="true scary stories", tone="tense"),
                script_format="youtube_long",
                allow_legacy_fallback=True,
                source_lang="en",
            )

        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 1)
        self.assertIn("Segment: [SECTION 2]", calls[0]["user_message"])
        self.assertNotIn("Segment: [SECTION 1]", calls[0]["user_message"])
        self.assertIn("forbidden_flux_word", calls[0]["user_message"])
        self.assertEqual([beat["storyboard_batch_label"] for beat in result], ["[SECTION 1]", "[SECTION 2]"])
        self.assertNotIn("ominous", result[1]["flux_prompt"].lower())

        # Kept-segment integrity: SECTION 1's beat survives byte-identical on
        # the fields that matter, and both beats are re-mapped onto real
        # Whisper timings (renumbered 0..1, "Mara" = token 10 → 3 000 ms).
        self.assertEqual(
            result[0]["flux_prompt"],
            initial_beats[0]["flux_prompt"],
        )
        self.assertEqual([b["beat_order"] for b in result], [0, 1])
        self.assertEqual(result[0]["audio_start_ms"], 0)
        self.assertEqual(result[1]["audio_start_ms"], 3_000)
        self.assertEqual(result[-1]["audio_end_ms"], 6_000)

    def test_missing_provenance_falls_back_to_full_retry(self) -> None:
        script = (
            "[SECTION 1]\n"
            "Eli opens the office drawer and studies the brass handle.\n"
            "[SECTION 2]\n"
            "Mara steps onto the porch and checks the quiet driveway."
        )
        transcript_words = (
            "Eli opens the office drawer and studies the brass handle "
            "Mara steps onto the porch and checks the quiet driveway"
        ).split()
        transcript = [
            {"word": word, "start": index * 0.3, "end": (index + 1) * 0.3}
            for index, word in enumerate(transcript_words)
        ]
        # Beats WITHOUT storyboard_batch_label provenance (e.g. produced by a
        # pre-provenance code version): the orchestrator cannot scope the
        # retry and must fall back to regenerating every segment.
        initial_beats = []
        for order, (prompt, sh, eh) in enumerate([
            ("desk drawer clue, documentary photograph", "Eli opens the office drawer", "studies the brass handle"),
            ("ominous mood around a porch", "Mara steps onto the porch", "checks the quiet driveway"),
        ]):
            beat = section_beat(order, "", prompt=prompt, start_hint=sh, end_hint=eh)
            for key in ("storyboard_segment_label", "storyboard_batch_label",
                        "storyboard_segment_index", "storyboard_batch_local_order"):
                beat.pop(key)
            initial_beats.append(beat)

        calls: list[dict] = []

        def fake_claude(**kwargs):
            calls.append(kwargs)
            return (
                {
                    "storyboard_status": "APPROVED",
                    "overall_style": "documentary continuity",
                    "global_notes": [],
                    "beats": [raw_retry_beat()],
                },
                {"input_tokens": 100, "output_tokens": 200},
            )

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_claude):
            with self.assertLogs(
                "app.agents.agent4_visuals.services.visual_orchestrator", level="WARNING"
            ) as logs:
                result = _run_storyboard_validation(
                    beats=initial_beats,
                    voice_script=script,
                    source_audio=SimpleNamespace(duration_ms=6000, whisper_transcript=transcript),
                    channel=SimpleNamespace(niche="true scary stories", tone="tense"),
                    script_format="youtube_long",
                    allow_legacy_fallback=True,
                    source_lang="en",
                )

        self.assertIsNotNone(result)
        # Full retry: BOTH segments regenerated, each carrying the MAJOR
        # constraint text in its user message.
        self.assertEqual(len(calls), 2)
        segments_called = {c["user_message"].split("Segment: ")[1].split(" —")[0] for c in calls}
        self.assertEqual(segments_called, {"[SECTION 1]", "[SECTION 2]"})
        for c in calls:
            self.assertIn("forbidden_flux_word", c["user_message"])
        self.assertTrue(
            any("STORYBOARD_SEGMENT_RETRY_FALLBACK" in m and "no_batch_provenance" in m
                for m in logs.output),
            logs.output,
        )


class RetryConstraintMappingTest(unittest.TestCase):
    """Unit coverage for _storyboard_retry_constraints_by_batch — the mapping
    that makes aggregate MAJORs (visual_monotony) affordable by scoping the
    retry to the batches that produced the supporting findings."""

    def _beats(self):
        b0 = section_beat(0, "[INTRO]", prompt="p0", start_hint="a", end_hint="a")
        b1 = section_beat(1, "[SECTION 1]", prompt="p1", start_hint="b", end_hint="b")
        b2 = section_beat(2, "[SECTION 2]", prompt="p2", start_hint="c", end_hint="c")
        b2["motif"] = "document"
        return [b0, b1, b2]

    def test_beat_scoped_major_maps_to_its_own_batch_only(self):
        issues = [{"severity": "MAJOR", "beat_order": 1, "check": "forbidden_flux_word",
                   "description": "bad word"}]
        constraints = _storyboard_retry_constraints_by_batch(self._beats(), issues)
        self.assertEqual(sorted(constraints), ["[SECTION 1]"])
        self.assertIn("forbidden_flux_word", constraints["[SECTION 1]"])

    def test_visual_monotony_maps_to_supporting_finding_batches(self):
        issues = [
            {"severity": "MAJOR", "beat_order": -1, "check": "visual_monotony",
             "description": "aggregate"},
            {"severity": "MINOR", "beat_order": 2, "check": "consecutive_same_environment",
             "description": "run"},
        ]
        constraints = _storyboard_retry_constraints_by_batch(self._beats(), issues)
        self.assertEqual(sorted(constraints), ["[SECTION 2]"])

    def test_document_saturation_maps_to_documentish_batches(self):
        issues = [{"severity": "MAJOR", "beat_order": -1, "check": "document_saturation",
                   "description": "too many documents"}]
        constraints = _storyboard_retry_constraints_by_batch(self._beats(), issues)
        self.assertEqual(sorted(constraints), ["[SECTION 2]"])  # only b2 is document-ish

    def test_unmappable_major_targets_all_batches(self):
        issues = [{"severity": "MAJOR", "beat_order": -1, "check": "some_future_aggregate",
                   "description": "storyboard-wide"}]
        constraints = _storyboard_retry_constraints_by_batch(self._beats(), issues)
        self.assertEqual(sorted(constraints), ["[INTRO]", "[SECTION 1]", "[SECTION 2]"])

    def test_no_major_returns_empty(self):
        issues = [{"severity": "MINOR", "beat_order": 1, "check": "near_duplicate_beat",
                   "description": "minor only"}]
        self.assertEqual(_storyboard_retry_constraints_by_batch(self._beats(), issues), {})


if __name__ == "__main__":
    unittest.main()
