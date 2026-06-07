#!/usr/bin/env python3
"""
Full Agent 2 → Agent 3 → Agent 4 → Agent 5 end-to-end test script.

Steps:
  1. Discovery    — Claude browses your sources, picks the best story
  2. Scripts      — Claude generates video + voice scripts in source language
  3. Telegram     — sends summary to your phone, waits for APPROVE / CHANGE
  4. Multilingual — generates culturally adapted scripts for every channel language
  5. Agent 3      — validates all scripts, auto-corrects MAJOR issues
  6. Agent 4      — ElevenLabs TTS + Whisper transcription + breakpoints
  7. Agent 5      — Section Splitter → Validator → Stock Fetch → Assembly Validator
                    → Shorts Cutter → Subtitles → Remotion render
  8. Summary      — prints final DB state (scripts + audio + video renders)

Usage:
    source venv/bin/activate

    # Full run (discovery → Agent 5):
    python test_full_pipeline.py [channel_id]

    # Skip discovery — use an existing content_id (has source scripts, no Telegram yet):
    python test_full_pipeline.py --from-content <content_id>

    # Skip Telegram — content already approved, restart multilingual generation:
    python test_full_pipeline.py --from-multilingual <content_id>

    # Skip multilingual generation — jump straight to Agent 3:
    python test_full_pipeline.py --from-agent3 <content_id>

    # Jump directly to Agent 4 — content already has SCRIPTS_VALIDATED status:
    python test_full_pipeline.py --from-audio <content_id>

    # Jump directly to Agent 5 — content already has AUDIO_DONE status:
    python test_full_pipeline.py --from-video <content_id>

If no channel_id is given, uses the first active channel found.
"""

import logging
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


# ── DB helper ────────────────────────────────────────────────────────────────

