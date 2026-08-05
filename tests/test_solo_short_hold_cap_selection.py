"""Phase A1: direct-storyboard Solo Shorts select the Short hold cap."""

from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent4_visuals.subagents import storyboard


def _transcript():
    return [
        {"word": word, "start": i * 0.5, "end": (i + 1) * 0.5}
        for i, word in enumerate("one two three four five six seven eight nine ten".split())
    ]


def _run(is_short_episode: bool):
    raw = [{"beat_order": 0, "start_hint": "one two", "end_hint": "nine ten"}]
    observed = {}

    def fake_map(beats, transcript, duration_ms, **kwargs):
        observed["map_cap"] = kwargs["max_hold_ms"]
        return [{"beat_order": 0, "audio_start_ms": 0, "audio_end_ms": duration_ms}]

    def fake_cap(beats, *, max_hold_ms, **kwargs):
        observed["apply_cap"] = max_hold_ms
        observed["log_prefix"] = kwargs["log_prefix"]
        return [{**beats[0], "audio_end_ms": min(beats[0]["audio_end_ms"], max_hold_ms)}]

    with (
        patch.object(storyboard, "_split_voice_script_into_segments", return_value=[("[FULL]", "one two three")]),
        patch.object(storyboard, "generate_storyboard_batch", return_value=(
            {"beats": raw, "overall_style": ""},
            {"output_tokens": 1},
            {"input_tokens": 1, "elapsed_ms": 1, "attempt_count": 1},
        )),
        patch.object(storyboard, "_harden_hints", side_effect=lambda beats, *_a, **_k: (beats, {
            "total_hints": 0, "valid_hints": 0, "invalid_hints": 0,
        })),
        patch.object(storyboard, "_trim_beat_count_overshoot", side_effect=lambda beats, **_k: beats),
        patch.object(storyboard, "_merge_batches", side_effect=lambda batches: batches[0]),
        patch.object(storyboard, "_collapse_duplicate_hint_runs", side_effect=lambda beats, **_k: beats),
        patch.object(storyboard, "map_storyboard_beats_to_timestamps", side_effect=fake_map),
        patch.object(storyboard, "_apply_visual_hold_cap", side_effect=fake_cap),
    ):
        result = storyboard.split_into_beats(
            voice_script="one two three",
            duration_ms=8_000,
            channel=SimpleNamespace(niche="general", tone="neutral"),
            script_format="tiktok",
            whisper_transcript=_transcript(),
            is_short_episode=is_short_episode,
        )
    return result, observed


def test_solo_short_uses_short_cap_at_mapping_and_final_clamp():
    result, observed = _run(True)
    assert observed["map_cap"] == storyboard._SHORT_VISUAL_MAX_HOLD_MS
    assert observed["apply_cap"] == storyboard._SHORT_VISUAL_MAX_HOLD_MS
    assert observed["log_prefix"] == "SHORT_VISUAL_HOLD_CAP"
    assert all(
        beat["audio_end_ms"] - beat["audio_start_ms"]
        <= storyboard._SHORT_VISUAL_MAX_HOLD_MS
        for beat in result
    )


def test_parent_still_uses_parent_cap():
    _, observed = _run(False)
    assert observed["map_cap"] == storyboard._PARENT_VISUAL_MAX_HOLD_MS
    assert observed["apply_cap"] == storyboard._PARENT_VISUAL_MAX_HOLD_MS
    assert observed["log_prefix"] == "PARENT_VISUAL_HOLD_CAP"

