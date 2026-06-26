"""Phase 10A-0 Fix 3 — runtime proof that validate_script_globally()'s result is
persisted to ContentValidation and wired into the existing rewrite mechanism.

Zero live API calls — stubs only `assess_script_quality`, `validate_script_globally`,
and `rewrite_script_for_quality` (the three Claude-calling functions
`run_script_quality_gate()` invokes). Everything else — `run_script_quality_gate()`
itself, `_run_global_script_validation()`, `_collect_quality_gate_issues()`, the
ContentValidation read/write, the rewrite cap — is real, unmodified code, run
against a real local dev DB fixture.

Proves, per the Phase 10A-0 brief:
  1. A NEEDS_FIX result with a fact-repetition issue reaches the rewrite call's
     issue list (captured directly from the stubbed rewrite_script_for_quality
     call's actual arguments — not inferred).
  2. The result is queryable from ContentValidation after the run (real DB
     read-back, not just checking the in-memory object).
  3. The existing _MAX_QUALITY_REWRITES cap is respected even with two issue
     sources (Claude quality-gate issues + global-validation issues) feeding
     the same rewrite loop — no second, independent retry counter.

Run: python scripts/smoke_phase10a0_global_validation_wiring_runtime_proof.py
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]" + (f" — {detail}" if detail else ""))


import app.agents.agent2_discovery.services.scripts as scripts_mod
from app.agents.agent2_discovery.services.scripts import run_script_quality_gate, _MAX_QUALITY_REWRITES
from app.database import _get_session_factory
from app.models import User, Channel, Content, ContentValidation

db = _get_session_factory()()

created_user_id = None
created_channel_id = None
created_content_id = None

orig_assess = scripts_mod.assess_script_quality
orig_global = scripts_mod.validate_script_globally
orig_rewrite = scripts_mod.rewrite_script_for_quality

rewrite_calls_seen: list[list[dict]] = []


def _stub_assess_script_quality(current, channel, script_format="youtube_long"):
    # No Claude-judged issues of its own — isolates this proof to the global-
    # validation issue source specifically, per the brief's Deliverable 1.
    return {"status": "PASSED", "issues": []}


def _stub_validate_script_globally(voice_script, blueprint):
    return {
        "status": "NEEDS_FIX",
        "issues": [
            {
                "section": "SECTION 2",
                "description": "Fact repeated: the mine's name is stated twice with no new detail",
                "suggestion": "Remove the second mention or attach a new consequence to it",
            }
        ],
    }


def _stub_rewrite_script_for_quality(current, issues, channel, script_format="youtube_long", tts_model="sonic-2", tts_provider="cartesia"):
    rewrite_calls_seen.append(issues)
    return {"title": current.get("title", "t"), "voice_script": current.get("voice_script", "") + " [rewritten]"}


try:
    # ── Minimal real fixtures ───────────────────────────────────────────────────
    user = User(id=uuid.uuid4(), name="smoke-10a0-user", telegram_chat_id=f"smoke-{uuid.uuid4()}", primary_language="en")
    db.add(user)
    db.flush()
    created_user_id = user.id

    channel = Channel(id=uuid.uuid4(), user_id=user.id, name="smoke-10a0-channel", niche="history", tone="documentary", active=False)
    db.add(channel)
    db.flush()
    created_channel_id = channel.id

    content = Content(
        id=uuid.uuid4(), channel_id=channel.id,
        source_url="https://example.invalid/smoke-10a0",
        source_language="en",
        content_hash=f"smoke-10a0-{uuid.uuid4()}",
        title="Smoke Phase 10A-0 parent",
        status="GENERATING_SCRIPTS",
    )
    db.add(content)
    db.flush()
    created_content_id = content.id

    validation = ContentValidation(id=uuid.uuid4(), content_id=content.id, status="APPROVED")
    db.add(validation)
    db.commit()

    blueprint = {"hook": "h", "central_question": "q", "major_turns": ["a", "b"], "final_payoff": "p", "comment_trigger": "c?"}
    scripts_in = {"title": "Draft title", "voice_script": "[INTRO]\nSome opening.\n[SECTION 1]\nBody.\n[OUTRO]\nEnd."}

    scripts_mod.assess_script_quality = _stub_assess_script_quality
    scripts_mod.validate_script_globally = _stub_validate_script_globally
    scripts_mod.rewrite_script_for_quality = _stub_rewrite_script_for_quality
    try:
        result = run_script_quality_gate(
            scripts_in, channel, content=content, db=db, blueprint=blueprint,
            script_format="youtube_long", language="en",
        )
    finally:
        scripts_mod.assess_script_quality = orig_assess
        scripts_mod.validate_script_globally = orig_global
        scripts_mod.rewrite_script_for_quality = orig_rewrite

    # ── 1. Fact-repetition issue reached the rewrite call's actual issue list ──
    assert_ok("rewrite_script_for_quality was called at least once", len(rewrite_calls_seen) >= 1)
    first_call_issues = rewrite_calls_seen[0]
    global_issue_descriptions = [
        i["description"] for i in first_call_issues if i.get("category") == "global_narrative"
    ]
    assert_ok(
        "the fact-repetition global-validation issue reached the rewrite call's issue list",
        any("Fact repeated" in d for d in global_issue_descriptions),
        f"issues passed to rewrite: {first_call_issues}",
    )

    # ── 2. Result is queryable from ContentValidation after the run (fresh read-back) ──
    db.commit()
    db.expire_all()
    reloaded = db.query(ContentValidation).filter(ContentValidation.content_id == content.id).first()
    assert_ok(
        "ContentValidation.script_validation_status persisted as AUTO_CORRECTED",
        reloaded.script_validation_status == "AUTO_CORRECTED",
        f"got {reloaded.script_validation_status!r}",
    )
    assert_ok(
        "ContentValidation.script_issues_log persisted with the raw global-validation issue",
        bool(reloaded.script_issues_log) and "Fact repeated" in reloaded.script_issues_log[0]["description"],
        f"got {reloaded.script_issues_log!r}",
    )

    # ── 3. Existing _MAX_QUALITY_REWRITES cap respected with two issue sources ──
    # _stub_assess_script_quality always returns PASSED/no issues, so after attempt 1
    # consumes the global issue, attempt 2 must find issue_group["global"] empty
    # (global issues are attempt-1-only) AND claude status PASSED -> gate returns
    # early on attempt 2, never reaching a 3rd attempt. Confirm exactly 1 rewrite call,
    # not _MAX_QUALITY_REWRITES calls, and confirm the cap constant itself is untouched.
    assert_ok(
        "exactly 1 rewrite call occurred (attempt 2 found nothing left to fix — no runaway loop)",
        len(rewrite_calls_seen) == 1,
        f"got {len(rewrite_calls_seen)} calls",
    )
    assert_ok("_MAX_QUALITY_REWRITES constant unchanged at 2 (no second counter introduced)", _MAX_QUALITY_REWRITES == 2)

    # ── Second scenario: force every attempt to keep failing, confirm the SAME single
    # cap (_MAX_QUALITY_REWRITES) bounds total rewrites even with both issue sources firing ──
    rewrite_calls_seen.clear()

    def _stub_assess_always_needs_rewrite(current, channel, script_format="youtube_long"):
        return {
            "status": "NEEDS_REWRITE",
            "issues": [{"severity": "HIGH", "category": "hook", "description": "weak hook", "fix": "sharpen it"}],
        }

    scripts_mod.assess_script_quality = _stub_assess_always_needs_rewrite
    scripts_mod.validate_script_globally = _stub_validate_script_globally
    scripts_mod.rewrite_script_for_quality = _stub_rewrite_script_for_quality
    try:
        run_script_quality_gate(
            scripts_in, channel, content=content, db=db, blueprint=blueprint,
            script_format="youtube_long", language="en",
        )
    finally:
        scripts_mod.assess_script_quality = orig_assess
        scripts_mod.validate_script_globally = orig_global
        scripts_mod.rewrite_script_for_quality = orig_rewrite

    assert_ok(
        f"with both issue sources persistently firing, total rewrite calls capped at _MAX_QUALITY_REWRITES ({_MAX_QUALITY_REWRITES})",
        len(rewrite_calls_seen) == _MAX_QUALITY_REWRITES,
        f"got {len(rewrite_calls_seen)} calls",
    )
    # Attempt 1's rewrite call must contain BOTH the Claude hook issue AND the global issue.
    assert_ok(
        "attempt 1's rewrite call merged BOTH issue sources (Claude quality-gate + global validation)",
        any(i.get("category") == "hook" for i in rewrite_calls_seen[0])
        and any(i.get("category") == "global_narrative" for i in rewrite_calls_seen[0]),
        f"attempt 1 issues: {rewrite_calls_seen[0]}",
    )
    # Attempt 2's rewrite call must NOT contain the global issue again (attempt-1-only).
    assert_ok(
        "attempt 2's rewrite call does NOT re-include the global-validation issue (fed once, not every attempt)",
        not any(i.get("category") == "global_narrative" for i in rewrite_calls_seen[1]),
        f"attempt 2 issues: {rewrite_calls_seen[1]}",
    )

finally:
    scripts_mod.assess_script_quality = orig_assess
    scripts_mod.validate_script_globally = orig_global
    scripts_mod.rewrite_script_for_quality = orig_rewrite
    if created_content_id is not None:
        db.query(ContentValidation).filter(ContentValidation.content_id == created_content_id).delete()
        db.query(Content).filter(Content.id == created_content_id).delete()
    if created_channel_id is not None:
        db.query(Channel).filter(Channel.id == created_channel_id).delete()
    if created_user_id is not None:
        db.query(User).filter(User.id == created_user_id).delete()
    db.commit()

    leftover_content = db.query(Content).filter(Content.id == created_content_id).count() if created_content_id else 0
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
