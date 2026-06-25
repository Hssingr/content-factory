"""Storyboard shape-coercion smoke test — zero API calls, zero DB access.

Verifies the fix for a real production failure: Claude returned the
``beats`` field as a JSON-encoded STRING (e.g. ``'[\\n  {"beat_order": 0, ...}\\n]'``)
instead of a native list, on an otherwise complete, non-truncated response.
This happened twice in a row (initial attempt + the one bounded shape retry),
so the old fail-loud-with-no-coercion behavior aborted the entire storyboard,
cascading the parent to FAILED and every child short to deferred.

``generate_storyboard_batch()``'s nested ``_check_shape()`` now attempts one
narrow coercion before failing: if a non-str field comes back as a string,
try ``json.loads()`` on it; if it parses into the expected type, substitute
it in place and proceed (log-only, not fail-loud). A string that fails to
parse, or parses to the wrong type, still fails exactly as before.

Verifies (all with call_claude_structured_with_usage stubbed, no live API):
  1. A 'beats' value returned as a valid JSON-encoded string is coerced to a
     list and the call succeeds WITHOUT needing the shape retry.
  2. The coerced beats list has the same length/content as the JSON-encoded
     string represented.
  3. A 'beats' value returned as a string that is NOT valid JSON still fails
     (after the one bounded shape retry) exactly as before — coercion does
     not mask a genuinely malformed response.
  4. A 'beats' value returned as a JSON string that parses to the WRONG type
     (e.g. a dict, not a list) still fails — coercion only accepts a parse
     into the exact expected type.
  5. global_notes (the other non-str required field) gets the same coercion
     treatment.
  6. A 'beats' value wrapped in a markdown code fence (```json ... ```) is
     recovered via the fence-stripping step.
  7. A 'beats' value followed by trailing non-JSON commentary after a
     complete, valid array is recovered via json.JSONDecoder.raw_decode
     (the trailing content is logged and discarded, not silently dropped).
  8. A genuinely malformed string (the production case: json.loads raises)
     logs STORYBOARD_SHAPE_COERCION_FAILED with the real parse-failure
     reason, not just a generic "wrong_type" message with no diagnosis.

Run: python scripts/smoke_storyboard_shape_coercion.py
Expected output: all lines prefixed with PASS, then SMOKE PASS
"""

import json
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


import app.agents.agent4_visuals.system_prompt as system_prompt

assert_ok("imports", True)

