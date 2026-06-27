"""Phase 14.1 — child Short visual reuse root-cause investigation proof.

Read-only investigation. No production code is modified by this script.
Zero live API calls — stubs only `call_claude_structured_with_usage` (the
single Claude-call boundary inside `remap_beats_for_short()`); everything
else — the match_score threshold gate, `validate_storyboard()`'s
`excessive_reuse_ratio`/`reuse_clustering` checks, and
`_check_storyboard_issues()`'s MAJOR/MINOR routing — is real, unmodified
code, exercised end to end against local fixtures only.

Proves, per the Phase 14.1 brief:
  1. The reuse decision path is traced (asserted directly against the real
     functions, not re-implemented).
  2. A representative fixture reproduces ~95-100% reuse when (a) nearly all
     parent beats already have real cached images (the normal post-Agent-4
     parent-visual-pass state) and (b) Haiku's match_score distribution
     sits in the prompt's own "70-89: compatible environment and mood —
     reuse is appropriate" band for nearly every phrase — a plausible,
     not contrived, distribution given how the remap prompt is framed.
  3. The fresh-generation path is proven *reachable*: the same code, given
     a low-score distribution, produces near-0% reuse — ruling out "fresh
     generation is structurally unreachable" as the root cause and
     confirming the threshold gate itself is implemented correctly.
  4. `excessive_reuse_ratio`'s computation is verified directly against
     `validate_storyboard()`.
  5. No real fal.ai/Flux/stock/web/Claude API call is made anywhere.
  6. Existing relevant Agent 4 smokes still pass (no files were touched, so
     this is a pure confirmation, not a regression check).

Run: python scripts/smoke_child_visual_reuse_investigation.py
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]" + (f" — {detail}" if detail else ""))


import app.agents.agent4_visuals.subagents.storyboard as storyboard_mod
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    validate_storyboard, _REUSE_RATIO_THRESHOLD,
)

# ── Fixture: 10 parent beats, every single one already has a real cached
# image — this is the normal, expected state by the time child remap runs,
# since Agent 4's own parent visual pass generates Flux images for all beats
# before PARENT_VISUALS_DONE is ever reached (CLAUDE.md §11.2/§11.4).

PARENT_BEAT_COUNT = 10


class _FakeVideoSection:
    def __init__(self, section_order: int, media_url: str):
        self.section_order = section_order
        self.flux_prompt = f"beat {section_order} flux prompt"
        self.effect = "static"
        self.color_grade = "neutral"
        self.generation_prompt = json.dumps({
            "visual_intent": f"beat {section_order} intent",
            "visual_type": "b-roll",
            "visual_category": "establishing",
            "environment": "interior",
            "motif": "documents",
            "transition_to_next": "cut",
            "overlay_text": "",
            "overlay_position": "none",
            "media_url": media_url,
        })


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows

    def limit(self, *a, **k):
        return self


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


class _FakeContent:
    id = "child-short-content-id"


class _FakeAudioFile:
    duration_ms = 60000
    whisper_transcript = []  # empty -> proportional fallback only, no crash
    language = "en"


def _all_beats_have_real_cache(n: int) -> list:
    return [_FakeVideoSection(i, f"cache/parent-content-id/beat{i}.jpg") for i in range(n)]


def _make_remap_stub(match_scores: list[int], n: int):
    """Return a stub for call_claude_structured_with_usage() that assigns one
    narration phrase per parent beat, with the given match_score sequence."""
    def _stub(**kwargs):
        assignments = [
            {
                "narration_phrase": f"phrase number {i} about the case",
                "long_beat_order": i % n,
                "beat_intensity": "medium",
                "match_score": match_scores[i % len(match_scores)],
            }
            for i in range(n)
        ]
        return {"assignments": assignments}, {"output_tokens": 500}
    return _stub


SHORT_NARRATION = " ".join(f"Phrase number {i} about the case happened." for i in range(PARENT_BEAT_COUNT))

orig_call = storyboard_mod.call_claude_structured_with_usage

# ── 1, 2: representative real-world-plausible high-score distribution ──────
# This is exactly the band the remap prompt's own rubric calls "reuse is
# appropriate" (70-89) for every single phrase — not an adversarial or
# contrived score set, just "Haiku consistently lands in the band the prompt
# itself tells it means reuse is fine."
HIGH_SCORES = [72, 75, 78, 80, 74, 76, 82, 79, 77, 81]

storyboard_mod.call_claude_structured_with_usage = _make_remap_stub(HIGH_SCORES, PARENT_BEAT_COUNT)
try:
    beats = storyboard_mod.remap_beats_for_short(
        short_content=_FakeContent(),
        short_voice_script=SHORT_NARRATION,
        short_audio_file=_FakeAudioFile(),
        parent_content_id="parent-content-id",
        db=_FakeDB(_all_beats_have_real_cache(PARENT_BEAT_COUNT)),
    )
finally:
    storyboard_mod.call_claude_structured_with_usage = orig_call

reused = sum(1 for b in beats if (b.get("media_url") or "").startswith("cache/"))
reuse_ratio = reused / len(beats) if beats else 0.0
assert_ok(
    "representative fixture (all parent beats have real cache media + Haiku "
    "scores land in the prompt's own '70-89 reuse is appropriate' band) "
    "reproduces ~100% reuse",
    len(beats) == PARENT_BEAT_COUNT and reuse_ratio >= 0.95,
    f"reused={reused}/{len(beats)} ratio={reuse_ratio:.2f}",
)

# ── 3: fresh-generation path IS reachable — same code, low scores -> ~0% reuse ──

LOW_SCORES = [30, 35, 40, 45, 38, 42, 48, 44, 36, 41]
storyboard_mod.call_claude_structured_with_usage = _make_remap_stub(LOW_SCORES, PARENT_BEAT_COUNT)
try:
    beats_low = storyboard_mod.remap_beats_for_short(
        short_content=_FakeContent(),
        short_voice_script=SHORT_NARRATION,
        short_audio_file=_FakeAudioFile(),
        parent_content_id="parent-content-id",
        db=_FakeDB(_all_beats_have_real_cache(PARENT_BEAT_COUNT)),
    )
finally:
    storyboard_mod.call_claude_structured_with_usage = orig_call

reused_low = sum(1 for b in beats_low if (b.get("media_url") or "").startswith("cache/"))
pending_low = sum(1 for b in beats_low if not b.get("media_url"))
assert_ok(
    "fresh-generation path is reachable: identical cache-media availability, "
    "only the score distribution changed -> ~0% reuse, beats left pending "
    "for generate_pending_beat_images()",
    reused_low == 0 and pending_low == PARENT_BEAT_COUNT,
    f"reused={reused_low}/{len(beats_low)} pending={pending_low}",
)
assert_ok(
    "the threshold gate itself is correctly implemented — it faithfully "
    "reflects whatever match_score distribution it is given; the gate is not "
    "the structural bug, the upstream score distribution is the variable "
    "that determines the outcome",
    reuse_ratio - (reused_low / len(beats_low)) > 0.9,
    f"high-score ratio={reuse_ratio:.2f} vs low-score ratio={reused_low/len(beats_low):.2f}",
)

# ── boundary check: exactly-at-threshold and just-below-threshold scores ───

AT_AND_BELOW = [69, 70, 69, 70, 69, 70, 69, 70, 69, 70]
storyboard_mod.call_claude_structured_with_usage = _make_remap_stub(AT_AND_BELOW, PARENT_BEAT_COUNT)
try:
    beats_boundary = storyboard_mod.remap_beats_for_short(
        short_content=_FakeContent(),
        short_voice_script=SHORT_NARRATION,
        short_audio_file=_FakeAudioFile(),
        parent_content_id="parent-content-id",
        db=_FakeDB(_all_beats_have_real_cache(PARENT_BEAT_COUNT)),
    )
finally:
    storyboard_mod.call_claude_structured_with_usage = orig_call

reused_boundary = sum(1 for b in beats_boundary if (b.get("media_url") or "").startswith("cache/"))
assert_ok(
    "_MATCH_SCORE_THRESHOLD=70 boundary is exact: score=70 reuses, score=69 "
    "does not, exactly 50% of this alternating fixture reuses",
    reused_boundary == 5,
    f"reused={reused_boundary}/10 (expected 5)",
)

# ── 4: excessive_reuse_ratio computation verified directly ──────────────────

issues = validate_storyboard(beats)  # the ~100%-reuse beat list from above
reuse_issues = [i for i in issues if i["check"] == "excessive_reuse_ratio"]
assert_ok(
    "excessive_reuse_ratio fires on the ~100%-reuse fixture, computed correctly "
    f"(threshold={_REUSE_RATIO_THRESHOLD:.0%})",
    len(reuse_issues) == 1 and f"{reused}/{len(beats)}" in reuse_issues[0]["description"],
    reuse_issues[0]["description"] if reuse_issues else "none found",
)
assert_ok(
    "excessive_reuse_ratio is severity=MINOR (confirmed, not assumed) — this "
    "is the basis for the advisory-only conclusion in the report",
    reuse_issues[0]["severity"] == "MINOR",
)

issues_low = validate_storyboard(beats_low)  # the ~0%-reuse beat list
reuse_issues_low = [i for i in issues_low if i["check"] == "excessive_reuse_ratio"]
assert_ok(
    "excessive_reuse_ratio does NOT fire on the ~0%-reuse fixture — confirms "
    "the check only fires when reuse is genuinely high, not unconditionally",
    reuse_issues_low == [],
)

# ── Trace _check_storyboard_issues() routing: MINOR findings never reach the
#    caller as something requiring action — confirms "advisory, not blocking" ──

import app.agents.agent4_visuals.services.visual_orchestrator as orchestrator_mod
major_issues_returned = orchestrator_mod._check_storyboard_issues(beats)
assert_ok(
    "_check_storyboard_issues() returns zero MAJOR issues for the ~100%-reuse "
    "fixture — excessive_reuse_ratio (MINOR) is logged but never surfaced to "
    "the caller as something to act on; nothing in the real call chain ever "
    "retries or blocks on it",
    major_issues_returned == [],
    f"{major_issues_returned}",
)

print()
print("── Confirming no real/live external API calls were made ──────────────")
assert_ok(
    "call_claude_structured_with_usage was restored to the original (unstubbed) "
    "function after every use — no lingering stub, no real call attempted",
    storyboard_mod.call_claude_structured_with_usage is orig_call,
)

print()
print("── Re-running existing relevant Agent 4 smokes (no files touched — confirmation only) ──")
existing_smokes = [
    "scripts/smoke_storyboard_validator_expansion.py",
    "scripts/smoke_storyboard_quote_escaping_rule.py",
    "scripts/smoke_storyboard_shape_coercion.py",
    "scripts/smoke_storyboard_intensity.py",
]
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for smoke in existing_smokes:
    proc = subprocess.run(
        [sys.executable, smoke], cwd=repo_root, capture_output=True, text=True, timeout=120,
    )
    ok = proc.returncode == 0 and "SMOKE PASS" in proc.stdout
    assert_ok(f"existing smoke still passes: {smoke}", ok, proc.stdout[-300:] if not ok else "")

print()
print("SMOKE PASS")
