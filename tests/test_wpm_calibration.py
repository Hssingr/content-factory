"""Runtime proof for WPM calibration (roadmap 3.8 / audit G-7).

Real production data showed every duration assumption baked into the pipeline
was ~25% off: parent narration measured 135 wpm (assumed 150), child Shorts
measured 120 wpm (planner math assumed 180 / "3 words per second"), and the
quality gate had no upper bound at all (a real script reached 1,799 words
against the 1,200-1,600 spec). This proves:

  1. script_estimator.compute_measured_wpm()/get_calibrated_wpm() compute a
     real rolling average from AudioFile rows (a fake in-memory DB — same
     precedent as test_agent4_media_validation.py's _FakeDb — not a live SQL
     connection; no external paid API involved on this path at all).
  2. estimate_duration_sec() actually uses the calibrated rate when a db is
     provided, and the pre-existing static fallback when it is not.
  3. script_checks.check_maximum_length() fires MAJOR at the calibrated
     ceiling, and is really wired into Agent 2's quality-gate issue collector
     (_collect_quality_gate_issues), not just defined and unused.
  4. The recalibrated Short word band constants (_MIN_SHORT_WORDS=125,
     _MAX_SHORT_WORDS=270) are in place.
  5. storyboard._estimate_beat_count() accepts a wpm override, and the real
     split_into_beats() chain picks up a calibrated per-language wpm for its
     diagnostic log when a db is supplied — with only the paid Claude
     boundary (generate_storyboard_batch's call_claude_structured_with_usage)
     stubbed, nothing internal.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))

from app.services import script_estimator
from app.services.script_checks import check_maximum_length
from app.agents.agent2_discovery.services.scripts import (
    _MIN_SHORT_WORDS,
    _MAX_SHORT_WORDS,
    _collect_quality_gate_issues,
)
from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.subagents.storyboard import (
    _estimate_beat_count,
    _WORDS_PER_MINUTE,
    split_into_beats,
)


# ── Fake DB (in-memory, no SQL) — same precedent as test_agent4_media_validation.py ──

class _FakeAudioFile:
    def __init__(self, language: str, duration_ms: int, whisper_transcript: list | None):
        self.id = uuid.uuid4()
        self.language = language
        self.duration_ms = duration_ms
        self.whisper_transcript = whisper_transcript


class _FakeQuery:
    def __init__(self, rows: list, tracker: dict | None = None):
        self._rows = rows
        self._tracker = tracker if tracker is not None else {}

    def filter(self, *args, **kwargs):
        self._tracker.setdefault("filters", []).extend(args)
        return self

    def join(self, *args, **kwargs):
        self._tracker["joined"] = True
        return self

    def order_by(self, *args, **kwargs):
        self._tracker.setdefault("order_by", []).extend(args)
        return self

    def limit(self, n):
        return _FakeQuery(self._rows[:n], self._tracker)

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows: list[_FakeAudioFile]):
        self._rows = rows
        self.tracker: dict = {}

    def query(self, model):
        return _FakeQuery(self._rows, self.tracker)


def _transcript(word_count: int) -> list[dict]:
    return [{"word": f"w{i}", "start": i * 0.4, "end": (i + 1) * 0.4} for i in range(word_count)]


class TestContentKindScopedCalibration(unittest.TestCase):
    """Fresh full-system audit §3.4: parents (~135 wpm) and Shorts (~120 wpm)
    are different distributions — the calibration window must be scopeable to
    one content kind via a Content join, and unscoped calls must keep the
    original blended (no-join) behavior."""

    def _rows(self):
        return [_FakeAudioFile("en", 60_000, _transcript(130)) for _ in range(5)]

    def test_default_call_does_not_join_content(self):
        db = _FakeDb(self._rows())
        script_estimator.compute_measured_wpm(db, "en")
        self.assertNotIn("joined", db.tracker)

    def test_kind_scoped_call_joins_and_filters_is_short_episode(self):
        for kind in (True, False):
            db = _FakeDb(self._rows())
            result = script_estimator.compute_measured_wpm(db, "en", is_short_episode=kind)
            self.assertTrue(db.tracker.get("joined"), f"kind={kind} must join Content")
            filter_strs = [str(f) for f in db.tracker.get("filters", [])]
            self.assertTrue(
                any("is_short_episode" in s for s in filter_strs),
                f"kind={kind} must filter on Content.is_short_episode: {filter_strs}",
            )
            self.assertIsNotNone(result)

    def test_estimate_duration_sec_threads_kind_to_query(self):
        db = _FakeDb(self._rows())
        script_estimator.estimate_duration_sec(
            "word " * 100, "en", db=db, is_short_episode=True,
        )
        self.assertTrue(db.tracker.get("joined"))


class TestChannelScopedCalibration(unittest.TestCase):
    """Phase E2: channel-owned voices must not consume other channels' rates."""

    def _rows(self):
        return [_FakeAudioFile("en", 60_000, _transcript(144)) for _ in range(3)]

    def test_channel_scope_joins_content_and_filters_channel_id(self):
        db = _FakeDb(self._rows())
        channel_id = uuid.uuid4()

        result = script_estimator.compute_measured_wpm(
            db, "en", is_short_episode=False, channel_id=channel_id,
        )

        self.assertAlmostEqual(result, 144.0)
        self.assertTrue(db.tracker.get("joined"))
        filter_strs = [str(value) for value in db.tracker.get("filters", [])]
        self.assertTrue(any("channel_id" in value for value in filter_strs), filter_strs)

    def test_estimate_duration_threads_channel_scope_to_query(self):
        db = _FakeDb(self._rows())
        script_estimator.estimate_duration_sec(
            "word " * 144, "en", db=db, is_short_episode=False,
            channel_id=uuid.uuid4(),
        )
        filter_strs = [str(value) for value in db.tracker.get("filters", [])]
        self.assertTrue(any("channel_id" in value for value in filter_strs), filter_strs)

    def test_rolling_window_orders_by_generated_at_not_random_uuid_alone(self):
        db = _FakeDb(self._rows())
        script_estimator.compute_measured_wpm(db, "en", channel_id=uuid.uuid4())
        order_strs = [str(value) for value in db.tracker.get("order_by", [])]
        self.assertTrue(any("generated_at" in value for value in order_strs), order_strs)


