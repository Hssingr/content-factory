"""Runtime proof for Phase B1's single bounded storyboard shortfall top-up."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent4_visuals.subagents import storyboard


def _beat(order: int) -> dict:
    return {
        "beat_order": order,
        "start_hint": f"start hint words for beat {order}",
        "end_hint": f"ending hint words for beat {order}",
        "visual_intent": f"physical action {order}",
        "visual_type": "b-roll",
        "visual_category": "place",
        "environment": "other",
        "flux_prompt": f"cinematic illustration, physical scene {order}",
        "effect": "slow_zoom",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "motif": "other",
        "beat_intensity": "medium",
        "suggested_duration_sec": 4.0,
    }


def _response(count: int):
    return (
        {"overall_style": "test", "beats": [_beat(i) for i in range(count)]},
        {"output_tokens": count * 10},
        {"input_tokens": 20, "elapsed_ms": 1, "attempt_count": 1, "was_truncated": False},
    )


def _run(deliveries: list[int]):
    calls: list[int] = []

    def fake_batch(**kwargs):
        calls.append(kwargs["target_beat_count"])
        return _response(deliveries[len(calls) - 1])

    hint_stats = {"total_hints": 0, "valid_hints": 0, "invalid_hints": 0}
    with (
        patch.object(storyboard, "generate_storyboard_batch", side_effect=fake_batch),
        patch.object(storyboard, "_harden_hints", side_effect=lambda beats, *_a, **_k: (beats, hint_stats)),
        patch.object(storyboard, "map_storyboard_beats_to_timestamps",
                     side_effect=lambda beats, *_a, **_k: beats),
        patch.object(storyboard, "_apply_visual_hold_cap",
                     side_effect=lambda beats, *_a, **_k: beats),
    ):
        result = storyboard.split_into_beats(
            voice_script="[SECTION 1]\n" + " ".join(["word"] * 140),
            duration_ms=56_000,  # youtube_long pacing computes exactly 14 requested beats
            channel=SimpleNamespace(niche="any", tone="any"),
            script_format="youtube_long",
            whisper_transcript=[{"word": "word", "start": 0.0, "end": 0.1}],
            allow_legacy_fallback=True,
        )
    return calls, result


def test_eight_of_fourteen_requests_exact_six_beat_topup():
    calls, result = _run([8, 6])
    assert calls == [14, 6]
    assert len(result) == 14
    assert [beat["beat_order"] for beat in result] == list(range(14))


def test_thirteen_of_fourteen_needs_no_topup():
    calls, result = _run([13])
    assert calls == [14]
    assert len(result) == 13


def test_under_delivering_topup_is_accepted_without_a_second_topup(caplog):
    with caplog.at_level(logging.WARNING, logger=storyboard.logger.name):
        calls, result = _run([8, 2])
    assert calls == [14, 6]
    assert len(result) == 10
    assert "STORYBOARD_SHORTFALL_ACCEPTED" in caplog.text
