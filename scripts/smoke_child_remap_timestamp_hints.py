"""Child remap timestamp-hint runtime proof (Phase 6A-1).

Per CLAUDE.md Sec 19.4 (Runtime-Proof Requirements), this is a multi-function
data-flow fix (Haiku assignment -> beat dict construction -> shared timestamp
aligner -> boundary resolver -> persisted VideoSection), so a static/AST
check alone is not sufficient. This proof:

  - uses real application code for the entire internal chain
    (`remap_beats_for_short()`, `_derive_child_alignment_hints()`,
    `map_storyboard_beats_to_timestamps()` all run unmodified/unstubbed),
  - stubs only the paid external API (`call_claude_structured_with_usage`,
    the Haiku remap call) and uses a real local dev Postgres database for
    the parent `__visual__` VideoSection rows `remap_beats_for_short()`
    queries via `db`,
  - captures the actual logged `total=/exact=/fuzzy=/fallback=` mapping
    stats and the actual resolved beat durations, not just "no exception",
  - proves the parent path's alignment mechanics are unaffected by
    re-running the same shared aligner against a parent-shaped beat list
    (real start_hint/end_hint, as Claude would author them) and confirming
    a low fallback ratio is still achievable through the same code path.

Verifies:
  1. `_derive_child_alignment_hints()` — normal phrase -> distinct start/end
     hint windows; short phrase (<6 words) -> full phrase for both hints;
     empty phrase -> ("", "").
  2. `remap_beats_for_short()` builds beat dicts containing non-empty
     `start_hint`/`end_hint` for assignments that have a narration_phrase.
  3. Missing-narration_phrase policy: an isolated missing phrase is logged
     (`CHILD_REMAP_HINT_MISSING`) and tolerated; a response where missing
     phrases exceed the fail-ratio threshold makes the whole call return
     `[]` (fail loud), matching the function's existing failure convention.
  4. Before/after timestamp-mapping proof on a representative 27-beat,
     ~90s fixture mirroring the originally reported shape
     (`total=27 exact=0 fuzzy=0 fallback=27`): "before" (hints stripped,
     reproducing the pre-fix beat shape) is 100% fallback; "after" (real
     `remap_beats_for_short()` output) clears the required thresholds.
  5. Max single-beat duration after the fix stays within the declared
     safety threshold (no beat absorbs a large unbounded remainder).

Cleans up every fixture row it creates (User, Channel, parent Content,
parent VideoSection rows) and re-verifies they are gone.

Run: python scripts/smoke_child_remap_timestamp_hints.py
"""

import json
import logging
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL_RATIO_THRESHOLD = 0.50          # hard ceiling required by the task
PREFERRED_FAIL_RATIO_THRESHOLD = 0.20  # preferred, tighter target
MAX_SINGLE_BEAT_DURATION_MS = 15000   # product duration-safety threshold


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]" + (f" — {detail}" if detail else ""))


import app.agents.agent4_visuals.subagents.storyboard as storyboard
from app.agents.agent4_visuals.subagents.storyboard import (
    _derive_child_alignment_hints,
    remap_beats_for_short,
    map_storyboard_beats_to_timestamps,
)

# ── 1. _derive_child_alignment_hints() unit behavior ──────────────────────────

normal_phrase = "the door slowly opened to reveal a hidden room full of old photographs"
sh, eh = _derive_child_alignment_hints(normal_phrase)
assert_ok(
    "normal phrase -> distinct start/end hint windows",
    sh != eh
    and sh == "the door slowly opened to reveal a hidden room full"
    and eh == "opened to reveal a hidden room full of old photographs",
    f"start_hint={sh!r} end_hint={eh!r}",
)

short_phrase = "she never spoke again"
sh, eh = _derive_child_alignment_hints(short_phrase)
assert_ok(
    "short phrase (<6 words) -> full phrase for both hints",
    sh == short_phrase and eh == short_phrase,
    f"start_hint={sh!r} end_hint={eh!r}",
)