def _db():
    from app.database import _get_session_factory
    return _get_session_factory()()


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _tg_send(bot_token: str, chat_id: str, text: str) -> str | None:
    """Send a Telegram message. Returns message_id str or None on failure."""
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
    Returns (reply_text, username).  Falls back to "APPROVE" on timeout.
    """
    base     = f"https://api.telegram.org/bot{bot_token}"
    offset   = 0
    deadline = time.time() + timeout_sec

    print(f"\n  Waiting for reply to message_id={expected_reply_to_id} (timeout {timeout_sec}s)")
    print("  → Reply APPROVE in Telegram to proceed, or describe the change you want.\n")

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        poll_secs = min(20, remaining)
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
            offset   = upd["update_id"] + 1
            msg      = upd.get("message", {})
            reply_to = msg.get("reply_to_message", {})
            reply_id = str(reply_to.get("message_id", ""))
            if reply_id == str(expected_reply_to_id):
                text     = (msg.get("text") or "").strip()
                username = msg.get("from", {}).get("username", "user")
                print(f"  📩 @{username} replied: {text!r}")
                return text, username

    print("  ⏱ Timeout reached — auto-approving")
    return "APPROVE", "timeout"


# ── Summary helpers ───────────────────────────────────────────────────────────

def _print_audio_summary(content_id: uuid.UUID, audio_ok: bool) -> None:
    from app.models import AudioFile
    db = _db()
    audio_files = (
        db.query(AudioFile)
        .filter(AudioFile.content_id == content_id)
        .all()
    )
    db.close()

    if not audio_files:
        if audio_ok:
            print("  ⚠ No AudioFile records found despite reported success")
        return

    print(f"\n  Audio files ({len(audio_files)} language(s)):")
    for af in sorted(audio_files, key=lambda a: a.language):
        bp  = len(af.shorts_breakpoints or [])
        wc  = len(af.whisper_transcript or [])
        dur = af.duration_ms / 1000
        print(
            f"    [{af.language}]  {dur:.1f}s ({dur / 60:.1f}min)"
            f"  {bp} breakpoints  {wc} Whisper words"
        )
        if af.whisper_transcript:
            sample = af.whisper_transcript[:3]
            print(f"             Whisper sample: {sample}")


def _print_video_summary(content_id: uuid.UUID, video_ok: bool) -> None:
    from app.models import VideoRender, VideoSection
    db = _db()
    renders = (
        db.query(VideoRender)
        .filter(VideoRender.content_id == content_id)
        .order_by(VideoRender.language, VideoRender.format, VideoRender.short_order)
        .all()
    )
    sections = (
        db.query(VideoSection)
        .filter(VideoSection.content_id == content_id)
        .all()
    )
    db.close()

    if not renders:
        if video_ok:
            print("  ⚠ No VideoRender records found despite reported success")
        return

    langs       = sorted({r.language for r in renders})
    sec_by_lang = {}
    for s in sections:
        sec_by_lang.setdefault(s.language, []).append(s)

    print(f"\n  Video renders ({len(renders)} total across {len(langs)} language(s)):")
    for lang in langs:
        lang_renders  = [r for r in renders if r.language == lang]
        lang_sections = sec_by_lang.get(lang, [])
        main_r   = next((r for r in lang_renders if r.format == "main"), None)
        short_rs = sorted(
            [r for r in lang_renders if r.format == "short"],
            key=lambda r: r.short_order or 0,
        )
        print(f"\n    [{lang}]  {len(lang_sections)} section(s)")
        if main_r:
            rt = f"{main_r.render_time_seconds:.0f}s render" if main_r.render_time_seconds else ""
            print(f"      Main   16:9   {main_r.duration_seconds:.1f}s  {rt}")
        for sr in short_rs:
            rt = f"{sr.render_time_seconds:.0f}s render" if sr.render_time_seconds else ""
            print(
                f"      Short  9:16   part={sr.short_order}  {sr.duration_seconds:.1f}s"
                f"  hook={sr.hook_modified}  {rt}"
            )


def _print_final_summary(
    content_id: uuid.UUID,
    audio_ok: bool,
    video_ok: bool,
    passed: bool,
    multilingual_ok: bool,
) -> None:
    from app.models import Content, ContentValidation, Script
    from app.agents.agent3_validation.services.estimator import estimate_duration_sec

    db = _db()
    content     = db.get(Content, content_id)
    val         = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
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
        print(
            f"  Validation: {val.script_validation_status}"
            f" | corrections={val.self_correction_attempts}"
            f" | issues_logged={len(issues)}"
        )

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

    _print_audio_summary(content_id, audio_ok)
    _print_video_summary(content_id, video_ok)

    print()
    print(SEP)
    if video_ok:
        print(f"  ✅  COMPLETE — content is {content.status} → ready for Agent 6")
    elif audio_ok:
        print(f"  ❌  AGENT 5 FAILED — check Remotion setup (Node.js + npx in PATH)")
        print(f"      Re-run:  python test_full_pipeline.py --from-video {content_id}")
    elif passed:
        print(f"  ❌  AGENT 4 FAILED — check ELEVENLABS_API_KEY and OPENAI_API_KEY in .env")
        print(f"      Re-run:  python test_full_pipeline.py --from-audio {content_id}")
    elif multilingual_ok:
        print(f"  ⚠   AGENT 3 BLOCKED — MAJOR issues remain after max auto-corrections")
        print(f"      Review scripts in DB, then re-run:")
        print(f"      python test_full_pipeline.py --from-agent3 {content_id}")
    else:
        print(f"  ❌  MULTILINGUAL GENERATION FAILED")
        print(f"      Re-run:  python test_full_pipeline.py --from-multilingual {content_id}")
    print(SEP)


# ── Step runners (reused by multiple entry points) ────────────────────────────

def _run_agent3(content_id: uuid.UUID) -> bool:
    """Run Agent 3 script validation. Returns True if scripts passed."""
    from app.models import Content
    from app.agents.agent3_validation.services.validation import run_validation

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
    return passed


def _run_agent4(content_id: uuid.UUID) -> bool:
    """Run Agent 4 TTS + Whisper. Returns True if at least one language succeeded."""
    from app.models import Content
    from app.agents.agent4_audio.services.audio import run_audio_generation

    print(STEP("STEP 6: Agent 4 — ElevenLabs TTS + Whisper"))
    db = _db()
    content = db.get(Content, content_id)
    if content.status not in ("SCRIPTS_VALIDATED", "AUDIO_DONE"):
        content.status = "SCRIPTS_VALIDATED"
        db.commit()
    audio_ok = run_audio_generation(content_id, db)
    db.close()
    return audio_ok


def _run_agent5(content_id: uuid.UUID) -> bool:
    """Run Agent 5 Remotion video generation. Returns True on success."""
    from app.models import Content
    from app.agents.agent5_video.services.video import run_video_generation

    print(STEP("STEP 7: Agent 5 — Video generation (Remotion)"))
    db = _db()
    content = db.get(Content, content_id)
    if content.status not in ("AUDIO_DONE", "GENERATING_VIDEO"):
        content.status = "AUDIO_DONE"
        db.commit()
    video_ok = run_video_generation(content_id, db)
    db.close()
    return video_ok


# ── Entry-point: Content model not imported at top level ─────────────────────

def _load_content(content_id: uuid.UUID):
    from app.models import Content
    db = _db()
    content = db.get(Content, content_id)
    db.close()
    return content


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(
    channel_id_str: str | None = None,
    from_content_id_str: str | None = None,
    from_multilingual_id_str: str | None = None,
    from_agent3_id_str: str | None = None,
    from_audio_id_str: str | None = None,
    from_video_id_str: str | None = None,
) -> None:
    from app.config import settings
    from app.models import (
        Channel, ChannelConfig, ChannelSource, Content,
        ContentValidation, Script,
    )
    from app.agents.agent2_discovery.services.discovery import run_discovery
    from app.agents.agent2_discovery.services.scripts import generate_multilingual_scripts
    from app.agents.agent2_discovery.services.validation import send_for_validation, _handle_change
    from app.agents.agent2_discovery.system_prompt import generate_scripts
    from app.agents.agent3_validation.services.estimator import estimate_duration_sec

    print(SEP)
    print("  FULL PIPELINE TEST  —  Agent 2 → Agent 3 → Agent 4 → Agent 5")
    print(SEP)

    # ── --from-video: jump directly to Agent 5 ───────────────────────────────
    if from_video_id_str:
        content_id = uuid.UUID(from_video_id_str)
        content = _load_content(content_id)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            return

        print(f"\n  Jumping to Agent 5 for content {content_id}")
        print(f"  Status: {content.status}")

        video_ok = _run_agent5(content_id)
        _print_audio_summary(content_id, True)
        _print_video_summary(content_id, video_ok)
        print()
        print(SEP)
        if video_ok:
            print(f"  ✅  COMPLETE — ready for Agent 6")
        else:
            print(f"  ❌  AGENT 5 FAILED — check Remotion setup")
            print(f"      Re-run:  python test_full_pipeline.py --from-video {content_id}")
        print(SEP)
        return

    # ── --from-audio: jump directly to Agent 4 ───────────────────────────────
    if from_audio_id_str:
        content_id = uuid.UUID(from_audio_id_str)
        content = _load_content(content_id)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            return

        print(f"\n  Jumping to Agent 4 for content {content_id}")
        print(f"  Status: {content.status}")

        audio_ok = _run_agent4(content_id)
        _print_audio_summary(content_id, audio_ok)

        video_ok = False
        if audio_ok:
            video_ok = _run_agent5(content_id)
            _print_video_summary(content_id, video_ok)
        else:
            print("\n  ⚠ Skipping Agent 5 — Agent 4 produced no audio")

        print()
        print(SEP)
        if video_ok:
            print(f"  ✅  COMPLETE — ready for Agent 6")
        elif audio_ok:
            print(f"  ❌  AGENT 5 FAILED")
            print(f"      Re-run:  python test_full_pipeline.py --from-video {content_id}")
        else:
            print(f"  ❌  AGENT 4 FAILED — check ELEVENLABS_API_KEY and OPENAI_API_KEY")
            print(f"      Re-run:  python test_full_pipeline.py --from-audio {content_id}")
        print(SEP)
        return

    # ── --from-agent3: jump directly to Agent 3 ──────────────────────────────
    if from_agent3_id_str:
        content_id = uuid.UUID(from_agent3_id_str)
        content = _load_content(content_id)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            return

        print(f"\n  Jumping to Agent 3 for content {content_id}")
        print(f"  Status: {content.status}")

        passed      = _run_agent3(content_id)
        audio_ok    = _run_agent4(content_id) if passed else False
        video_ok    = _run_agent5(content_id) if audio_ok else False

        if not passed:
            print("\n  ⚠ Skipping Agents 4+5 — Agent 3 did not pass")
        elif not audio_ok:
            print("\n  ⚠ Skipping Agent 5 — Agent 4 produced no audio")

        _print_final_summary(content_id, audio_ok, video_ok, passed, multilingual_ok=True)
        return

    # ── --from-multilingual: skip Telegram, restart multilingual generation ──
    if from_multilingual_id_str:
        content_id = uuid.UUID(from_multilingual_id_str)
        db = _db()
        content = db.get(Content, content_id)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return
        channel   = db.get(Channel, content.channel_id)
        db.close()

        print(f"\n  Jumping to multilingual generation for content {content_id}")
        print(f"  Status: {content.status}")

        print(STEP("STEP 4: Generating multilingual scripts"))
        db = _db()
        content = db.get(Content, content_id)
        channel = db.get(Channel, content.channel_id)
        lang_scripts = generate_multilingual_scripts(content, channel, db)
        db.close()

        if not lang_scripts:
            print("  ❌ Multilingual generation produced no scripts")
            print(f"      Re-run:  python test_full_pipeline.py --from-multilingual {content_id}")
            return

        print(f"  Languages: {[s.language for s in lang_scripts]}")

        passed   = _run_agent3(content_id)
        audio_ok = _run_agent4(content_id) if passed else False
        video_ok = _run_agent5(content_id) if audio_ok else False

        if not passed:
            print("\n  ⚠ Skipping Agents 4+5 — Agent 3 did not pass")
        elif not audio_ok:
            print("\n  ⚠ Skipping Agent 5 — Agent 4 produced no audio")

        _print_final_summary(content_id, audio_ok, video_ok, passed, multilingual_ok=True)
        return

    # ── Find channel ──────────────────────────────────────────────────────────
    db = _db()

    content_id                  = None
    scripts                     = None
    story                       = None
    skip_discovery_and_scripts  = False

    if from_content_id_str:
        content_id = uuid.UUID(from_content_id_str)
        content    = db.get(Content, content_id)

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
            "title":        content.title,
            "video_script": existing_script.video_script,
            "voice_script": existing_script.voice_script,
        }
        channel_id                 = channel.id
        skip_discovery_and_scripts = True

    elif channel_id_str:
        channel = db.get(Channel, uuid.UUID(channel_id_str))
    else:
        channel = db.query(Channel).filter(Channel.active.is_(True)).first()

    if not channel:
        print("\nERROR: No active channel found. Activate one via the UI first.")
        db.close()
        return

    config   = db.get(ChannelConfig, channel.id)
    sources  = db.query(ChannelSource).filter(ChannelSource.channel_id == channel.id).all()
    max_rev  = config.validation_max_revisions if config else 3

    print(f"\n  Channel : {channel.name}")
    print(f"  Niche   : {channel.niche}")
    print(f"  Tone    : {channel.tone}")
    print(f"  Sources : {[(s.source_type, s.source_value[:50]) for s in sources]}")

    channel_id = channel.id
    db.close()

    if not skip_discovery_and_scripts:
        # ── STEP 1: Discovery ────────────────────────────────────────────────
        print(STEP("STEP 1: Discovery — Claude browsing sources (30-60s)"))

        db    = _db()
        result = run_discovery(channel_id, db)
        db.close()

        if not result:
            print("  ❌ No story found.")
            print("     Check: is the channel active? Are sources configured?")
            return

        content, story = result
        content_id     = content.id

        print(f"  Story   : {story.title[:70]}")
        print(f"  URL     : {story.url}")
        print(f"  Language: {story.language}")
        print(f"  Body    : {len(story.body.split())} words")

        # ── STEP 2: Generate source-language scripts ─────────────────────────
        print(STEP("STEP 2: Generating scripts (Claude)"))

        db      = _db()
        channel = db.get(Channel, channel_id)
        config  = db.get(ChannelConfig, channel_id)
        script_format = config.script_format if config else "youtube_long"
        scripts = generate_scripts(story, channel, script_format=script_format)

        content       = db.get(Content, content_id)
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

        wc        = len(scripts["voice_script"].split())
        dur       = estimate_duration_sec(scripts["voice_script"], story.language)
        sec_count = scripts["video_script"].count("[SECTION")

        print(f"  Title   : {scripts['title'][:70]}")
        print(f"  Voice   : {wc} words → ~{dur:.0f}s ({dur / 60:.1f} min)")
        print(f"  Sections: {sec_count}")

    else:
        print(STEP("SKIP STEP 1 & 2: Using existing content/scripts"))

        wc        = len(scripts["voice_script"].split())
        sec_count = scripts["video_script"].count("[SECTION")

        print(f"  Content : {content_id}")
        print(f"  Title   : {scripts.get('title', '')[:70] or '(no title)'}")
        print(f"  Voice   : {wc} words")
        print(f"  Sections: {sec_count}")

    # ── STEP 3: Telegram validation loop ──────────────────────────────────────
    print(STEP("STEP 3: Sending to Telegram for approval"))

    db      = _db()
    content = db.get(Content, content_id)
    channel = db.get(Channel, channel_id)
    send_for_validation(content, channel, scripts, db)

    val    = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
    msg_id = val.telegram_message_id if val else None
    db.close()

    if not msg_id:
        print("  ❌ Telegram send failed — check TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
        print(f"     When fixed, re-run: python test_full_pipeline.py --from-content {content_id}")
        return

    print(f"  ✅ Sent (message_id={msg_id})")

    for attempt in range(1, max_rev + 2):
        decision, from_user = poll_telegram(settings.telegram_bot_token, msg_id, timeout_sec=300)

        if decision.upper().startswith("APPROVE") or from_user == "timeout":
            db      = _db()
            content = db.get(Content, content_id)
            val     = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
            if val:
                val.status      = "APPROVED"
                val.approved_at = datetime.now(timezone.utc)
            content.status = "APPROVED"
            db.commit()
            db.close()
            reason = f"approved by @{from_user}" if from_user != "timeout" else "auto-approved (timeout)"
            print(f"  ✅ Content {reason}")
            break

        print(f"  CHANGE ({attempt}/{max_rev}): {decision!r} — regenerating…")

        db      = _db()
        content = db.get(Content, content_id)
        channel = db.get(Channel, channel_id)
        val     = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()

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
            db      = _db()
            content = db.get(Content, content_id)
            content.status = "APPROVED"
            db.commit()
            db.close()

    # ── STEP 4: Multilingual scripts ──────────────────────────────────────────
    print(STEP("STEP 4: Generating multilingual scripts"))

    db           = _db()
    content      = db.get(Content, content_id)
    channel      = db.get(Channel, channel_id)
    lang_scripts = generate_multilingual_scripts(content, channel, db)
    db.close()

    multilingual_ok = bool(lang_scripts)
    print(f"  Languages: {[s.language for s in lang_scripts]}")

    if not multilingual_ok:
        print("  ❌ Multilingual generation produced no scripts")
        print(f"     Re-run: python test_full_pipeline.py --from-multilingual {content_id}")
        return

    # ── STEP 5: Agent 3 ───────────────────────────────────────────────────────
    passed   = _run_agent3(content_id)

    # ── STEP 6: Agent 4 ───────────────────────────────────────────────────────
    audio_ok = False
    if passed:
        audio_ok = _run_agent4(content_id)
    else:
        print("  ⚠ Skipping Agents 4+5 — Agent 3 did not pass")

    # ── STEP 7: Agent 5 ───────────────────────────────────────────────────────
    video_ok = False
    if audio_ok:
        video_ok = _run_agent5(content_id)
    elif passed:
        print("  ⚠ Skipping Agent 5 — Agent 4 produced no audio")

    # ── STEP 8: Final summary ─────────────────────────────────────────────────
    print(STEP("STEP 8: Final state"))
    _print_final_summary(content_id, audio_ok, video_ok, passed, multilingual_ok)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Full Agent 2 → Agent 3 → Agent 4 → Agent 5 end-to-end test.",
    )

    parser.add_argument(
        "channel_id",
        nargs="?",
        help="Optional channel UUID. If omitted, first active channel is used.",
    )
    parser.add_argument(
        "--from-content",
        dest="from_content_id",
        metavar="CONTENT_ID",
        help="Skip discovery + script generation. Restart from Telegram validation (Step 3).",
    )
    parser.add_argument(
        "--from-multilingual",
        dest="from_multilingual_id",
        metavar="CONTENT_ID",
        help="Skip Telegram. Restart multilingual generation (Step 4).",
    )
    parser.add_argument(
        "--from-agent3",
        dest="from_agent3_id",
        metavar="CONTENT_ID",
        help="Skip Telegram + multilingual. Restart Agent 3 validation (Step 5).",
    )
    parser.add_argument(
        "--from-audio",
        dest="from_audio_id",
        metavar="CONTENT_ID",
        help="Skip to Agent 4 TTS+Whisper (Step 6). Content must be SCRIPTS_VALIDATED.",
    )
    parser.add_argument(
        "--from-video",
        dest="from_video_id",
        metavar="CONTENT_ID",
        help="Skip to Agent 5 Remotion render (Step 7). Content must be AUDIO_DONE.",
    )

    args = parser.parse_args()

    run(
        channel_id_str=args.channel_id,
        from_content_id_str=args.from_content_id,
        from_multilingual_id_str=args.from_multilingual_id,
        from_agent3_id_str=args.from_agent3_id,
        from_audio_id_str=args.from_audio_id,
        from_video_id_str=args.from_video_id,
    )
