"""Runtime proof for Phase B1's single bounded storyboard shortfall top-up,
extended for the content-069d8d06 root-cause follow-up (2026-08-05,
code_report/TODO Task 1): the top-up call is scoped to the narration span the
original delivery did NOT cover, not the identical full segment text.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.agent4_visuals.subagents import storyboard


def _fill(i: int) -> str:
    """Unique letter-only token per integer — no vocabulary shared across
    different values of i, unlike a fixed "start hint words for beat {i}"
    template (whose 6 of 7 tokens are IDENTICAL across every beat regardless
    of order, which made _filter_duplicate_topup_beats's overlap check treat
    any two same-helper beats as near-duplicates purely from the shared
    boilerplate — a test-fixture bug, not a production one, caught by
    actually running this suite under pytest, 2026-08-05)."""
    letters = ""
    n = i
    while True:
        letters = chr(ord("a") + n % 26) + letters
        n //= 26
        if n == 0:
            break
    return "tok" + letters


_WORDS_PER_BEAT = 10
_TOTAL_BEATS = 14
_TOTAL_WORDS = _WORDS_PER_BEAT * _TOTAL_BEATS  # 140 — youtube_long pacing at
                                                 # duration_ms=56_000 computes
                                                 # exactly 14 requested beats
_ALL_WORDS = [_fill(i) for i in range(_TOTAL_WORDS)]
_VOICE_SCRIPT = "[SECTION 1]\n" + " ".join(_ALL_WORDS)


def _beat(order: int) -> dict:
    # Each beat's hint is its OWN real, locatable 10-word slice of
    # _ALL_WORDS/_VOICE_SCRIPT — unlike a synthetic template disconnected
    # from any real segment text, this lets _locate_uncovered_span actually
    # do real work against these fixtures instead of always falling back to
    # "nothing locatable, cover the whole segment".
    words = _ALL_WORDS[order * _WORDS_PER_BEAT:(order + 1) * _WORDS_PER_BEAT]
    hint = " ".join(words)
    return {
        "beat_order": order,
        "start_hint": hint,
        "end_hint": hint,
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


def _response(count: int, offset: int = 0):
    return (
        {"overall_style": "test", "beats": [_beat(offset + i) for i in range(count)]},
        {"output_tokens": count * 10},
        {"input_tokens": 20, "elapsed_ms": 1, "attempt_count": 1, "was_truncated": False},
    )


def _run(deliveries: list[int]):
    calls: list[int] = []
    call_kwargs: list[dict] = []

    def fake_batch(**kwargs):
        calls.append(kwargs["target_beat_count"])
        call_kwargs.append(kwargs)
        # Real distinct hint text per call (each beat's hint embeds its own
        # order number via _beat()) — a topup call must not reuse the SAME
        # order numbers as the original call, or its hints would collide
        # with the original delivery's own hints and be dropped as
        # duplicates by _filter_duplicate_topup_beats (2026-08-05 content
        # 069d8d06 root-cause fix). offset mirrors what a real topup call's
        # beats look like: new content, not a restatement of beat 0..N-1.
        offset = sum(deliveries[: len(calls) - 1])
        return _response(deliveries[len(calls) - 1], offset=offset)

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
            voice_script=_VOICE_SCRIPT,
            duration_ms=56_000,
            channel=SimpleNamespace(niche="any", tone="any"),
            script_format="youtube_long",
            whisper_transcript=[{"word": "word", "start": 0.0, "end": 0.1}],
            allow_legacy_fallback=True,
        )
    return calls, result, call_kwargs


def test_eight_of_fourteen_requests_exact_six_beat_topup():
    # 8 delivered beats cover words 0-79 (real, locatable hints); the
    # uncovered span is words 80-139 (60 words) -> at this segment's 10
    # words/beat ratio, topup_target = round(60/10) = 6 — the same number
    # the old raw-shortfall math (14-8) happened to produce too, but now
    # derived from the actual uncovered span, not the beat-count gap.
    calls, result, _ = _run([8, 6])
    assert calls == [14, 6]
    assert len(result) == 14
    assert [beat["beat_order"] for beat in result] == list(range(14))


def test_topup_call_receives_only_the_uncovered_span_not_the_full_segment():
    """Task 1's explicit runtime-proof requirement: the topup call's message
    must contain ONLY the uncovered span text — the already-covered
    sentences must be absent — with a target derived from span length."""
    _, _, call_kwargs = _run([8, 6])
    assert len(call_kwargs) == 2
    topup_kwargs = call_kwargs[1]

    covered_words = _ALL_WORDS[:80]     # delivered beats 0-7's own territory
    uncovered_words = _ALL_WORDS[80:]   # what the topup call should receive

    topup_segment_text = topup_kwargs["segment_text"]
    for w in covered_words:
        assert w not in topup_segment_text.split(), (
            f"already-covered word {w!r} leaked into the topup segment_text"
        )
    for w in uncovered_words:
        assert w in topup_segment_text.split(), (
            f"uncovered word {w!r} missing from the topup segment_text"
        )
    assert topup_segment_text == " ".join(uncovered_words)

    # Target beat count derived from the span's own word count, not the raw
    # (sub_target_beat_count - delivered) shortfall.
    assert topup_kwargs["target_beat_count"] == 6

    # The one-line deterministic continuation framing is present and never
    # empty for a real topup call.
    assert topup_kwargs["continuation_framing"] == storyboard._TOPUP_CONTINUATION_FRAMING
    assert topup_kwargs["continuation_framing"]

    # The original (first) call got the FULL segment, unaffected.
    assert call_kwargs[0]["segment_text"] == " ".join(_ALL_WORDS)
    assert call_kwargs[0].get("continuation_framing", "") == ""


def test_thirteen_of_fourteen_needs_no_topup():
    calls, result, call_kwargs = _run([13])
    assert calls == [14]
    assert len(result) == 13
    assert call_kwargs[0].get("continuation_framing", "") == ""


def test_under_delivering_topup_is_accepted_without_a_second_topup(caplog):
    with caplog.at_level(logging.WARNING, logger=storyboard.logger.name):
        calls, result, _ = _run([8, 2])
    assert calls == [14, 6]
    assert len(result) == 10
    assert "STORYBOARD_SHORTFALL_ACCEPTED" in caplog.text
    assert "STORYBOARD_SHORTFALL_TOPUP " in caplog.text  # span-scoped call was attempted
    # The duplicate filter is still active behind span-scoping — no
    # duplicates fire here because the topup beats are genuinely new
    # territory, but the mechanism itself must not have been removed.
    assert hasattr(storyboard, "_filter_duplicate_topup_beats")


def test_uncovered_span_too_thin_skips_the_topup_call_entirely(caplog):
    """When delivered beats already cover almost the whole segment (fewer,
    WIDER beats than requested — under the count floor, but not under a
    narration-coverage floor), a topup call has nothing real to add. Below
    storyboard._TOPUP_MIN_UNCOVERED_WORDS of uncovered narration, no second
    Claude call is made at all."""
    total_words = 140
    all_words = [_fill(i) for i in range(total_words)]
    voice_script = "[SECTION 1]\n" + " ".join(all_words)

    # 9 delivered beats (under the 10-beat/14-target shortfall floor), but
    # each covers ~15 words instead of the assumed 10 -> together they cover
    # words 0-134, leaving only 5 words uncovered (well under the 8-word
    # minimum) despite being short on BEAT COUNT.
    wide_beats = []
    cursor = 0
    for order in range(9):
        span = all_words[cursor:cursor + 15]
        hint = " ".join(span)
        wide_beats.append({
            "beat_order": order, "start_hint": hint, "end_hint": hint,
            "visual_intent": f"action {order}", "visual_type": "b-roll",
            "visual_category": "place", "environment": "other",
            "flux_prompt": f"prompt {order}", "effect": "slow_zoom",
            "color_grade": "neutral", "transition_to_next": "cut",
            "motif": "other", "beat_intensity": "medium",
            "suggested_duration_sec": 4.0,
        })
        cursor += 15
    assert total_words - cursor == 5  # confirms the thin-tail setup above

    calls: list[int] = []

    def fake_batch(**kwargs):
        calls.append(kwargs["target_beat_count"])
        return (
            {"overall_style": "test", "beats": wide_beats},
            {"output_tokens": 90},
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
            voice_script=voice_script,
            duration_ms=56_000,
            channel=SimpleNamespace(niche="any", tone="any"),
            script_format="youtube_long",
            whisper_transcript=[{"word": "word", "start": 0.0, "end": 0.1}],
            allow_legacy_fallback=True,
        )

    # Only ONE Claude call was ever made — the topup call is skipped, not
    # attempted-and-rejected.
    assert calls == [14]
    assert len(result) == 9
    assert "STORYBOARD_SHORTFALL_TOPUP_SKIPPED" in caplog.text
    assert "reason=uncovered_span_too_thin" in caplog.text
    assert "STORYBOARD_SHORTFALL_TOPUP " not in caplog.text
