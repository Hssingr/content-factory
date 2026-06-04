#!/usr/bin/env python3
"""
Full Agent 2 → Agent 3 end-to-end test script.

Steps:
  1. Discovery    — Claude browses your sources, picks the best story
  2. Scripts      — Claude generates video + voice scripts in source language
  3. Telegram     — sends summary to your phone, waits for APPROVE / CHANGE
  4. Multilingual — generates culturally adapted scripts for every channel language
  5. Agent 3      — validates all scripts, auto-corrects MAJOR issues
  6. Summary      — prints final DB state

Usage:
    source venv/bin/activate

    # Full run (discovery → Agent 3):
    python test_full_pipeline.py [channel_id]

    # Skip steps 1 & 2 — use an existing content_id (already has scripts in DB):
    python test_full_pipeline.py --from-content <content_id>

If no channel_id is given, uses the first active channel found.
"""

import logging
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(name)s — %(message)s",
)

SEP  = "=" * 62
STEP = lambda title: f"\n── {title} " + "─" * max(0, 58 - len(title))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db():
    from app.database import _get_session_factory
    return _get_session_factory()()


def _tg_send(bot_token: str, chat_id: str, text: str) -> str | None:
    """Send a Telegram message synchronously. Returns message_id or None."""
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        resp.raise_for_status()
        return str(resp.json()["result"]["message_id"])
    except Exception as exc:
        print(f"  ❌ Telegram send failed: {exc}")
        return None