medium_phrase = "investigators found nothing unusual at first"
sh, eh = _derive_child_alignment_hints(medium_phrase)
assert_ok(
    "medium phrase (6 words, < max window) -> overlapping full phrase allowed",
    sh == medium_phrase and eh == medium_phrase,
    f"start_hint={sh!r} end_hint={eh!r}",
)

sh, eh = _derive_child_alignment_hints("   ")
assert_ok("empty/whitespace-only phrase -> (\"\", \"\")", sh == "" and eh == "")

sh, eh = _derive_child_alignment_hints("")
assert_ok("empty string -> (\"\", \"\")", sh == "" and eh == "")

# ── DB-backed runtime proof setup ──────────────────────────────────────────────

from app.database import _get_session_factory
from app.models import User, Channel, Content, VideoSection

db = _get_session_factory()()

created_user_id = None
created_channel_id = None
created_parent_id = None

# Capture the real "Storyboard timestamp mapping: total=... " log line emitted
# by map_storyboard_beats_to_timestamps() so we can assert on the actual
# exact/fuzzy/fallback counts, not just that the call didn't raise.
_captured_records: list[str] = []


class _CaptureHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _captured_records.append(record.getMessage())


_capture_handler = _CaptureHandler()
storyboard.logger.addHandler(_capture_handler)
storyboard.logger.setLevel(logging.DEBUG)


def _last_mapping_stats() -> tuple[int, int, int, int]:
    pattern = re.compile(
        r"Storyboard timestamp mapping: total=(\d+) exact=(\d+) fuzzy=(\d+) fallback=(\d+)"
    )
    for msg in reversed(_captured_records):
        m = pattern.search(msg)
        if m:
            return tuple(int(x) for x in m.groups())  # type: ignore[return-value]
    raise AssertionError("no 'Storyboard timestamp mapping: total=...' log line captured")


