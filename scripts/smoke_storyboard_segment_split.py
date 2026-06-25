"""Storyboard oversized-segment split smoke test — zero API calls, zero DB access.

Verifies the fix for a real production failure: [SECTION 3] had
target_beat_count=27, which truncated generate_storyboard_batch() at
max_tokens=8192 on both the first attempt and the in-call retry (reduced to
24 beats) — aborting the entire storyboard, cascading the parent to FAILED
and deferring every child short. _split_segment_for_batching() is the
preventive fix: a Python check, run BEFORE any Claude call, that splits an
oversized segment into multiple sub-calls.

Verifies:
  1. _split_segment_for_batching importable.
  2. A segment at/under _MAX_BEATS_PER_BATCH is returned unchanged (no split).
  3. A segment over _MAX_BEATS_PER_BATCH (target_beat_count=27, mirroring the
     real failure) is split into multiple sub-calls.
  4. Each sub-call's target_beat_count is <= _MAX_BEATS_PER_BATCH.
  5. Sub-call target_beat_counts sum back to (approximately) the original
     target_beat_count (proportional split, no beats invented or dropped).
  6. Concatenating all sub-texts in order, with single spaces collapsed,
     reproduces the original segment text verbatim (no words dropped/duplicated).
  7. Sub-labels are distinct and ordered (part 1/N, part 2/N, ...).
  8. Split snaps to a sentence boundary when one exists near the target split
     point, rather than cutting mid-sentence.
  9. split_into_beats() drives multiple generate_storyboard_batch() calls for
     one oversized segment (call-count proof, Claude call stubbed).

Run: python scripts/smoke_storyboard_segment_split.py
Expected output: all lines prefixed with PASS, then SMOKE PASS
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]")


# ── 1. Import check ───────────────────────────────────────────────────────────

from app.agents.agent4_visuals.subagents import storyboard as sb

assert_ok("imports", True)

# ── 2. Segment at/under the threshold is unchanged ────────────────────────────

small_text = "One short sentence here. Another one follows."
result = sb._split_segment_for_batching("[INTRO]", small_text, target_beat_count=5)
assert_ok(
    "under-threshold segment returned unchanged",
    result == [("[INTRO]", small_text, 5)],
    f"got {result}",
)

result_at_threshold = sb._split_segment_for_batching(
    "[INTRO]", small_text, target_beat_count=sb._MAX_BEATS_PER_BATCH
)
assert_ok(
    "at-threshold segment returned unchanged",
    len(result_at_threshold) == 1,
    f"got {result_at_threshold}",
)

# ── 3-7. Oversized segment (mirrors the real [SECTION 3] failure: target=27) ──

# Build a long, multi-sentence narration text so the split has real sentence
# boundaries to snap to, similar in shape to real narration.
sentences = [f"This is sentence number {i} in the long section." for i in range(1, 61)]
long_text = " ".join(sentences)
original_target = 27

parts = sb._split_segment_for_batching("[SECTION 3]", long_text, target_beat_count=original_target)

assert_ok(
    "oversized segment is split into more than one sub-call",
    len(parts) > 1,
    f"got {len(parts)} part(s)",
)

assert_ok(
    "every sub-call's target_beat_count is within the per-batch cap",
    all(sub_target <= sb._MAX_BEATS_PER_BATCH for _, _, sub_target in parts),
    f"sub-targets: {[t for _, _, t in parts]}",
)

summed_target = sum(sub_target for _, _, sub_target in parts)
assert_ok(
    "sub-call target_beat_counts sum back to ~the original target (proportional split)",
    abs(summed_target - original_target) <= len(parts),  # rounding tolerance
    f"summed={summed_target} original={original_target}",
)

reconstructed = " ".join(sub_text for _, sub_text, _ in parts)
normalize = lambda s: " ".join(s.split())
assert_ok(
    "concatenated sub-texts reproduce the original segment text verbatim",
    normalize(reconstructed) == normalize(long_text),
    f"reconstructed length={len(reconstructed)} original length={len(long_text)}",
)

labels = [label for label, _, _ in parts]
assert_ok(
    "sub-labels are distinct and carry part N/total markers",
    labels == [f"[SECTION 3] (part {i + 1}/{len(parts)})" for i in range(len(parts))],
    f"got {labels}",
)

# ── 8. Split snaps to a sentence boundary, not mid-sentence ───────────────────

for sub_text in parts[:-1]:  # every part except the last should end cleanly
    text = sub_text[1]
    assert_ok(
        f"sub-text ends at a sentence boundary: {text[-40:]!r}",
        text.rstrip().endswith("."),
        f"sub-text tail: {text[-40:]!r}",
    )

# ── 9. split_into_beats() drives multiple Claude calls for one oversized segment ──

import app.agents.agent4_visuals.subagents.storyboard as storyboard_module

_call_log: list[tuple[str, int]] = []


def _fake_generate_storyboard_batch(
    segment_label, segment_text, segment_index, segment_count, channel,
    script_format="youtube_long", previous_segment_summary="", target_beat_count=0,
    override_instructions="",
):
    _call_log.append((segment_label, target_beat_count))
    n_beats = max(target_beat_count, 1)
    beats = [
        {
            "beat_order": i,
            "start_hint": "placeholder start hint words here now",
            "end_hint": "placeholder end hint words here now",
            "visual_intent": "test",
            "visual_type": "b-roll",
            "visual_category": "place",
            "environment": "other",
            "flux_prompt": "test prompt",
            "effect": "slow_zoom",
            "color_grade": "neutral",
            "transition_to_next": "cut",
            "overlay_text": "",
            "overlay_position": "none",
            "motif": "other",
            "beat_intensity": "medium",
            "suggested_duration_sec": 3.0,
            "media_strategy": "flux_generated",
            "stock_queries": [],
            "fallback_flux_prompt": "",
            "text_card_style": "default",
        }
        for i in range(n_beats)
    ]
    storyboard = {
        "storyboard_status": "APPROVED",
        "overall_style": "test style",
        "beats": beats,
        "global_notes": [],
    }
    usage = {"output_tokens": 100, "input_tokens": 50}
    diag = {"was_truncated": False, "attempt_count": 1, "input_tokens": 50, "elapsed_ms": 1}
    return storyboard, usage, diag


_orig_generate = storyboard_module.generate_storyboard_batch
storyboard_module.generate_storyboard_batch = _fake_generate_storyboard_batch

try:
    voice_script = f"[SECTION 3]\n{long_text}\n"
    whisper_transcript = [
        {"word": w, "start": idx * 0.3, "end": idx * 0.3 + 0.25}
        for idx, w in enumerate(long_text.split())
    ]

    class _FakeChannel:
        niche = "test niche"
        tone = "test tone"

    mapped = sb.split_into_beats(
        voice_script=voice_script,
        duration_ms=int(len(long_text.split()) * 0.3 * 1000),
        channel=_FakeChannel(),
        script_format="youtube_long",
        whisper_transcript=whisper_transcript,
        allow_legacy_fallback=True,
        language="en",
    )
finally:
    storyboard_module.generate_storyboard_batch = _orig_generate

assert_ok(
    "split_into_beats() issued more than one Claude call for the oversized segment",
    len(_call_log) > 1,
    f"call log: {_call_log}",
)
assert_ok(
    "every recorded call's target_beat_count respects the per-batch cap",
    all(target <= sb._MAX_BEATS_PER_BATCH for _, target in _call_log),
    f"call log: {_call_log}",
)
assert_ok(
    "split_into_beats() returned a non-empty mapped result",
    mapped is not None and len(mapped) > 0,
    f"mapped={mapped!r}" if mapped is None else f"len={len(mapped)}",
)

print(f"\nsplit_into_beats() sub-call breakdown: {_call_log}")
print("\nSMOKE PASS")