class TestComputeMeasuredWpm(unittest.TestCase):
    def test_returns_none_below_minimum_samples(self):
        db = _FakeDb([_FakeAudioFile("en", 60_000, _transcript(135))])
        self.assertIsNone(script_estimator.compute_measured_wpm(db, "en"))

    def test_computes_rolling_average_of_real_rows(self):
        # Three rows, each exactly 120 wpm: 120 words in 60s.
        rows = [_FakeAudioFile("en", 60_000, _transcript(120)) for _ in range(3)]
        db = _FakeDb(rows)
        result = script_estimator.compute_measured_wpm(db, "en")
        self.assertAlmostEqual(result, 120.0, places=3)

    def test_ignores_rows_without_whisper_transcript(self):
        rows = [
            _FakeAudioFile("en", 60_000, _transcript(120)),
            _FakeAudioFile("en", 60_000, _transcript(120)),
            _FakeAudioFile("en", 60_000, None),
            _FakeAudioFile("en", 60_000, []),
        ]
        db = _FakeDb(rows)
        # Only 2 usable rows — below _MIN_SAMPLES_FOR_CALIBRATION (3).
        self.assertIsNone(script_estimator.compute_measured_wpm(db, "en"))

    def test_averages_mixed_real_rates(self):
        rows = [
            _FakeAudioFile("fr", 60_000, _transcript(130)),
            _FakeAudioFile("fr", 60_000, _transcript(140)),
            _FakeAudioFile("fr", 60_000, _transcript(150)),
        ]
        db = _FakeDb(rows)
        result = script_estimator.compute_measured_wpm(db, "fr")
        self.assertAlmostEqual(result, 140.0, places=3)


