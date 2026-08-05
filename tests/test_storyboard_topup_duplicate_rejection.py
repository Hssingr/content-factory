"""Runtime proof: shortfall-topup beats that restate already-delivered
narration are dropped instead of appended (content 069d8d06 root cause,
2026-08-05).

Root cause of the reviewer-observed 47-65s misalignment (9 beats holding for
1.0/2.0/3.0 s against semantically unrelated images): the shortfall-topup
call in `split_into_beats` (Phase B1) re-sends the IDENTICAL ``segment_text``
as the original delivery, with nothing telling the model which beats already
exist for it. A real production run showed this produce an independent
re-telling of the whole segment from its own beginning — 9 topup beats whose
hints restated content the original 11-beat delivery already covered. None
of those restated hints could hint-match forward of the transcript cursor
the original delivery's own beats had already advanced past, so every one of
them fell back to proportional interpolation, packing a real ~11 s gap with
9 beats showing the wrong images.

``_filter_duplicate_topup_beats`` keeps only topup beats whose start_hint +
end_hint tokens are NOT already substantially covered by a single
already-delivered beat's hint tokens — deterministic set overlap, no AI
judgment, no retry.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent4_visuals.subagents import storyboard


class TestFilterDuplicateTopupBeatsUnit:
    def test_near_verbatim_restatement_is_dropped(self):
        # Real production hint pair from content 069d8d06: beat 12's topup
        # hint restates beat 1's own already-delivered hint almost verbatim.
        delivered = [{
            "beat_order": 1,
            "start_hint": "That mountain is Cerro Rico, high in",
            "end_hint": "named Diego Huallpa lights a fire",
        }]
        topup = [{
            "beat_order": 0,
            "start_hint": "That mountain is Cerro Rico, high in the",
            "end_hint": "named Diego Huallpa lights a fire",
        }]
        kept, dropped = storyboard._filter_duplicate_topup_beats(topup, delivered, "en")
        assert kept == []
        assert dropped == 1

    def test_genuinely_new_content_is_kept(self):
        delivered = [{
            "beat_order": 1,
            "start_hint": "That mountain is Cerro Rico, high in",
            "end_hint": "named Diego Huallpa lights a fire",
        }]
        topup = [{
            "beat_order": 0,
            "start_hint": "And ships don't always sail on time",
            "end_hint": "Storms scatter fleets across the ocean",
        }]
        kept, dropped = storyboard._filter_duplicate_topup_beats(topup, delivered, "en")
        assert kept == topup
        assert dropped == 0

    def test_beat_with_no_hint_text_is_kept_not_treated_as_duplicate(self):
        delivered = [{"beat_order": 0, "start_hint": "alpha bravo", "end_hint": "charlie delta"}]
        topup = [{"beat_order": 0, "start_hint": "", "end_hint": ""}]
        kept, dropped = storyboard._filter_duplicate_topup_beats(topup, delivered, "en")
        assert kept == topup
        assert dropped == 0

    def test_partial_overlap_below_threshold_is_kept(self):
        # Shares only "the"/"mountain" with the delivered beat — well under
        # the 0.65 overlap threshold — genuinely distinct content, not a
        # restatement.
        delivered = [{
            "beat_order": 0,
            "start_hint": "The mountain overflowed with silver",
            "end_hint": "kept going bankrupt",
        }]
        topup = [{
            "beat_order": 0,
            "start_hint": "The mountain cast a long shadow at dusk",
            "end_hint": "over the distant valley floor below",
        }]
        kept, dropped = storyboard._filter_duplicate_topup_beats(topup, delivered, "en")
        assert kept == topup
        assert dropped == 0

    def test_checks_every_delivered_beat_not_just_the_first(self):
        delivered = [
            {"beat_order": 0, "start_hint": "unrelated opening content here", "end_hint": "more filler text"},
            {"beat_order": 1, "start_hint": "That mountain is Cerro Rico, high in", "end_hint": "named Diego Huallpa lights a fire"},
        ]
        topup = [{
            "beat_order": 0,
            "start_hint": "That mountain is Cerro Rico, high in the",
            "end_hint": "named Diego Huallpa lights a fire",
        }]
        kept, dropped = storyboard._filter_duplicate_topup_beats(topup, delivered, "en")
        assert kept == []
        assert dropped == 1


def _full_beat(order: int, start_hint: str, end_hint: str) -> dict:
    return {
        "beat_order": order,
        "start_hint": start_hint,
        "end_hint": end_hint,
        "visual_intent": f"intent {order}",
        "visual_type": "b-roll",
        "visual_category": "place",
        "environment": "other",
        "flux_prompt": f"prompt {order}",
        "effect": "slow_zoom",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "motif": "other",
        "beat_intensity": "medium",
        "suggested_duration_sec": 4.0,
    }


class TestSplitIntoBeatsDropsDuplicateTopup:
    """Full split_into_beats chain — mirrors the shortfall-topup harness in
    tests/test_storyboard_shortfall_topup.py, with a topup response whose
    beats restate the original delivery's narration instead of covering new
    ground (the exact content 069d8d06 shape)."""

    def test_topup_beats_that_restate_delivered_narration_are_all_dropped(self, caplog):
        delivered_beats = [
            _full_beat(i, f"real narration phrase number {i}", f"real narration phrase number {i} end")
            for i in range(8)
        ]
        # Light paraphrase of the SAME 8 beats — not identical text, proving
        # the rejection is overlap-based, not a literal string-equality check.
        duplicate_topup_beats = [
            _full_beat(i, f"real narration phrase number {i} indeed", f"real narration phrase number {i} end truly")
            for i in range(6)
        ]

        calls: list[int] = []

        def fake_batch(**kwargs):
            calls.append(kwargs["target_beat_count"])
            beats = delivered_beats if len(calls) == 1 else duplicate_topup_beats
            return (
                {"overall_style": "test", "beats": beats},
                {"output_tokens": len(beats) * 10},
                {"input_tokens": 20, "elapsed_ms": 1, "attempt_count": 1, "was_truncated": False},
            )

        hint_stats = {"total_hints": 0, "valid_hints": 0, "invalid_hints": 0}
        with (
            patch.object(storyboard, "generate_storyboard_batch", side_effect=fake_batch),
            patch.object(storyboard, "_harden_hints", side_effect=lambda beats, *_a, **_k: (beats, hint_stats)),
            patch.object(storyboard, "map_storyboard_beats_to_timestamps",
                         side_effect=lambda beats, *_a, **_k: beats),
            patch.object(storyboard, "_apply_visual_hold_cap",
                         side_effect=lambda beats, *_a, **_k: beats),
            caplog.at_level(logging.WARNING, logger=storyboard.logger.name),
        ):
            result = storyboard.split_into_beats(
                voice_script="[SECTION 1]\n" + " ".join(["word"] * 140),
                duration_ms=56_000,  # youtube_long pacing computes exactly 14 requested beats
                channel=SimpleNamespace(niche="any", tone="any"),
                script_format="youtube_long",
                whisper_transcript=[{"word": "word", "start": 0.0, "end": 0.1}],
                allow_legacy_fallback=True,
            )

        # This fixture's voice_script ("word word word...") shares no real
        # vocabulary with any beat's hint, so _locate_uncovered_span finds
        # nothing locatable and conservatively falls back to the whole
        # segment (target=14) — span-scoping is exercised for real in
        # tests/test_storyboard_shortfall_topup.py; this file's fixture
        # tests the duplicate filter specifically, independent of the exact
        # requested count.
        assert calls == [14, 14]
        # All 6 topup beats were duplicates of already-delivered narration —
        # none survive. Only the 8 originally-delivered beats ship, never
        # padded out with restated content that can't hint-match anyway.
        assert len(result) == 8
        assert [beat["beat_order"] for beat in result] == list(range(8))
        assert "STORYBOARD_TOPUP_DUPLICATE_DROPPED" in caplog.text
        assert "STORYBOARD_TOPUP_DUPLICATE_SUMMARY" in caplog.text
        assert "duplicates_dropped=6" in caplog.text
        # The shortfall is still accepted (not retried a second time) —
        # this fix changes WHICH beats count toward the topup, not the
        # single-bounded-follow-up contract Phase B1 already established.
        assert "STORYBOARD_SHORTFALL_ACCEPTED" in caplog.text

    def test_topup_beats_with_genuinely_new_narration_are_kept(self, caplog):
        """Regression guard: a topup call that actually covers NEW territory
        (the intended, non-broken case) must not be dropped."""
        delivered_beats = [
            _full_beat(i, f"real narration phrase number {i}", f"real narration phrase number {i} end")
            for i in range(8)
        ]
        new_topup_beats = [
            _full_beat(i, f"an entirely different later scene {i}", f"continuing forward from there {i}")
            for i in range(6)
        ]

        calls: list[int] = []

        def fake_batch(**kwargs):
            calls.append(kwargs["target_beat_count"])
            beats = delivered_beats if len(calls) == 1 else new_topup_beats
            return (
                {"overall_style": "test", "beats": beats},
                {"output_tokens": len(beats) * 10},
                {"input_tokens": 20, "elapsed_ms": 1, "attempt_count": 1, "was_truncated": False},
            )

        hint_stats = {"total_hints": 0, "valid_hints": 0, "invalid_hints": 0}
        with (
            patch.object(storyboard, "generate_storyboard_batch", side_effect=fake_batch),
            patch.object(storyboard, "_harden_hints", side_effect=lambda beats, *_a, **_k: (beats, hint_stats)),
            patch.object(storyboard, "map_storyboard_beats_to_timestamps",
                         side_effect=lambda beats, *_a, **_k: beats),
            patch.object(storyboard, "_apply_visual_hold_cap",
                         side_effect=lambda beats, *_a, **_k: beats),
            caplog.at_level(logging.WARNING, logger=storyboard.logger.name),
        ):
            result = storyboard.split_into_beats(
                voice_script="[SECTION 1]\n" + " ".join(["word"] * 140),
                duration_ms=56_000,
                channel=SimpleNamespace(niche="any", tone="any"),
                script_format="youtube_long",
                whisper_transcript=[{"word": "word", "start": 0.0, "end": 0.1}],
                allow_legacy_fallback=True,
            )

        assert calls == [14, 14]  # see note above on this fixture's voice_script
        assert len(result) == 14
        assert [beat["beat_order"] for beat in result] == list(range(14))
        assert "STORYBOARD_TOPUP_DUPLICATE_DROPPED" not in caplog.text