def poll_telegram(bot_token: str, expected_reply_to_id: str, timeout_sec: int = 300) -> tuple[str, str]:
    """
    Long-poll Telegram getUpdates until the user replies to our specific message.
    Returns (reply_text, username).
    Falls back to "APPROVE" on timeout.
    """
    base   = f"https://api.telegram.org/bot{bot_token}"
    offset = 0
    deadline = time.time() + timeout_sec

    print(f"\n  Waiting for reply to message_id={expected_reply_to_id} (timeout {timeout_sec}s)")
    print("  → Reply APPROVE in Telegram to proceed, or describe the change you want.\n")

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        poll_secs  = min(20, remaining)
        if poll_secs <= 0:
            break
        try:
            resp = httpx.get(
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": poll_secs, "allowed_updates": ["message"]},
                timeout=poll_secs + 5,
            )
            updates = resp.json().get("result", [])
        except Exception as exc:
            print(f"  Poll error: {exc} — retrying in 3s…")
            time.sleep(3)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg        = upd.get("message", {})
            reply_to   = msg.get("reply_to_message", {})
            reply_id   = str(reply_to.get("message_id", ""))
            if reply_id == str(expected_reply_to_id):
                text       = (msg.get("text") or "").strip()
                username   = msg.get("from", {}).get("username", "user")
                print(f"  📩 @{username} replied: {text!r}")
                return text, username

    print("  ⏱ Timeout reached — auto-approving")
    return "APPROVE", "timeout"


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(channel_id_str: str | None = None, from_content_id_str: str | None = None) -> None:  
    from app.config import settings
    from app.models import Channel, ChannelConfig, ChannelSource, Content, ContentValidation, Script
    from app.agents.agent2_discovery.services.discovery import run_discovery
    from app.agents.agent2_discovery.services.scripts import generate_multilingual_scripts
    from app.agents.agent2_discovery.services.validation import send_for_validation, _handle_change
    from app.agents.agent2_discovery.system_prompt import generate_scripts
    from app.agents.agent3_validation.services.validation import run_validation
    from app.agents.agent3_validation.services.estimator import estimate_duration_sec

    print(SEP)
    print("  FULL PIPELINE TEST  —  Agent 2 → Agent 3")
    print(SEP)

    # ── Find channel / existing content ───────────────────────────────────────
    db = _db()

    content_id = None
    scripts = None
    story = None
    skip_discovery_and_scripts = False

    if from_content_id_str:
        content_id = uuid.UUID(from_content_id_str)
        content = db.get(Content, content_id)

        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return

        channel = db.get(Channel, content.channel_id)

        existing_script = (
            db.query(Script)
            .filter(Script.content_id == content_id)
            .order_by(Script.version.desc())
            .first()
        )

        if not existing_script:
            print(f"\nERROR: Content {content_id} has no scripts in DB.")
            db.close()
            return

        scripts = {
            "title": content.title,
            "video_script": existing_script.video_script,
            "voice_script": existing_script.voice_script,
        }

        channel_id = channel.id
        skip_discovery_and_scripts = True

    elif channel_id_str:
        channel = db.get(Channel, uuid.UUID(channel_id_str))
    else:
        channel = db.query(Channel).filter(Channel.active.is_(True)).first()

    if not channel:
        print("\nERROR: No active channel found. Activate one via the UI first.")
        db.close()
        return

    config = db.get(ChannelConfig, channel.id)
    sources = db.query(ChannelSource).filter(ChannelSource.channel_id == channel.id).all()
    max_rev = config.validation_max_revisions if config else 3

    print(f"\n  Channel : {channel.name}")
    print(f"  Niche   : {channel.niche}")
    print(f"  Tone    : {channel.tone}")
    print(f"  Sources : {[(s.source_type, s.source_value[:50]) for s in sources]}")

    channel_id = channel.id
    db.close()

    if not skip_discovery_and_scripts:
        # ── STEP 1: Discovery ────────────────────────────────────────────────
        print(STEP("STEP 1: Discovery — Claude browsing sources (30-60s)"))

        db = _db()
        result = run_discovery(channel_id, db)
        db.close()

        if not result:
            print("  ❌ No story found.")
            print("     Check: is the channel active? Are sources configured (Tab 1 Section 5)?")
            return

        content, story = result
        content_id = content.id

        print(f"  Story   : {story.title[:70]}")
        print(f"  URL     : {story.url}")
        print(f"  Language: {story.language}")
        print(f"  Body    : {len(story.body.split())} words")

        # ── STEP 2: Generate source-language scripts ─────────────────────────
        print(STEP("STEP 2: Generating scripts (Claude)"))

        db = _db()
        channel = db.get(Channel, channel_id)
        scripts = generate_scripts(story, channel)

        content = db.get(Content, content_id)
        content.title = scripts.get("title", content.title)

        db.add(Script(
            content_id=content_id,
            language=story.language,
            video_script=scripts["video_script"],
            voice_script=scripts["voice_script"],
            version=1,
            validated=False,
        ))
        db.commit()
        db.close()

        wc = len(scripts["voice_script"].split())
        dur = estimate_duration_sec(scripts["voice_script"], story.language)
        sec_count = scripts["video_script"].count("[SECTION")

        print(f"  Title   : {scripts['title'][:70]}")
        print(f"  Voice   : {wc} words → ~{dur:.0f}s ({dur / 60:.1f} min)")
        print(f"  Sections: {sec_count}")

    else:
        print(STEP("SKIP STEP 1 & 2: Using existing content/scripts"))

        wc = len(scripts["voice_script"].split())
        sec_count = scripts["video_script"].count("[SECTION")

        print(f"  Content : {content_id}")
        print(f"  Title   : {scripts['title'][:70] if scripts.get('title') else '(no title)'}")
        print(f"  Voice   : {wc} words")
        print(f"  Sections: {sec_count}")

    # ── STEP 3: Telegram validation loop ──────────────────────────────────────
    print(STEP("STEP 3: Sending to Telegram for approval"))

    db = _db()
    content = db.get(Content, content_id)
    channel = db.get(Channel, channel_id)
    send_for_validation(content, channel, scripts, db)

    val = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
    msg_id = val.telegram_message_id if val else None
    db.close()

    if not msg_id:
        print("  ❌ Telegram send failed — check TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
        return

    print(f"  ✅ Sent (message_id={msg_id})")

    # Validation loop — handle APPROVE or CHANGE
    for attempt in range(1, max_rev + 2):
        decision, from_user = poll_telegram(settings.telegram_bot_token, msg_id, timeout_sec=300)

        if decision.upper().startswith("APPROVE") or from_user == "timeout":
            db = _db()
            content = db.get(Content, content_id)
            val = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
            if val:
                val.status     = "APPROVED"
                val.approved_at = datetime.now(timezone.utc)
            content.status = "APPROVED"
            db.commit()
            db.close()
            reason = "approved by @" + from_user if from_user != "timeout" else "auto-approved (timeout)"
            print(f"  ✅ Content {reason}")
            break

        # CHANGE request
        print(f"  CHANGE ({attempt}/{max_rev}): {decision!r} — regenerating…")

        db = _db()
        content = db.get(Content, content_id)
        channel = db.get(Channel, channel_id)
        val = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()

        pending = _handle_change(val, content, channel, decision, db)

        if pending:
            chat_id, new_message = pending
            new_msg_id = _tg_send(settings.telegram_bot_token, chat_id, new_message)
            if new_msg_id:
                val.telegram_message_id = new_msg_id
                db.commit()
                msg_id = new_msg_id
                print(f"  ✅ Revised script sent (message_id={msg_id})")
        db.close()

        if attempt >= max_rev:
            print(f"  ⚠ Max revisions ({max_rev}) reached — auto-approving")
            db = _db()
            content = db.get(Content, content_id)
            content.status = "APPROVED"
            db.commit()
            db.close()

    # ── STEP 4: Multilingual scripts ──────────────────────────────────────────
    print(STEP("STEP 4: Generating multilingual scripts"))

    db = _db()
    content = db.get(Content, content_id)
    channel = db.get(Channel, channel_id)
    lang_scripts = generate_multilingual_scripts(content, channel, db)
    db.close()

    print(f"  Languages: {[s.language for s in lang_scripts]}")

    # ── STEP 5: Agent 3 validation ────────────────────────────────────────────
    print(STEP("STEP 5: Agent 3 — Script validation"))

    db = _db()
    content = db.get(Content, content_id)
    if content.status != "SCRIPTS_READY":
        content.status = "SCRIPTS_READY"
        db.commit()
    db.close()

    db = _db()
    passed = run_validation(content_id, db)
    db.close()

    # ── STEP 6: Final summary ─────────────────────────────────────────────────
    print(STEP("STEP 6: Final state"))

    db = _db()
    content = db.get(Content, content_id)
    val     = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
    scripts_all = (
        db.query(Script)
        .filter(Script.content_id == content_id)
        .order_by(Script.language, Script.version.desc())
        .all()
    )
    db.close()

    print(f"\n  Title    : {content.title[:65]}")
    print(f"  Status   : {content.status}")
    if val:
        issues = [i for i in (val.script_issues_log or []) if isinstance(i, dict) and "severity" in i]
        print(f"  Validation : {val.script_validation_status} | corrections={val.self_correction_attempts} | issues_logged={len(issues)}")

    print(f"\n  Scripts:")
    seen: set[str] = set()
    for s in scripts_all:
        if s.language in seen:
            continue
        seen.add(s.language)
        wc  = len(s.voice_script.split())
        dur = s.estimated_duration_sec or estimate_duration_sec(s.voice_script, s.language)
        bp  = len(s.shorts_breakpoints or [])
        print(
            f"    [{s.language}] v{s.version}  validated={s.validated}"
            f"  {wc}w  {dur:.0f}s ({dur / 60:.1f}min)  {bp} breakpoints"
        )

    print()
    print(SEP)
    if passed:
        print(f"  ✅  COMPLETE — content is {content.status} → ready for Agent 4")
    else:
        print(f"  ⚠️   BLOCKED — MAJOR issues remain → check Telegram for PROCEED/REVALIDATE")
    print(SEP)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Full Agent 2 → Agent 3 end-to-end test script."
    )

    parser.add_argument(
        "channel_id",
        nargs="?",
        help="Optional channel UUID. If omitted, first active channel is used.",
    )

    parser.add_argument(
        "--from-content",
        dest="from_content_id",
        help="Skip discovery and source script generation, using an existing content UUID.",
    )

    args = parser.parse_args()

    run(
        channel_id_str=args.channel_id,
        from_content_id_str=args.from_content_id,
    )