class TestGetCalibratedWpm(unittest.TestCase):
    def test_no_db_uses_static_fallback(self):
        self.assertEqual(
            script_estimator.get_calibrated_wpm(None, "en"),
            script_estimator.SPEECH_RATES["en"],
        )

    def test_insufficient_samples_uses_static_fallback(self):
        db = _FakeDb([_FakeAudioFile("de", 60_000, _transcript(130))])
        self.assertEqual(
            script_estimator.get_calibrated_wpm(db, "de"),
            script_estimator.SPEECH_RATES["de"],
        )

    def test_enough_samples_uses_measured_rate_over_static(self):
        rows = [_FakeAudioFile("en", 60_000, _transcript(135)) for _ in range(5)]
        db = _FakeDb(rows)
        result = script_estimator.get_calibrated_wpm(db, "en")
        self.assertAlmostEqual(result, 135.0, places=3)
        self.assertNotEqual(result, script_estimator.SPEECH_RATES["en"])

    def test_unknown_language_falls_back_to_default_rate(self):
        self.assertEqual(
            script_estimator.get_calibrated_wpm(None, "xx"),
            script_estimator._DEFAULT_RATE,
        )


class TestEstimateDurationSec(unittest.TestCase):
    def test_no_db_matches_pre_existing_static_behavior(self):
        script = " ".join(["word"] * 150)
        result = script_estimator.estimate_duration_sec(script, "en")
        expected = round((150 / script_estimator.SPEECH_RATES["en"]) * 60.0, 1)
        self.assertEqual(result, expected)

    def test_with_db_uses_calibrated_rate(self):
        rows = [_FakeAudioFile("en", 60_000, _transcript(120)) for _ in range(4)]
        db = _FakeDb(rows)
        script = " ".join(["word"] * 120)
        result = script_estimator.estimate_duration_sec(script, "en", db=db)
        # 120 words at a calibrated 120 wpm == exactly 60s.
        self.assertEqual(result, 60.0)


class TestCheckMaximumLength(unittest.TestCase):
    def test_under_ceiling_youtube_long_passes(self):
        script = " ".join(["word"] * 1600)
        self.assertEqual(check_maximum_length(script, "en", "youtube_long"), [])

    def test_over_ceiling_youtube_long_is_major(self):
        script = " ".join(["word"] * 1799)  # the real measured production overage
        issues = check_maximum_length(script, "en", "youtube_long")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "MAJOR")
        self.assertEqual(issues[0]["category"], "maximum_length")
        self.assertIn("1799", issues[0]["description"])

    def test_short_form_uses_lower_ceiling(self):
        script = " ".join(["word"] * 801)
        issues = check_maximum_length(script, "en", "short_form")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "MAJOR")

    def test_short_form_under_own_ceiling_passes(self):
        script = " ".join(["word"] * 750)
        self.assertEqual(check_maximum_length(script, "en", "short_form"), [])


class TestQualityGateWiresMaxLengthCheck(unittest.TestCase):
    """Proves check_maximum_length is really reachable through Agent 2's
    quality-gate issue collector, not just defined in isolation."""

    def test_overlong_script_produces_major_issue_in_det_majors(self):
        overlong_script = " ".join(["word"] * 1799)
        current = {"voice_script": overlong_script}
        issue_group = _collect_quality_gate_issues(
            current=current, language="source", script_format="youtube_long",
        )
        length_majors = [i for i in issue_group["length_det"] if i["severity"] == "MAJOR"]
        self.assertEqual(len(length_majors), 1)
        self.assertTrue(any(i["category"] == "maximum_length" for i in issue_group["det_majors"]))

    def test_normal_length_script_has_no_length_issue(self):
        normal_script = " ".join(["word"] * 1400)
        current = {"voice_script": normal_script}
        issue_group = _collect_quality_gate_issues(
            current=current, language="source", script_format="youtube_long",
        )
        self.assertEqual(issue_group["length_det"], [])


class TestShortWordBandRecalibrated(unittest.TestCase):
    def test_min_and_max_match_measured_compressed_rate_band(self):
        # Recalibrated (2026-07-16) to the measured ~176 wpm POST-silence-
        # compression Short rate (run 41f7eeb8: 246 words → 83.7 s). At that
        # rate the old 135-word floor was ~46 s — far under the binding 61 s
        # Short floor; 190 words ≈ 65 s keeps real margin. The 270 cap
        # (~92 s) is telemetry-only: an over-cap Short ships, never fails.
        self.assertEqual(_MIN_SHORT_WORDS, 190)
        self.assertEqual(_MAX_SHORT_WORDS, 270)