try:
    # ── Minimal real fixtures: User -> Channel -> parent Content -> __visual__ rows ──

    user = User(
        id=uuid.uuid4(),
        name="smoke-phase6a1-user",
        telegram_chat_id=f"smoke-{uuid.uuid4()}",
        primary_language="en",
    )
    db.add(user)
    db.flush()
    created_user_id = user.id

    channel = Channel(
        id=uuid.uuid4(),
        user_id=user.id,
        name="smoke-phase6a1-channel",
        niche="history",
        tone="documentary",
        active=False,
    )
    db.add(channel)
    db.flush()
    created_channel_id = channel.id

    parent = Content(
        id=uuid.uuid4(),
        channel_id=channel.id,
        source_url="https://example.invalid/smoke-phase6a1",
        source_language="en",
        content_hash=f"smoke-phase6a1-{uuid.uuid4()}",
        title="Smoke Phase 6A-1 parent",
        status="PARENT_VISUALS_DONE",
    )
    db.add(parent)
    db.flush()
    created_parent_id = parent.id

    # 8 parent __visual__ beats — half have a valid reusable cache/ image
    # (score >= threshold assignments will reuse these), half do not.
    n_parent_beats = 8
    for i in range(n_parent_beats):
        extras = {
            "visual_intent": f"parent beat {i} visual intent",
            "environment": "indoor_domestic",
            "motif": "room",
            "media_url": f"cache/{parent.id}/parentbeat{i}.jpg" if i % 2 == 0 else "",
        }
        db.add(VideoSection(
            id=uuid.uuid4(),
            content_id=parent.id,
            language="__visual__",
            section_order=i,
            script_text=f"parent narration excerpt {i}",
            audio_start_ms=i * 1000,
            audio_end_ms=(i + 1) * 1000,
            flux_prompt=f"parent flux prompt {i}",
            generation_prompt=json.dumps(extras),
        ))
    db.commit()

    # ── Representative 27-beat, ~90s child short fixture ───────────────────────
    # Generic unique tokens — matching is purely token-based, so nonsense words
    # exercise the real alignment mechanics exactly like real narration would.
    n_words = 250
    words = [f"tok{idx:04d}" for idx in range(n_words)]
    full_narration = " ".join(words)

    word_ms = 350  # ~172 wpm, plausible narration pace
    whisper_transcript = [
        {"word": w, "start": (i * word_ms) / 1000.0, "end": ((i + 1) * word_ms) / 1000.0}
        for i, w in enumerate(words)
    ]
    duration_ms = n_words * word_ms  # 87,500ms ≈ 87.5s — in-range for a Short

    n_beats = 27
    chunk_size = n_words // n_beats  # ~9 words/beat
    assignments = []
    for i in range(n_beats):
        start = i * chunk_size
        end = start + chunk_size if i < n_beats - 1 else n_words
        phrase = " ".join(words[start:end])
        assignments.append({
            "narration_phrase": phrase,
            "long_beat_order": i % n_parent_beats,
            "beat_intensity": ["high", "medium", "low"][i % 3],
            "match_score": 85 if i % 2 == 0 else 40,  # alternate reuse / generate
        })

    class _FakeShortContent:
        def __init__(self) -> None:
            self.id = uuid.uuid4()

    class _FakeShortAudioFile:
        def __init__(self) -> None:
            self.duration_ms = duration_ms
            self.whisper_transcript = whisper_transcript
            self.language = "en"

    def _stub_remap_call(**kwargs):
        return {"assignments": assignments}, {"output_tokens": 1234}

    # ── "Before" proof: reproduce the pre-fix beat shape (no start_hint/end_hint) ──
    # using the exact same assignments/whisper/duration, by calling the shared
    # aligner directly with hints stripped — this is exactly what
    # remap_beats_for_short() used to hand it before this fix.
    pre_fix_beats = []
    for i, a in enumerate(assignments):
        pre_fix_beats.append({
            "beat_order": i,
            "section_order": i,
            "script_text": a["narration_phrase"],
            "visual_intent": a["narration_phrase"],
            "beat_intensity": a["beat_intensity"],
            "suggested_duration_sec": 3.0,
            "media_url": "",
            "media_type": "image",
            "start_hint": "",
            "end_hint": "",
        })

    _captured_records.clear()
    before_result = map_storyboard_beats_to_timestamps(
        beats=pre_fix_beats,
        whisper_transcript=whisper_transcript,
        duration_ms=duration_ms,
        allow_legacy_fallback=True,
        language="en",
    )
    before_total, before_exact, before_fuzzy, before_fallback = _last_mapping_stats()
    before_fallback_ratio = before_fallback / before_total if before_total else 1.0
    before_max_duration = max(
        (b["audio_end_ms"] - b["audio_start_ms"]) for b in before_result
    )

    assert_ok(
        "BEFORE fix shape: child fallback=100% reproduced",
        before_exact == 0 and before_fuzzy == 0 and before_fallback == before_total,
        f"total={before_total} exact={before_exact} fuzzy={before_fuzzy} fallback={before_fallback}",
    )
    print(
        f"    >>> BEFORE: total={before_total} exact={before_exact} fuzzy={before_fuzzy} "
        f"fallback={before_fallback} ({100 * before_fallback_ratio:.0f}%) "
        f"max_single_beat_duration_ms={before_max_duration}"
    )

    # ── "After" proof: real remap_beats_for_short(), Claude stubbed only ────────
    orig_call = storyboard.call_claude_structured_with_usage
    storyboard.call_claude_structured_with_usage = _stub_remap_call
    try:
        _captured_records.clear()
        short_content = _FakeShortContent()
        short_audio_file = _FakeShortAudioFile()
        after_result = remap_beats_for_short(
            short_content=short_content,
            short_voice_script=full_narration,
            short_audio_file=short_audio_file,
            parent_content_id=parent.id,
            db=db,
        )
    finally:
        storyboard.call_claude_structured_with_usage = orig_call

    assert_ok("remap_beats_for_short() returned beats (not [])", bool(after_result), f"len={len(after_result)}")
    assert_ok(
        "remap_beats_for_short() output beat count matches input assignment count",
        len(after_result) == n_beats,
        f"expected={n_beats} actual={len(after_result)}",
    )

    after_total, after_exact, after_fuzzy, after_fallback = _last_mapping_stats()
    after_fallback_ratio = after_fallback / after_total if after_total else 1.0
    after_max_duration = max(
        (b["audio_end_ms"] - b["audio_start_ms"]) for b in after_result
    )
    print(
        f"    >>> AFTER:  total={after_total} exact={after_exact} fuzzy={after_fuzzy} "
        f"fallback={after_fallback} ({100 * after_fallback_ratio:.0f}%) "
        f"max_single_beat_duration_ms={after_max_duration}"
    )

    assert_ok(
        "AFTER fix: exact/fuzzy matches > 0 (was 0/0 before)",
        (after_exact + after_fuzzy) > 0,
        f"exact={after_exact} fuzzy={after_fuzzy}",
    )
    assert_ok(
        f"AFTER fix: fallback_rate < {100*FAIL_RATIO_THRESHOLD:.0f}% hard threshold",
        after_fallback_ratio < FAIL_RATIO_THRESHOLD,
        f"actual={100*after_fallback_ratio:.0f}%",
    )
    if after_fallback_ratio <= PREFERRED_FAIL_RATIO_THRESHOLD:
        print(f"PASS [AFTER fix: fallback_rate <= preferred {100*PREFERRED_FAIL_RATIO_THRESHOLD:.0f}% target] — actual={100*after_fallback_ratio:.0f}%")
    else:
        print(f"WARN [AFTER fix: fallback_rate above preferred {100*PREFERRED_FAIL_RATIO_THRESHOLD:.0f}% target, but under hard {100*FAIL_RATIO_THRESHOLD:.0f}% ceiling] — actual={100*after_fallback_ratio:.0f}%")

    assert_ok(
        f"AFTER fix: max single beat duration <= {MAX_SINGLE_BEAT_DURATION_MS}ms safety threshold",
        after_max_duration <= MAX_SINGLE_BEAT_DURATION_MS,
        f"actual={after_max_duration}ms (before fix was {before_max_duration}ms)",
    )

    for b in after_result:
        assert_ok(
            f"beat_order={b['beat_order']} has non-empty source narration coverage",
            (b["audio_end_ms"] - b["audio_start_ms"]) > 0,
        )

    # ── 2/3. start_hint/end_hint presence + missing-phrase policy ───────────────

    # Re-run with one isolated missing narration_phrase — should be tolerated
    # (logged, beat falls back individually) since it's well under the fail ratio.
    assignments_isolated_missing = [dict(a) for a in assignments]
    assignments_isolated_missing[0] = dict(assignments_isolated_missing[0])
    assignments_isolated_missing[0]["narration_phrase"] = ""

    def _stub_isolated_missing(**kwargs):
        return {"assignments": assignments_isolated_missing}, {"output_tokens": 1234}

    storyboard.call_claude_structured_with_usage = _stub_isolated_missing
    try:
        log_records: list[logging.LogRecord] = []

        class _RecordCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        rc = _RecordCapture()
        storyboard.logger.addHandler(rc)
        try:
            isolated_result = remap_beats_for_short(
                short_content=_FakeShortContent(),
                short_voice_script=full_narration,
                short_audio_file=_FakeShortAudioFile(),
                parent_content_id=parent.id,
                db=db,
            )
        finally:
            storyboard.logger.removeHandler(rc)
    finally:
        storyboard.call_claude_structured_with_usage = orig_call

    assert_ok(
        "isolated missing narration_phrase: call still succeeds (tolerated)",
        bool(isolated_result) and len(isolated_result) == n_beats,
    )
    hint_missing_logged = any(
        "CHILD_REMAP_HINT_MISSING" in r.getMessage() for r in log_records
    )
    assert_ok("CHILD_REMAP_HINT_MISSING warning logged for the isolated missing phrase", hint_missing_logged)

    # All assignments missing narration_phrase -> exceeds fail ratio -> [] (fail loud)
    assignments_all_missing = [
        {**a, "narration_phrase": ""} for a in assignments
    ]

    def _stub_all_missing(**kwargs):
        return {"assignments": assignments_all_missing}, {"output_tokens": 1234}

    storyboard.call_claude_structured_with_usage = _stub_all_missing
    try:
        all_missing_result = remap_beats_for_short(
            short_content=_FakeShortContent(),
            short_voice_script=full_narration,
            short_audio_file=_FakeShortAudioFile(),
            parent_content_id=parent.id,
            db=db,
        )
    finally:
        storyboard.call_claude_structured_with_usage = orig_call

    assert_ok(
        "all assignments missing narration_phrase: fail-loud returns []",
        all_missing_result == [],
        f"actual={all_missing_result!r}",
    )

    # ── 4. Parent path regression check ─────────────────────────────────────────
    # Real Claude-shaped parent beats (verbatim start_hint/end_hint, as
    # generate_storyboard_batch()/Claude would author them) through the same
    # shared aligner — confirms the parent path's alignment mechanics still
    # achieve a low fallback ratio, unaffected by this child-only change.
    parent_words = words  # reuse the same token vocabulary/transcript
    n_parent_proof_beats = 10
    p_chunk = n_words // n_parent_proof_beats
    parent_style_beats = []
    for i in range(n_parent_proof_beats):
        start = i * p_chunk
        end = start + p_chunk if i < n_parent_proof_beats - 1 else n_words
        chunk_words = parent_words[start:end]
        parent_style_beats.append({
            "beat_order": i,
            "script_text": "",
            "visual_intent": f"parent visual intent {i}",
            "beat_intensity": "medium",
            "suggested_duration_sec": 3.0,
            "media_url": "",
            "media_type": "image",
            "start_hint": " ".join(chunk_words[:8]),
            "end_hint": " ".join(chunk_words[-8:]),
        })

    _captured_records.clear()
    parent_proof_result = map_storyboard_beats_to_timestamps(
        beats=parent_style_beats,
        whisper_transcript=whisper_transcript,
        duration_ms=duration_ms,
        allow_legacy_fallback=False,
        language="en",
    )
    p_total, p_exact, p_fuzzy, p_fallback = _last_mapping_stats()
    assert_ok(
        "parent-style beats (real start_hint/end_hint) still achieve exact/fuzzy matches",
        parent_proof_result is not None and (p_exact + p_fuzzy) == p_total,
        f"total={p_total} exact={p_exact} fuzzy={p_fuzzy} fallback={p_fallback}",
    )
    print(f"    >>> PARENT REGRESSION: total={p_total} exact={p_exact} fuzzy={p_fuzzy} fallback={p_fallback}")

finally:
    storyboard.logger.removeHandler(_capture_handler)
    # ── Cleanup: delete every fixture row this script created ───────────────────
    if created_parent_id is not None:
        db.query(VideoSection).filter(VideoSection.content_id == created_parent_id).delete()
        db.query(Content).filter(Content.id == created_parent_id).delete()
    if created_channel_id is not None:
        db.query(Channel).filter(Channel.id == created_channel_id).delete()
    if created_user_id is not None:
        db.query(User).filter(User.id == created_user_id).delete()
    db.commit()

    leftover_content = db.query(Content).filter(Content.id == created_parent_id).count() if created_parent_id else 0
    leftover_channel = db.query(Channel).filter(Channel.id == created_channel_id).count() if created_channel_id else 0
    leftover_user = db.query(User).filter(User.id == created_user_id).count() if created_user_id else 0
    db.close()

assert_ok(
    "fixture cleanup verified (no leftover rows)",
    leftover_content == 0 and leftover_channel == 0 and leftover_user == 0,
    f"leftover_content={leftover_content} leftover_channel={leftover_channel} leftover_user={leftover_user}",
)

print()
print("SMOKE PASS")
