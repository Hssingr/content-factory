#!/usr/bin/env python3
"""
Full Agent 2 → Agent 4 → Agent 5 end-to-end test script.

Steps:
  1. Discovery    — Claude browses your sources, picks the best story (fetch → dedup → score)
  2. Telegram     — sends title + URL + score to your phone, waits for APPROVE
  3. Scripts      — blueprint → section-by-section generation → quality gate → persist source Script
                    → Shorts Planner (non-blocking, creates Short episode Content rows)
  4. Multilingual — culturally adapted scripts for every channel language
  5. Validation   — deterministic checks + auto-correction, sets SCRIPTS_VALIDATED
  6. Agent 4      — Cartesia TTS + Whisper transcription for parent content
                    → Short episodes awaiting parent are flipped to SCRIPTS_VALIDATED
  6b. Agent 4    — Cartesia TTS + Whisper for each child Short episode (owns its audio)
  7. Agent 5      — Storyboard/remap → Flux Schnell image generation/reuse
                    → Per-language: Subtitles → Remotion render
  8. Summary      — prints final DB state (scripts + audio + video renders)

Note: Agent 3 is dissolved — validation (step 5) runs inline here and in the Celery pipeline.

Usage:
    source venv/bin/activate

    # Full run (discovery → Agent 5):
    python test_full_pipeline.py [channel_id]

    # Skip discovery + Telegram — content exists and is already approved, restart scripts:
    python test_full_pipeline.py --from-content <content_id>

    # Skip Telegram — content already approved, restart multilingual generation:
    python test_full_pipeline.py --from-multilingual <content_id>

    # Skip multilingual generation — jump straight to validation (step 5):
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
        wc  = len(af.whisper_transcript or [])
        dur = af.duration_ms / 1000
        print(
            f"    [{af.language}]  {dur:.1f}s ({dur / 60:.1f}min)"
            f"  {wc} Whisper words"
        )
        if af.whisper_transcript:
            sample = af.whisper_transcript[:3]
            print(f"             Whisper sample: {sample}")


def _print_agent5_failure_diagnostic(content_id: uuid.UUID) -> None:
    """Query DB and print per-language diagnostic when Agent 5 fails."""
    from app.models import AudioFile, Script, VideoRender, VideoSection, Content
    db = _db()
    content   = db.get(Content, content_id)
    scripts   = db.query(Script).filter(Script.content_id == content_id, Script.validated.is_(True)).all()
    audios    = db.query(AudioFile).filter(AudioFile.content_id == content_id).all()
    sections  = db.query(VideoSection).filter(VideoSection.content_id == content_id).all()
    renders   = db.query(VideoRender).filter(VideoRender.content_id == content_id).all()
    db.close()

    # "__visual__" rows = shared visual-pass beats (storyboard + Flux, run once)
    visual_beats  = sum(1 for s in sections if s.language == "__visual__")
    expected_langs = {s.language for s in scripts}
    audio_langs    = {a.language for a in audios}
    section_langs  = {}
    for s in sections:
        if s.language != "__visual__":
            section_langs.setdefault(s.language, 0)
            section_langs[s.language] += 1
    render_langs   = {}
    for r in renders:
        render_langs.setdefault(r.language, 0)
        render_langs[r.language] += 1

    print(f"\n  Agent 5 failure diagnostic for content {content_id}:")
    print(f"  Status: {content.status if content else 'NOT FOUND'}")
    print(f"  Shared visual-pass beats (Storyboard+Flux, once): {visual_beats}")

    print(f"\n  {'Lang':<6}  {'Audio':>6}  {'Sections':>9}  {'Renders':>7}  Inferred failure stage")
    for lang in sorted(expected_langs):
        has_audio  = lang in audio_langs
        n_sections = section_langs.get(lang, 0)
        n_renders  = render_langs.get(lang, 0)
        if n_renders > 0:
            stage = "SUCCESS"
        elif n_sections > 0:
            # Sections saved but no render — check logs for Agent5 [FAIL] status=...
            stage = (
                "FAILED after sections saved — check logs for Agent5 [FAIL] status=... "
                "(RENDER_BLOCKED=too many text_card beats | "
                "REMOTION_FAILED=Page crashed or render error | "
                "INVALID_PROPS=props sanity check | "
                "VERIFY_FAILED=post-render black/silence check)"
            )
        elif visual_beats > 0:
            stage = "FAILED during render setup — visual pass ran but sections not saved for this lang"
        elif has_audio:
            stage = "FAILED in visual pass — storyboard or Flux generation failed (0 shared beats)"
        else:
            stage = "SKIPPED — no audio file"
        print(f"  {lang:<6}  {'yes' if has_audio else 'NO':>6}  {n_sections:>9}  {n_renders:>7}  {stage}")

    missing_audio = expected_langs - audio_langs
    if missing_audio:
        print(f"\n  Missing audio for: {sorted(missing_audio)}")
        print("  → Check Agent 4 output or run --from-audio")


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
    # Separate shared visual-pass beats (language="__visual__") from per-lang sections
    visual_beats = [s for s in sections if s.language == "__visual__"]
    sec_by_lang  = {}
    for s in sections:
        if s.language != "__visual__":
            sec_by_lang.setdefault(s.language, []).append(s)

    flux_ok = sum(
        1 for s in visual_beats
        if (getattr(s, "generation_prompt", None) or "").find('"media_url": "cache/') >= 0
    )
    print(
        f"\n  Visual pass: {len(visual_beats)} shared beats"
        + (f"  (~{flux_ok} with Flux images)" if visual_beats else "")
    )
    print(f"  Video renders ({len(renders)} total across {len(langs)} language(s)):")
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
                f"  {rt}"
            )


def _print_final_summary(
    content_id: uuid.UUID,
    audio_ok: bool,
    video_ok: bool,
    passed: bool,
    multilingual_ok: bool,
) -> None:
    from app.models import Content, ContentValidation, Script
    from app.services.script_estimator import estimate_duration_sec

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
        print(
            f"    [{s.language}] v{s.version}  validated={s.validated}"
            f"  {wc}w  {dur:.0f}s ({dur / 60:.1f}min)"
        )

    _print_audio_summary(content_id, audio_ok)
    _print_video_summary(content_id, video_ok)

    print()
    print(SEP)
    if video_ok:
        print(f"  ✅  COMPLETE — content is {content.status} → ready for Agent 6")
    elif audio_ok:
        print(f"  ❌  AGENT 5 FAILED")
        _print_agent5_failure_diagnostic(content_id)
        print(f"\n      Re-run:  python test_full_pipeline.py --from-video {content_id}")
        print(f"      If Node.js / Remotion missing: check that npx is in PATH")
    elif passed:
        print(f"  ❌  AGENT 4 FAILED — check ELEVENLABS_API_KEY and OPENAI_API_KEY in .env")
        print(f"      Re-run:  python test_full_pipeline.py --from-audio {content_id}")
    elif multilingual_ok:
        print(f"  ⚠   VALIDATION BLOCKED — MAJOR script issues remain after max auto-corrections")
        print(f"      Review scripts in DB, then re-run:")
        print(f"      python test_full_pipeline.py --from-agent3 {content_id}")
    else:
        print(f"  ❌  MULTILINGUAL GENERATION FAILED")
        print(f"      Re-run:  python test_full_pipeline.py --from-multilingual {content_id}")
    print(SEP)


# ── Step runners (reused by multiple entry points) ────────────────────────────

def _run_script_validation(content_id: uuid.UUID) -> bool:
    """Run cross-language deterministic checks + auto-correction + set SCRIPTS_VALIDATED.

    In production this runs embedded inside run_agent2_scripts_for_content() after
    generate_multilingual_scripts() completes.  The test runner calls it as an
    explicit step because scripts are generated one step at a time for debuggability.
    Returns True if all languages are MAJOR-issue-free after up to 3 correction rounds.
    """
    from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script
    from app.agents.agent2_discovery.system_prompt import auto_correct_script
    from app.services.script_checks import run_deterministic_checks
    from app.services.script_estimator import estimate_duration_sec

    _MAX_ROUNDS = 3

    print(STEP("STEP 5: Script validation (det checks + auto-correction)"))
    db = _db()
    try:
        content = db.get(Content, content_id)
        channel = db.get(Channel, content.channel_id)
        config  = db.get(ChannelConfig, channel.id)
        script_format = config.script_format if config else "youtube_long"

        scripts_qs = (
            db.query(Script)
            .filter(Script.content_id == content_id)
            .order_by(Script.language, Script.version.desc())
            .all()
        )
        # Keep only the latest version per language
        seen: dict[str, Script] = {}
        for s in scripts_qs:
            if s.language not in seen:
                seen[s.language] = s
        scripts_rows = list(seen.values())

        if not scripts_rows:
            print("  No scripts found — skipping validation")
            return False

        scripts_by_lang = {
            s.language: {"video_script": s.video_script, "voice_script": s.voice_script}
            for s in scripts_rows
        }

        # ── Deterministic checks + auto-correct loop (per language) ──────────
        all_passed = True
        for lang, row in zip(scripts_by_lang, scripts_rows):
            voice_map_entry = (
                db.query(ChannelVoice)
                .filter(ChannelVoice.channel_id == channel.id, ChannelVoice.language == lang)
                .first()
            )
            tts_model    = voice_map_entry.tts_model if voice_map_entry else "sonic-2"
            tts_provider = voice_map_entry.provider if voice_map_entry else "cartesia"

            lang_passed = False
            for round_n in range(1, _MAX_ROUNDS + 1):
                issues_by_lang = run_deterministic_checks(scripts_by_lang, script_format)
                major = [i for i in issues_by_lang.get(lang, []) if i["severity"] == "MAJOR"]
                minor = [i for i in issues_by_lang.get(lang, []) if i["severity"] == "MINOR"]

                if not major:
                    lang_passed = True
                    if minor:
                        print(f"  [{lang}] PASS ({len(minor)} MINOR issues logged)")
                    else:
                        print(f"  [{lang}] PASS (clean)")
                    break

                print(f"  [{lang}] round={round_n} MAJOR={len(major)} — auto-correcting")
                try:
                    corrected = auto_correct_script(
                        current_scripts=scripts_by_lang[lang],
                        issues=major,
                        language=lang,
                        channel=channel,
                        script_format=script_format,
                        source_excerpt=content.source_excerpt,
                        tts_model=tts_model,
                        tts_provider=tts_provider,
                    )
                    scripts_by_lang[lang] = corrected
                except Exception as exc:
                    print(f"  [{lang}] auto-correct round {round_n} failed: {exc} — stopping")
                    break

            if not lang_passed:
                print(f"  [{lang}] FAIL — MAJOR issues remain after {_MAX_ROUNDS} rounds")
                all_passed = False

        # ── Persist corrected scripts + set validated=True ────────────────────
        for s in scripts_rows:
            updated = scripts_by_lang.get(s.language, {})
            if updated.get("video_script"):
                s.video_script = updated["video_script"]
            if updated.get("voice_script"):
                s.voice_script = updated["voice_script"]
            dur = estimate_duration_sec(s.voice_script, s.language)
            s.estimated_duration_sec = dur
            s.validated              = True

        content.status = "SCRIPTS_VALIDATED"
        db.commit()
        print(f"  {'PASS' if all_passed else 'PARTIAL'} — {len(scripts_rows)} language(s) validated; status=SCRIPTS_VALIDATED")

        # Non-blocking: create Short episode Content rows (mirrors tasks.py post-SCRIPTS_VALIDATED)
        try:
            from app.agents.agent2_discovery.services.scripts import run_shorts_planner
            run_shorts_planner(content_id, channel, config, db)
            print("  [shorts planner] Short episode rows created")
        except Exception as exc:
            print(f"  [shorts planner] skipped (non-blocking): {exc}")

        return all_passed

    finally:
        db.close()


def _run_agent4(content_id: uuid.UUID) -> bool:
    """Run Agent 4 TTS + Whisper. Returns True if at least one language succeeded.

    On success, also flips any Short episode Content rows that were waiting for
    this parent's audio to complete (mirrors pickup_short_episodes_awaiting_parent).
    """
    from app.models import Content
    from app.agents.agent4_audio.services.audio import run_audio_generation

    print(STEP("STEP 6: Agent 4 — Cartesia TTS + Whisper"))
    db = _db()
    content = db.get(Content, content_id)
    if content.status not in ("SCRIPTS_VALIDATED", "AUDIO_DONE"):
        content.status = "SCRIPTS_VALIDATED"
        db.commit()
    audio_ok = run_audio_generation(content_id, db)
    db.close()

    if audio_ok:
        # Flip Short episode children that were waiting for parent audio
        db2 = _db()
        try:
            children = (
                db2.query(Content)
                .filter(
                    Content.parent_content_id == content_id,
                    Content.status == "SCRIPTS_VALIDATED_AWAITING_PARENT",
                )
                .all()
            )
            for child in children:
                child.status = "SCRIPTS_VALIDATED"
            if children:
                db2.commit()
                print(f"  Flipped {len(children)} Short episode(s) → SCRIPTS_VALIDATED")
        finally:
            db2.close()

    return audio_ok


def _run_child_shorts_agent4(parent_content_id: uuid.UUID) -> bool:
    """Run Agent 4 TTS + Whisper for all SCRIPTS_VALIDATED child Short episodes.

    Called immediately after parent AUDIO_DONE. Checks AudioFile existence before
    each call to avoid re-generating audio that was already produced. Returns True
    if at least one child already had audio or was generated successfully.
    """
    from app.models import AudioFile, Content
    from app.agents.agent4_audio.services.audio import run_audio_generation

    print(STEP("STEP 6b: Agent 4 — Child Short TTS + Whisper"))
    db = _db()
    try:
        children = (
            db.query(Content)
            .filter(
                Content.parent_content_id == parent_content_id,
                Content.is_short_episode.is_(True),
            )
            .all()
        )
        if not children:
            print("  No child Short episodes found — skipping")
            return True

        eligible = [c for c in children if c.status == "SCRIPTS_VALIDATED"]
        if not eligible:
            statuses = {}
            for c in children:
                statuses[c.status] = statuses.get(c.status, 0) + 1
            print(
                f"  {len(children)} child Short episode(s) found, "
                f"none in SCRIPTS_VALIDATED (statuses={statuses}) — skipping"
            )
            return True

        print(f"  {len(eligible)} SCRIPTS_VALIDATED child Short episode(s) to process")
        any_ok = False
        for child in eligible:
            part_label = f"part {child.short_part_number}/{child.short_total_parts}"
            audio_exists = (
                db.query(AudioFile).filter(AudioFile.content_id == child.id).first()
            ) is not None
            if audio_exists:
                print(f"    [{part_label}] audio already exists — skipping")
                any_ok = True
                continue
            print(f"    [{part_label}] generating audio…")
            ok = run_audio_generation(child.id, db)
            if ok:
                print(f"    [{part_label}] → AUDIO_DONE")
                any_ok = True
            else:
                print(f"    [{part_label}] → FAILED (non-blocking — parent video will still render)")
        return any_ok
    finally:
        db.close()


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
        Channel, ChannelConfig, ChannelLanguage, ChannelSource, ChannelVoice,
        Content, ContentValidation, Script,
    )
    from app.agents.agent2_discovery.services.discovery import run_discovery
    from app.agents.agent2_discovery.services.scripts import (
        generate_multilingual_scripts,
        generate_script_sections,
        run_script_quality_gate,
    )
    from app.agents.agent2_discovery.services.story import Story
    from app.agents.agent2_discovery.services.validation import send_for_validation
    from app.agents.agent2_discovery.system_prompt import generate_story_blueprint
    from app.services.script_estimator import estimate_duration_sec

    print(SEP)
    print("  FULL PIPELINE TEST  —  Agent 2 → Agent 4 → Agent 5")
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
            print(f"  ❌  AGENT 5 FAILED")
            _print_agent5_failure_diagnostic(content_id)
            print(f"\n      Re-run:  python test_full_pipeline.py --from-video {content_id}")
            print(f"      If Node.js / Remotion missing: check that npx is in PATH")
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
        if audio_ok:
            _run_child_shorts_agent4(content_id)
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

    # ── --from-agent3: jump to validation step (det checks + auto-correction) ──
    if from_agent3_id_str:
        content_id = uuid.UUID(from_agent3_id_str)
        content = _load_content(content_id)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            return

        print(f"\n  Jumping to validation step for content {content_id}")
        print(f"  Status: {content.status}")

        passed      = _run_script_validation(content_id)
        audio_ok    = _run_agent4(content_id) if passed else False
        if audio_ok:
            _run_child_shorts_agent4(content_id)
        video_ok    = _run_agent5(content_id) if audio_ok else False

        if not passed:
            print("\n  ⚠ Skipping Agents 4+5 — validation did not pass")
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

        passed   = _run_script_validation(content_id)
        audio_ok = _run_agent4(content_id) if passed else False
        if audio_ok:
            _run_child_shorts_agent4(content_id)
        video_ok = _run_agent5(content_id) if audio_ok else False

        if not passed:
            print("\n  ⚠ Skipping Agents 4+5 — validation did not pass")
        elif not audio_ok:
            print("\n  ⚠ Skipping Agent 5 — Agent 4 produced no audio")

        _print_final_summary(content_id, audio_ok, video_ok, passed, multilingual_ok=True)
        return

    # ── Find channel ──────────────────────────────────────────────────────────
    db = _db()

    content_id    = None
    story: Story | None = None
    _assessment   = None
    skip_to_scripts = False  # True when jumping in after Telegram approval

    if from_content_id_str:
        # Content already exists and is approved — skip discovery + Telegram,
        # jump straight to script generation.
        content_id = uuid.UUID(from_content_id_str)
        content    = db.get(Content, content_id)

        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return

        channel    = db.get(Channel, content.channel_id)
        channel_id = channel.id

        # Ensure status allows script generation
        if content.status not in (
            "APPROVED", "GENERATING_SCRIPTS", "SCRIPTS_READY",
            "SCRIPTS_VALIDATED", "AUDIO_DONE", "GENERATING_VIDEO", "NEEDS_REVIEW",
        ):
            content.status = "APPROVED"
            db.commit()

        skip_to_scripts = True

    elif channel_id_str:
        channel = db.get(Channel, uuid.UUID(channel_id_str))
    else:
        channel = db.query(Channel).filter(Channel.active.is_(True)).first()

    if not channel:
        print("\nERROR: No active channel found. Activate one via the UI first.")
        db.close()
        return

    config  = db.get(ChannelConfig, channel.id)
    sources = db.query(ChannelSource).filter(ChannelSource.channel_id == channel.id).all()

    print(f"\n  Channel : {channel.name}")
    print(f"  Niche   : {channel.niche}")
    print(f"  Tone    : {channel.tone}")
    print(f"  Sources : {[(s.source_type, s.source_value[:50]) for s in sources]}")

    channel_id = channel.id
    db.close()

    if not skip_to_scripts:
        # ── STEP 1: Discovery ────────────────────────────────────────────────
        print(STEP("STEP 1: Discovery — fetch → dedup → score (30-60s)"))

        db     = _db()
        result = run_discovery(channel_id, db)
        db.close()

        if not result:
            print("  ❌ No story found.")
            print("     Check: is the channel active? Are sources configured?")
            return

        content, story, _assessment = result
        content_id = content.id

        print(f"  Story   : {story.title[:70]}")
        print(f"  URL     : {story.url}")
        print(f"  Language: {story.language}")
        print(f"  Score   : {_assessment.get('overall_score', '?') if _assessment else '?'}")

        # ── STEP 2: Telegram — title + URL + score, no scripts ───────────────
        print(STEP("STEP 2: Telegram approval (title + URL + score)"))

        db      = _db()
        content = db.get(Content, content_id)
        channel = db.get(Channel, channel_id)
        target_languages = [
            cl.language
            for cl in db.query(ChannelLanguage)
            .filter(ChannelLanguage.channel_id == channel_id)
            .all()
        ]
        send_for_validation(
            content, channel, db,
            assessment=_assessment,
            target_languages=target_languages,
        )
        val    = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
        msg_id = val.telegram_message_id if val else None
        db.close()

        if not msg_id:
            print("  ❌ Telegram send failed — check TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
            print(f"     When fixed, re-run: python test_full_pipeline.py --from-content {content_id}")
            return

        print(f"  ✅ Sent (message_id={msg_id})")
        print("  Reply APPROVE in Telegram (or wait 5 min for auto-approve)…\n")

        decision, from_user = poll_telegram(settings.telegram_bot_token, msg_id, timeout_sec=300)
        reason = f"approved by @{from_user}" if from_user != "timeout" else "auto-approved (timeout)"

        db      = _db()
        content = db.get(Content, content_id)
        val     = db.query(ContentValidation).filter(ContentValidation.content_id == content_id).first()
        if val:
            val.status      = "APPROVED"
            val.approved_at = datetime.now(timezone.utc)
        content.status = "APPROVED"
        db.commit()
        db.close()
        print(f"  ✅ Content {reason}")

    # ── STEP 3: Generate source-language scripts (after APPROVE) ──────────────
    print(STEP("STEP 3: Generating source scripts (Claude)"))

    db      = _db()
    content = db.get(Content, content_id)
    channel = db.get(Channel, channel_id)
    config  = db.get(ChannelConfig, channel_id)
    script_format      = config.script_format      if config else "youtube_long"
    audio_tags_enabled = config.audio_tags_enabled if config else False

    # Re-use existing source script if this is a re-run (--from-content path)
    existing_src = (
        db.query(Script)
        .filter(
            Script.content_id == content_id,
            Script.language   == content.source_language,
        )
        .order_by(Script.version.desc())
        .first()
    )

    if existing_src:
        print(f"  Existing source script found for lang={content.source_language} — skipping generation")
        wc = len(existing_src.voice_script.split())
        print(f"  Voice: {wc} words")
    else:
        # Reconstruct story proxy from content record when skipping discovery
        if story is None:
            story = Story(
                title=content.title,
                url=content.source_url,
                language=content.source_language,
                body=content.source_excerpt or "",
                source_type="db",
                source_value="content_record",
                published_at=datetime.now(timezone.utc),
                upvotes=0,
                comments=0,
            )

        src_voice = (
            db.query(ChannelVoice)
            .filter(
                ChannelVoice.channel_id == channel_id,
                ChannelVoice.language   == content.source_language,
            )
            .first()
        ) or db.query(ChannelVoice).filter(ChannelVoice.channel_id == channel_id).first()

        tts_model    = src_voice.tts_model if src_voice else "sonic-2"
        tts_provider = src_voice.provider if src_voice else "cartesia"

        # Step 3a: blueprint
        blueprint = generate_story_blueprint(story, channel, script_format=script_format)
        content.story_blueprint = blueprint
        db.commit()

        # Step 3b: section-by-section generation
        scripts = generate_script_sections(
            story=story,
            blueprint=blueprint,
            channel=channel,
            channel_voice=src_voice,
            script_format=script_format,
            audio_tags_enabled=audio_tags_enabled,
        )

        # Step 3c: quality gate (retention review + optional rewrite)
        scripts = run_script_quality_gate(
            scripts,
            channel,
            script_format=script_format,
            language=content.source_language,
            tts_model=tts_model,
            tts_provider=tts_provider,
        )

        content.title = scripts.get("title", content.title)
        src_voice_script = scripts["voice_script"]
        src_dur_sec = estimate_duration_sec(src_voice_script, content.source_language)

        # Merge visual_intent_history into story_blueprint (matches tasks.py)
        visual_history = scripts.get("visual_intent_history")
        if visual_history and content.story_blueprint is not None:
            bp = dict(content.story_blueprint)
            bp["visual_intent_history"] = visual_history
            content.story_blueprint = bp

        db.add(Script(
            content_id=content_id,
            language=story.language,
            video_script=scripts["video_script"],
            voice_script=src_voice_script,
            estimated_duration_sec=src_dur_sec,
            version=1,
            validated=True,  # matches tasks.py — source script is validated at generation
        ))
        db.commit()

        wc        = len(src_voice_script.split())
        sec_count = scripts["video_script"].count("[SECTION")
        print(f"  Title   : {scripts.get('title', content.title)[:70]}")
        print(f"  Blueprint: {len(blueprint.get('major_turns', []))} major turns")
        print(f"  Voice   : {wc} words → ~{src_dur_sec:.0f}s ({src_dur_sec / 60:.1f} min)")
        print(f"  Sections: {sec_count}")

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

    # ── STEP 5: Script validation (det checks + auto-correction) ─────────────
    passed   = _run_script_validation(content_id)

    # ── STEP 6: Agent 4 ───────────────────────────────────────────────────────
    audio_ok = False
    if passed:
        audio_ok = _run_agent4(content_id)
    else:
        print("  ⚠ Skipping Agents 4+5 — validation did not pass")

    # ── STEP 6b: Child Short TTS + Whisper ────────────────────────────────────
    if audio_ok:
        _run_child_shorts_agent4(content_id)

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
        description="Full Agent 2 → Agent 4 → Agent 5 end-to-end test.",
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
        help="Skip discovery + Telegram. Content must exist in DB (already approved). Restarts from script generation (Step 3).",
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
        help="Skip Telegram + multilingual. Jump to validation step (det checks + auto-correction, Step 5).",
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