class TestEstimateBeatCountWpmOverride(unittest.TestCase):
    def test_default_uses_static_words_per_minute(self):
        script = " ".join(["word"] * 270)  # 270 words
        default_result = _estimate_beat_count(script, "youtube_long")
        explicit_result = _estimate_beat_count(script, "youtube_long", wpm=_WORDS_PER_MINUTE)
        self.assertEqual(default_result, explicit_result)

    def test_slower_wpm_yields_more_estimated_beats(self):
        script = " ".join(["word"] * 270)
        fast_estimate = _estimate_beat_count(script, "youtube_long", wpm=180.0)
        slow_estimate = _estimate_beat_count(script, "youtube_long", wpm=90.0)
        # Slower narration -> more real seconds for the same word count -> more beats.
        self.assertGreater(slow_estimate, fast_estimate)

    def test_static_default_recalibrated_to_135(self):
        self.assertEqual(_WORDS_PER_MINUTE, 135)


def _storyboard_response() -> tuple[dict, dict]:
    return (
        {
            "storyboard_status": "APPROVED",
            "overall_style": "documentary continuity",
            "global_notes": [],
            "beats": [
                {
                    "beat_order": 0,
                    "start_hint": "the quick brown",
                    "end_hint": "over the lazy",
                    "visual_intent": "a dog running in a field",
                    "visual_type": "action",
                    "visual_category": "object",
                    "environment": "forest_nature",
                    "flux_prompt": "a dog running through tall grass, wide shot, photorealistic",
                    "effect": "cut",
                    "color_grade": "desaturated",
                    "transition_to_next": "cut",
                    "motif": "object",
                    "beat_intensity": "medium",
                    "suggested_duration_sec": 3.0,
                }
            ],
        },
        {"input_tokens": 100, "output_tokens": 200},
    )


class TestSplitIntoBeatsUsesCalibratedWpmForDiagnostics(unittest.TestCase):
    """Runtime chain proof: split_into_beats() picks up a real calibrated wpm
    from a db session for its diagnostic log, with only the paid Claude
    boundary (generate_storyboard_batch's underlying call) stubbed."""

    def test_calibrated_wpm_appears_in_diagnostic_log_when_db_provided(self):
        transcript_words = "the quick brown fox jumps over the lazy dog".split()
        transcript = [
            {"word": word, "start": i * 0.4, "end": (i + 1) * 0.4}
            for i, word in enumerate(transcript_words)
        ]
        # 5 real AudioFile rows at exactly 100 wpm -> calibrated rate should
        # be 100, distinct from the static default (135).
        rows = [_FakeAudioFile("en", 60_000, _transcript(100)) for _ in range(5)]
        db = _FakeDb(rows)

        def fake_claude(**kwargs):
            return _storyboard_response()

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_claude):
            with self.assertLogs(
                "app.agents.agent4_visuals.subagents.storyboard", level="DEBUG"
            ) as logs:
                beats = split_into_beats(
                    voice_script="[SECTION 1]\nthe quick brown fox jumps over the lazy dog",
                    duration_ms=3600,
                    channel=SimpleNamespace(niche="nature", tone="calm"),
                    script_format="youtube_long",
                    whisper_transcript=transcript,
                    db=db,
                    language="en",
                )

        self.assertTrue(beats)
        combined = "\n".join(logs.output)
        self.assertIn("WPM(100)", combined)
        self.assertNotIn("WPM(135)", combined)

    def test_no_db_falls_back_to_static_wpm_in_diagnostic_log(self):
        transcript_words = "the quick brown fox jumps over the lazy dog".split()
        transcript = [
            {"word": word, "start": i * 0.4, "end": (i + 1) * 0.4}
            for i, word in enumerate(transcript_words)
        ]

        def fake_claude(**kwargs):
            return _storyboard_response()

        with patch.object(system_prompt, "call_claude_structured_with_usage", side_effect=fake_claude):
            with self.assertLogs(
                "app.agents.agent4_visuals.subagents.storyboard", level="DEBUG"
            ) as logs:
                beats = split_into_beats(
                    voice_script="[SECTION 1]\nthe quick brown fox jumps over the lazy dog",
                    duration_ms=3600,
                    channel=SimpleNamespace(niche="nature", tone="calm"),
                    script_format="youtube_long",
                    whisper_transcript=transcript,
                    language="en",
                )

        self.assertTrue(beats)
        combined = "\n".join(logs.output)
        self.assertIn("WPM(135)", combined)


if __name__ == "__main__":
    unittest.main()