_GOOD_BEAT = {
    "beat_order": 0,
    "start_hint": "By the second morning the mother had",
    "end_hint": "had barely left her side",
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


class _FakeChannel:
    niche = "test niche"
    tone = "test tone"


def _run_with_stub(stub_fn):
    """Run generate_storyboard_batch with call_claude_structured_with_usage replaced by stub_fn."""
    orig = system_prompt.call_claude_structured_with_usage
    system_prompt.call_claude_structured_with_usage = stub_fn
    try:
        return system_prompt.generate_storyboard_batch(
            segment_label="[SECTION 2] (part 1/2)",
            segment_text="By the second morning the mother had barely left her side.",
            segment_index=3,
            segment_count=6,
            channel=_FakeChannel(),
            script_format="youtube_long",
            target_beat_count=11,
        )
    finally:
        system_prompt.call_claude_structured_with_usage = orig


# ── 1-2. beats returned as a valid JSON-encoded string → coerced, no retry ────

_call_count = {"n": 0}


def _stub_string_encoded_beats(**kwargs):
    _call_count["n"] += 1
    storyboard = {
        "storyboard_status": "APPROVED",
        "overall_style": "test style",
        "beats": json.dumps([_GOOD_BEAT]),  # string-encoded, the real-world quirk
        "global_notes": [],
    }
    usage = {"output_tokens": 100, "input_tokens": 50}
    return storyboard, usage


storyboard, usage, diag = _run_with_stub(_stub_string_encoded_beats)

assert_ok(
    "string-encoded beats is coerced to a list without raising",
    isinstance(storyboard["beats"], list),
    f"got type {type(storyboard['beats'])}",
)
assert_ok(
    "coerced beats list has the expected content",
    storyboard["beats"] == [_GOOD_BEAT],
    f"got {storyboard['beats']}",
)
assert_ok(
    "coercion succeeds on the FIRST attempt — no shape retry triggered",
    _call_count["n"] == 1,
    f"call count: {_call_count['n']}",
)

# ── 3. beats returned as a string that is NOT valid JSON → still fails ────────

_call_count2 = {"n": 0}


def _stub_invalid_string_beats(**kwargs):
    _call_count2["n"] += 1
    storyboard = {
        "storyboard_status": "APPROVED",
        "overall_style": "test style",
        "beats": "not valid json at all {{{",
        "global_notes": [],
    }
    usage = {"output_tokens": 100, "input_tokens": 50}
    return storyboard, usage


raised = False
try:
    _run_with_stub(_stub_invalid_string_beats)
except ValueError:
    raised = True

assert_ok(
    "non-JSON string for beats still raises ValueError (no false coercion)",
    raised,
)
assert_ok(
    "non-JSON string still triggers the one bounded shape retry before failing",
    _call_count2["n"] == 2,
    f"call count: {_call_count2['n']}",
)

# ── 4. beats parses to the WRONG type (dict, not list) → still fails ──────────

_call_count3 = {"n": 0}


def _stub_wrong_type_string_beats(**kwargs):
    _call_count3["n"] += 1
    storyboard = {
        "storyboard_status": "APPROVED",
        "overall_style": "test style",
        "beats": json.dumps({"not": "a list"}),
        "global_notes": [],
    }
    usage = {"output_tokens": 100, "input_tokens": 50}
    return storyboard, usage


raised_wrong_type = False
try:
    _run_with_stub(_stub_wrong_type_string_beats)
except ValueError:
    raised_wrong_type = True

assert_ok(
    "JSON string parsing to the wrong type (dict, not list) still raises",
    raised_wrong_type,
)

# ── 5. global_notes gets the same coercion treatment ───────────────────────────

_call_count4 = {"n": 0}


def _stub_string_encoded_global_notes(**kwargs):
    _call_count4["n"] += 1
    storyboard = {
        "storyboard_status": "APPROVED",
        "overall_style": "test style",
        "beats": [_GOOD_BEAT],
        "global_notes": json.dumps(["note one", "note two"]),
    }
    usage = {"output_tokens": 100, "input_tokens": 50}
    return storyboard, usage


storyboard4, usage4, diag4 = _run_with_stub(_stub_string_encoded_global_notes)

assert_ok(
    "global_notes string-encoded JSON is also coerced to a list",
    storyboard4["global_notes"] == ["note one", "note two"],
    f"got {storyboard4['global_notes']!r}",
)

# ── 6. beats wrapped in a markdown code fence → recovered ─────────────────────

_call_count5 = {"n": 0}


def _stub_fenced_beats(**kwargs):
    _call_count5["n"] += 1
    storyboard = {
        "storyboard_status": "APPROVED",
        "overall_style": "test style",
        "beats": "```json\n" + json.dumps([_GOOD_BEAT]) + "\n```",
        "global_notes": [],
    }
    usage = {"output_tokens": 100, "input_tokens": 50}
    return storyboard, usage


storyboard5, usage5, diag5 = _run_with_stub(_stub_fenced_beats)
assert_ok(
    "code-fence-wrapped beats is recovered to a list",
    storyboard5["beats"] == [_GOOD_BEAT],
    f"got {storyboard5['beats']!r}",
)
assert_ok(
    "code-fence recovery succeeds on the FIRST attempt — no shape retry",
    _call_count5["n"] == 1,
    f"call count: {_call_count5['n']}",
)

# ── 7. beats followed by trailing non-JSON commentary → recovered via raw_decode ──

_call_count6 = {"n": 0}


def _stub_trailing_garbage_beats(**kwargs):
    _call_count6["n"] += 1
    storyboard = {
        "storyboard_status": "APPROVED",
        "overall_style": "test style",
        "beats": json.dumps([_GOOD_BEAT]) + "\n\nNote: this covers the opening beat.",
        "global_notes": [],
    }
    usage = {"output_tokens": 100, "input_tokens": 50}
    return storyboard, usage


storyboard6, usage6, diag6 = _run_with_stub(_stub_trailing_garbage_beats)
assert_ok(
    "beats with trailing non-JSON commentary is recovered via raw_decode",
    storyboard6["beats"] == [_GOOD_BEAT],
    f"got {storyboard6['beats']!r}",
)
assert_ok(
    "trailing-garbage recovery succeeds on the FIRST attempt — no shape retry",
    _call_count6["n"] == 1,
    f"call count: {_call_count6['n']}",
)

# ── 8. genuinely malformed string → logs the real parse-failure reason ────────

import logging as _logging


class _CaptureHandler(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


_capture = _CaptureHandler()
system_prompt.logger.addHandler(_capture)
try:
    try:
        _run_with_stub(_stub_invalid_string_beats)
    except ValueError:
        pass
finally:
    system_prompt.logger.removeHandler(_capture)

coercion_failed_logs = [r for r in _capture.records if "STORYBOARD_SHAPE_COERCION_FAILED" in r]
assert_ok(
    "malformed string logs STORYBOARD_SHAPE_COERCION_FAILED with a real diagnosis "
    "(not a silent swallow of the parse error)",
    len(coercion_failed_logs) >= 1 and "reason=" in coercion_failed_logs[0],
    f"captured logs: {coercion_failed_logs}",
)

print("\nSMOKE PASS")
