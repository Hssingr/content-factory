#!/usr/bin/env python3
"""
Full operator entrypoint — Agent 2 (discovery + scripts) -> Agent 3 (audio)
-> Agent 4 (visuals) -> Agent 5 (render).

This is the real-money, real-API harness an operator runs to produce one
finished parent video and its standalone Shorts end to end. It is resumable:
every step first checks the database for already-valid artifacts and reuses
them instead of regenerating, unless the matching --force-* flag is passed.

Frozen parent flow:
    APPROVED -> Agent 2 scripts + multilingual -> SCRIPTS_VALIDATED
    -> Agent 3 audio + Whisper -> AUDIO_DONE
    -> Agent 4 visuals + validate_storyboard() + validate_media_assets() -> PARENT_VISUALS_DONE
    -> Agent 5 render -> RENDERED

Frozen child (standalone Short) flow:
    created by Agent 2's shorts planner from the parent's validated source script
    -> child source + multilingual scripts -> SCRIPTS_VALIDATED
    -> Agent 3 audio + Whisper (own audio, never the parent's) -> AUDIO_DONE
    -> Agent 4 remap + same two validators -> CHILD_SHORT_VISUALS_DONE
    -> Agent 5 render -> RENDERED

Child status never changes as a side effect of parent audio or parent visual
success — each child only advances through its own script/audio/visual/render
step, called independently in the same loop body the parent uses.

Steps:
    STEP 0  Preflight             — env vars, DB target, mode (dry-run/confirm)
    STEP 1  Discovery / load      — find a story, or load an existing content row
    STEP 2  Approval (Telegram)   — skipped/reused if already APPROVED+
    STEP 3  Parent scripts        — blueprint/sections/quality-gate (skipped if
                                     a validated source script already exists)
                                     + generate_multilingual_scripts() (already
                                     idempotent per language) -> SCRIPTS_VALIDATED
    STEP 4  Child short planning  — run_shorts_planner() (skipped if child rows
                                     already exist, unless --force-scripts)
    STEP 5  Parent + child audio  — run_audio_generation() per content row
    STEP 6  Parent visuals        — run_visual_generation_for_content() (parent)
    STEP 7  Child visuals         — run_visual_generation_for_content() (each child)
    STEP 8  Parent + child render — run_video_generation() per content row
    STEP 9  Final summary         — status table + rerun command suggestions

Usage:
    source venv/bin/activate

    # Inspect what would happen, no paid calls, no DB writes:
    python test_full_pipeline.py --dry-run [channel_id]

    # Full real run (discovery -> render). Requires --confirm:
    python test_full_pipeline.py --confirm [channel_id]

    # Resume points (each requires --confirm for a real run):
    python test_full_pipeline.py --confirm --from-content <content_id>
    python test_full_pipeline.py --confirm --from-validation <content_id>
    python test_full_pipeline.py --confirm --from-audio <content_id>
    python test_full_pipeline.py --confirm --from-visuals <content_id>
    python test_full_pipeline.py --confirm --from-render <content_id>

    # Force flags (never delete anything; only skip the "reuse" shortcut):
    python test_full_pipeline.py --confirm --from-audio <content_id> --force-audio

If no channel_id is given, uses the first active channel found.
"""

import argparse
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(name)s — %(message)s",
)

SEP  = "=" * 70
STEP = lambda title: f"\n── {title} " + "─" * max(0, 66 - len(title))

# Required for any real (non-dry-run) call. Each tuple is
# (settings attribute, displayed env var name, used by).
REQUIRED_ENV_VARS = [
    ("database_url",       "DATABASE_URL",       "all steps"),
    ("anthropic_api_key",  "ANTHROPIC_API_KEY",  "Agent 2 + Agent 4 (Claude)"),
    ("cartesia_api_key",   "CARTESIA_API_KEY",   "Agent 3 (TTS, default provider)"),
    ("openai_api_key",     "OPENAI_API_KEY",      "Agent 3 (Whisper)"),
    ("fal_key",            "FAL_KEY",             "Agent 4 (Flux image generation)"),
    ("telegram_bot_token", "TELEGRAM_BOT_TOKEN",  "Step 2 (story approval)"),
    ("telegram_chat_id",   "TELEGRAM_CHAT_ID",    "Step 2 (story approval)"),
]
OPTIONAL_ENV_VARS = [
    ("elevenlabs_api_key", "ELEVENLABS_API_KEY", "Agent 3 (legacy TTS provider, only if a channel voice uses it)"),
]


# ── DB helper ────────────────────────────────────────────────────────────────

def _db():
    from app.database import _get_session_factory
    return _get_session_factory()()


# ── Preflight: env vars + DB target (Part 4 / safe operator mode) ────────────

def _check_env_vars() -> tuple[bool, list[tuple[str, str, bool]]]:
    """Returns (all_required_present, rows) where rows = (name, used_by, present)."""
    from app.config import settings

    rows: list[tuple[str, str, bool]] = []
    all_required = True
    for attr, name, used_by in REQUIRED_ENV_VARS:
        present = bool(getattr(settings, attr, ""))
        rows.append((name, used_by, present))
        all_required = all_required and present
    for attr, name, used_by in OPTIONAL_ENV_VARS:
        present = bool(getattr(settings, attr, ""))
        rows.append((f"{name} (optional)", used_by, present))
    return all_required, rows


def _print_env_check() -> bool:
    all_required, rows = _check_env_vars()
    print("\n  Required environment variables:")
    for name, used_by, present in rows:
        mark = "✅" if present else "❌"
        print(f"    {mark}  {name:<28} ({used_by})")
    if not all_required:
        print("\n  ❌ One or more required env vars are missing — no paid API will be called.")
    return all_required


def _print_db_target() -> None:
    from app.config import settings
    parsed = urlsplit(settings.database_url)
    host = parsed.hostname or "?"
    port = parsed.port
    dbname = (parsed.path or "/?").lstrip("/")
    target = f"{host}:{port}/{dbname}" if port else f"{host}/{dbname}"
    print(f"\n  Database target : {target}   (credentials not shown)")


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


# ── State-detection dataclasses (Part 2 — resume / idempotency) ──────────────

@dataclass
class ScriptState:
    source_exists: bool
    source_validated: bool
    required_languages: list[str]
    validated_languages: set[str] = field(default_factory=set)

    @property
    def complete(self) -> bool:
        return self.source_validated and set(self.required_languages) <= self.validated_languages


def _script_state(content, channel, db) -> ScriptState:
    from app.models import Script
    from app.agents.agent2_discovery.services.scripts import _required_script_languages

    rows = (
        db.query(Script)
        .filter(Script.content_id == content.id)
        .order_by(Script.language, Script.version.desc())
        .all()
    )
    latest_by_lang = {}
    for s in rows:
        latest_by_lang.setdefault(s.language, s)

    source = latest_by_lang.get(content.source_language)
    required = _required_script_languages(content, channel, db)
    validated_langs = {lang for lang, s in latest_by_lang.items() if s.validated}

    return ScriptState(
        source_exists=source is not None,
        source_validated=bool(source and source.validated),
        required_languages=required,
        validated_languages=validated_langs,
    )


def _audio_exists(content_id: uuid.UUID, db) -> int:
    from app.models import AudioFile
    return db.query(AudioFile).filter(AudioFile.content_id == content_id).count()


def _visual_sections_count(content_id: uuid.UUID, db) -> int:
    from app.models import VideoSection
    return (
        db.query(VideoSection)
        .filter(VideoSection.content_id == content_id, VideoSection.language != "__visual__")
        .count()
    )


def _existing_children(parent_content_id: uuid.UUID, db) -> list:
    from app.models import Content
    return (
        db.query(Content)
        .filter(Content.parent_content_id == parent_content_id, Content.is_short_episode.is_(True))
        .order_by(Content.short_part_number)
        .all()
    )


# ── Step 3: parent source + multilingual scripts ─────────────────────────────

def _run_step_scripts(content, channel, config, db, *, story=None, force: bool = False) -> bool:
    """Generate (or reuse) the parent's complete required script set.

    Returns True iff content.status == SCRIPTS_VALIDATED after this step.
    """
    from app.models import Channel, ChannelVoice, Script
    from app.agents.agent2_discovery.services.scripts import (
        generate_multilingual_scripts, generate_script_sections, run_script_quality_gate,
    )
    from app.agents.agent2_discovery.services.story import Story
    from app.agents.agent2_discovery.system_prompt import generate_story_blueprint
    from app.services.script_estimator import estimate_duration_sec

    print(STEP("STEP 3: Parent scripts + multilingual scripts"))

    state = _script_state(content, channel, db)
    print(f"  Required languages: {state.required_languages}")
    print(
        f"  Source script: {'exists+validated' if state.source_validated else 'exists, not validated' if state.source_exists else 'missing'}"
    )

    if state.source_validated and not force:
        print("  REUSED — validated source script already exists, skipping blueprint/section generation")
    else:
        if state.source_exists and force:
            print("  WARNING --force-scripts set — a source script already exists; "
                  "this will insert a NEW Script version row (nothing is deleted).")

        config_obj = config
        script_format      = config_obj.script_format      if config_obj else "youtube_long"
        audio_tags_enabled = config_obj.audio_tags_enabled if config_obj else False

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
            .filter(ChannelVoice.channel_id == channel.id, ChannelVoice.language == content.source_language)
            .first()
        ) or db.query(ChannelVoice).filter(ChannelVoice.channel_id == channel.id).first()
        tts_model    = src_voice.tts_model if src_voice else "sonic-2"
        tts_provider = src_voice.provider if src_voice else "cartesia"

        content.status = "GENERATING_SCRIPTS"
        db.commit()

        blueprint = generate_story_blueprint(story, channel, script_format=script_format)
        content.story_blueprint = blueprint
        db.commit()

        scripts = generate_script_sections(
            story=story, blueprint=blueprint, channel=channel, channel_voice=src_voice,
            script_format=script_format, audio_tags_enabled=audio_tags_enabled,
        )
        scripts = run_script_quality_gate(
            scripts, channel, script_format=script_format, language=content.source_language,
            tts_model=tts_model, tts_provider=tts_provider,
        )

        content.title = scripts.get("title", content.title)
        voice_script = scripts["voice_script"]
        dur_sec = estimate_duration_sec(voice_script, content.source_language)

        visual_history = scripts.get("visual_intent_history")
        if visual_history and content.story_blueprint is not None:
            bp = dict(content.story_blueprint)
            bp["visual_intent_history"] = visual_history
            content.story_blueprint = bp

        next_version = 1
        if state.source_exists:
            prev = (
                db.query(Script)
                .filter(Script.content_id == content.id, Script.language == content.source_language)
                .order_by(Script.version.desc())
                .first()
            )
            next_version = (prev.version + 1) if prev else 1

        db.add(Script(
            content_id=content.id, language=content.source_language,
            voice_script=voice_script,
            estimated_duration_sec=dur_sec, version=next_version, validated=True,
        ))
        db.commit()
        wc = len(voice_script.split())
        print(f"  GENERATED — source script v{next_version}: {wc}w, ~{dur_sec:.0f}s")

    # generate_multilingual_scripts() is already idempotent per-language
    # (skips any language that already has a validated Script row) — see
    # app/agents/agent2_discovery/services/scripts.py. It owns the complete
    # required-set check and sets Content.status="FAILED" itself if any
    # required language could not be generated; it never writes
    # SCRIPTS_VALIDATED — that transition belongs to the caller, mirroring
    # run_script_workflow() exactly.
    required_scripts = generate_multilingual_scripts(content, channel, db)
    print(f"  Multilingual set: {[s.language for s in required_scripts] or 'INCOMPLETE'}")

    if not required_scripts:
        print("  ❌ SCRIPTS INCOMPLETE — required language(s) could not be generated/validated.")
        print(f"     Content.status is now {content.status!r} (set by generate_multilingual_scripts()).")
        print(f"     Re-run after investigating:  python test_full_pipeline.py --confirm --from-validation {content.id}")
        return False

    content.status = "SCRIPTS_VALIDATED"
    db.commit()
    print(f"  ✅ SCRIPTS_VALIDATED — {len(required_scripts)} language(s) complete")
    return True


# ── Step 4: child short planning ──────────────────────────────────────────────

def _run_step_shorts_planning(content, channel, config, db, *, force: bool = False) -> list:
    from app.agents.agent2_discovery.services.scripts import run_shorts_planner

    print(STEP("STEP 4: Child short planning + scripts"))

    existing = _existing_children(content.id, db)
    if existing and not force:
        print(f"  REUSED — {len(existing)} child Short row(s) already exist, skipping shorts planner")
        return existing

    if existing and force:
        print(
            f"  WARNING --force-scripts set — {len(existing)} child row(s) already exist. "
            "run_shorts_planner() will not create duplicate Content rows (it checks for "
            "existing children before persisting), but it will still make a Claude planning "
            "call that is discarded. No child rows will be deleted."
        )

    try:
        run_shorts_planner(content.id, channel, config, db)
    except Exception as exc:
        print(f"  ⚠ run_shorts_planner failed (non-blocking, matches production): {exc}")

    children = _existing_children(content.id, db)
    if not children:
        print("  No child Short rows were created (planner declined, or parent source script not validated)")
    else:
        print(f"  {len(children)} child Short row(s) present:")
        for c in children:
            print(f"    part {c.short_part_number}/{c.short_total_parts}  id={c.id}  status={c.status}")
    return children


# ── Step 5/6/7/8: per-content generic step runners ────────────────────────────

def _label(content) -> str:
    if content.is_short_episode:
        return f"child part {content.short_part_number}/{content.short_total_parts} ({content.id})"
    return f"parent ({content.id})"


def _run_step_audio(content, db, *, force: bool = False) -> bool:
    from app.agents.agent3_audio.services.audio import run_audio_generation

    label = _label(content)
    if content.status not in ("SCRIPTS_VALIDATED", "GENERATING_AUDIO", "AUDIO_DONE", "FAILED"):
        print(f"  [{label}] status={content.status} — not eligible for audio yet, skipping")
        return content.status == "AUDIO_DONE"

    if content.status == "FAILED":
        # FAILED is a failure state, not valid data — retrying is the default
        # resumable behavior and needs no --force. run_audio_generation() does not
        # require a specific starting status (it only needs validated scripts), so
        # no status reset is needed before calling it, unlike the visuals/render steps.
        print(f"  [{label}] status=FAILED — retrying audio generation (see application logs for the original failure)")

    existing = _audio_exists(content.id, db)
    if content.status == "AUDIO_DONE" and existing and not force:
        print(f"  [{label}] REUSED — AUDIO_DONE with {existing} AudioFile row(s), skipping")
        return True
    if force and existing:
        print(
            f"  [{label}] WARNING --force-audio set — re-running audio generation. "
            "TTS is skipped only if the mp3 is still on disk; Whisper will be re-billed "
            "for every language regardless. No AudioFile rows will be deleted (upsert only)."
        )

    ok = run_audio_generation(content.id, db)
    print(f"  [{label}] {'✅ AUDIO_DONE' if ok else '❌ FAILED'} (status={content.status})")
    return ok


def _run_step_visuals(content, db, *, force: bool = False) -> bool:
    from app.agents.agent4_visuals.services.visual_orchestrator import run_visual_generation_for_content

    label = _label(content)
    done_status = "CHILD_SHORT_VISUALS_DONE" if content.is_short_episode else "PARENT_VISUALS_DONE"

    if content.status == done_status and not force:
        n = _visual_sections_count(content.id, db)
        print(f"  [{label}] REUSED — already {done_status} ({n} persisted VideoSection row(s)), skipping")
        return True

    if content.status not in ("AUDIO_DONE", "GENERATING_VISUALS", done_status, "FAILED"):
        print(f"  [{label}] status={content.status} — not eligible for visuals yet, skipping")
        return False

    if content.status == "FAILED":
        # FAILED is not "valid data that exists" — it's a failure state, so retrying
        # it is the default resumable behavior and needs no --force (unlike redoing
        # an already-successful done_status row, which does). See application logs
        # for why it failed previously.
        print(f"  [{label}] status=FAILED — retrying visual generation (see application logs for the original failure)")
        content.status = "AUDIO_DONE"
        db.commit()
    elif force and content.status == done_status:
        print(
            f"  [{label}] WARNING --force-visuals set on {done_status} content — "
            "run_visual_generation_for_content() only proceeds from AUDIO_DONE/GENERATING_VISUALS, "
            "so this resets Content.status to AUDIO_DONE to re-enter the visual pass. "
            "No VideoSection rows are deleted; Flux only regenerates beats whose cached image is missing."
        )
        content.status = "AUDIO_DONE"
        db.commit()

    ok = run_visual_generation_for_content(content.id, db)
    print(f"  [{label}] {'✅ ' + done_status if ok else '⏸ deferred/failed'} (status={content.status})")
    return ok


def _run_step_render(content, db, *, force: bool = False) -> bool:
    from app.agents.agent5_render.services.video import run_video_generation

    label = _label(content)
    visuals_done_status = "CHILD_SHORT_VISUALS_DONE" if content.is_short_episode else "PARENT_VISUALS_DONE"

    if content.status == "RENDERED" and not force:
        n = _render_count(content.id, db)
        print(f"  [{label}] REUSED — already RENDERED ({n} VideoRender row(s)), skipping")
        return True

    if content.status not in (visuals_done_status, "RENDERING", "RENDERED", "FAILED"):
        print(f"  [{label}] status={content.status} — not eligible for render yet, skipping")
        return False

    if content.status == "FAILED":
        # Same reasoning as the visuals step: FAILED is a failure state, not valid
        # data — retrying it is the default resumable behavior and needs no --force.
        print(f"  [{label}] status=FAILED — retrying render (see application logs for the original failure)")
        content.status = visuals_done_status
        db.commit()
    elif force and content.status == "RENDERED":
        print(
            f"  [{label}] WARNING --force-render set on RENDERED content — resetting Content.status "
            f"to {visuals_done_status} to re-enter the render pass. No VideoRender rows are deleted; "
            "Agent 5 itself skips re-rendering a language whose MP4 + VideoRender row already exist."
        )
        content.status = visuals_done_status
        db.commit()

    ok = run_video_generation(content.id, db)
    print(f"  [{label}] {'✅ RENDERED' if ok else '❌ FAILED'} (status={content.status})")
    return ok


def _render_count(content_id: uuid.UUID, db) -> int:
    from app.models import VideoRender
    return db.query(VideoRender).filter(VideoRender.content_id == content_id).count()


# ── Final summary (Part 9) ────────────────────────────────────────────────────

def _media_path_for_render(content_id: uuid.UUID, language: str, fmt: str, short_order: int | None) -> str:
    from app.config import settings
    if fmt == "short":
        return f"{settings.media_path}/video/{content_id}/{language}_short_{short_order}.mp4"
    return f"{settings.media_path}/video/{content_id}/{language}_main.mp4"


def _summary_row(content, channel, db) -> dict:
    from app.models import AudioFile, Channel, ChannelLanguage, Script, VideoRender

    scripts = db.query(Script).filter(Script.content_id == content.id).all()
    by_lang: dict[str, Script] = {}
    for s in scripts:
        if s.language not in by_lang or s.version > by_lang[s.language].version:
            by_lang[s.language] = s
    required = [
        cl.language for cl in db.query(ChannelLanguage).filter(ChannelLanguage.channel_id == channel.id).all()
    ]
    if content.source_language not in required:
        required = [content.source_language] + required

    audio_n = _audio_exists(content.id, db)
    sections_n = _visual_sections_count(content.id, db)
    renders = db.query(VideoRender).filter(VideoRender.content_id == content.id).all()

    return {
        "content_id": str(content.id),
        "type": "parent" if not content.is_short_episode else f"child part {content.short_part_number}/{content.short_total_parts}",
        "status": content.status,
        "source_script": "validated" if by_lang.get(content.source_language) and by_lang[content.source_language].validated else (
            "exists" if content.source_language in by_lang else "missing"
        ),
        "multilingual": f"{sum(1 for s in by_lang.values() if s.validated)}/{len(required)} validated",
        "audio_files": audio_n,
        "video_sections": sections_n,
        "video_renders": len(renders),
        "render_paths": [
            _media_path_for_render(content.id, r.language, r.format, r.short_order) for r in renders
        ],
    }


def _print_final_summary(parent, children, channel, db) -> None:
    print(STEP("STEP 9: Final summary"))

    rows = [_summary_row(parent, channel, db)] + [_summary_row(c, channel, db) for c in children]

    print(f"\n  {'Type':<22} {'Status':<24} {'Src script':<10} {'Multilingual':<16} {'Audio':>5} {'Sections':>8} {'Renders':>7}")
    for r in rows:
        print(
            f"  {r['type']:<22} {r['status']:<24} {r['source_script']:<10} {r['multilingual']:<16} "
            f"{r['audio_files']:>5} {r['video_sections']:>8} {r['video_renders']:>7}"
        )

    print("\n  Content IDs:")
    for r in rows:
        print(f"    {r['type']:<22} {r['content_id']}")
        for p in r["render_paths"]:
            print(f"      → {p}")

    print()
    print(SEP)
    all_rendered = all(r["status"] == "RENDERED" for r in rows) if rows else False
    if all_rendered:
        print("  ✅  COMPLETE — parent and all child Shorts are RENDERED")
    else:
        not_done = [r for r in rows if r["status"] != "RENDERED"]
        print(f"  ⚠   {len(not_done)}/{len(rows)} row(s) not yet RENDERED")
    print(SEP)

    print("\n  Resume commands for the parent:")
    print(f"    python test_full_pipeline.py --confirm --from-content  {parent.id}")
    print(f"    python test_full_pipeline.py --confirm --from-validation {parent.id}")
    print(f"    python test_full_pipeline.py --confirm --from-audio    {parent.id}")
    print(f"    python test_full_pipeline.py --confirm --from-visuals  {parent.id}")
    print(f"    python test_full_pipeline.py --confirm --from-render   {parent.id}")
    if children:
        print("\n  Each child can be targeted individually with the same flags and its own content_id above.")

    print(
        "\n  Note: per-beat media-asset validation issue counts (validate_media_assets()) are "
        "observability-only log output, not a persisted DB column — check application logs for "
        "MAJOR findings; they are not summarized here because there is nothing to query."
    )


# ── Resume-point dispatch (Part 3 entrypoints) ───────────────────────────────

def _load_content_channel_config(content_id: uuid.UUID, db):
    from app.models import Channel, ChannelConfig, Content
    content = db.get(Content, content_id)
    if not content:
        return None, None, None
    channel = db.get(Channel, content.channel_id)
    config = db.get(ChannelConfig, channel.id) if channel else None
    return content, channel, config


def _execute_audio_through_render(parent, children, channel, db, force_audio, force_visuals, force_render) -> None:
    print(STEP("STEP 5: Parent + child audio"))
    _run_step_audio(parent, db, force=force_audio)
    for c in children:
        _run_step_audio(c, db, force=force_audio)

    print(STEP("STEP 6: Parent visuals"))
    _run_step_visuals(parent, db, force=force_visuals)

    print(STEP("STEP 7: Child visuals"))
    for c in children:
        _run_step_visuals(c, db, force=force_visuals)

    print(STEP("STEP 8: Parent + child render"))
    _run_step_render(parent, db, force=force_render)
    for c in children:
        _run_step_render(c, db, force=force_render)

    _print_final_summary(parent, children, channel, db)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    print(SEP)
    print("  FULL PIPELINE — Agent 2 -> Agent 3 -> Agent 4 -> Agent 5")
    print(SEP)

    print(STEP("STEP 0: Preflight"))
    env_ok = _print_env_check()
    _print_db_target()
    print(f"\n  Mode: {'DRY RUN (no paid calls, no DB writes)' if args.dry_run else 'REAL RUN'}")

    if not args.dry_run and not args.confirm:
        print(
            "\n  ❌ Refusing to run for real without --confirm. "
            "This run may call Claude, a TTS provider, Whisper, fal.ai, and Remotion, "
            "all of which can cost money. Re-run with --confirm, or use --dry-run first."
        )
        sys.exit(1)

    if not env_ok and not args.dry_run:
        print("\n  ❌ Required environment variable(s) missing — aborting before any paid call.")
        sys.exit(1)

    # ── Resume entrypoints that skip straight past discovery/scripts ─────────
    if args.from_render:
        content_id = uuid.UUID(args.from_render)
        if args.dry_run:
            _dry_run_report(content_id)
            return
        db = _db()
        content, channel, config = _load_content_channel_config(content_id, db)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return
        from app.models import Content
        parent = content if not content.is_short_episode else db.get(Content, content.parent_content_id)
        children = _existing_children(parent.id, db) if not content.is_short_episode else []
        print(STEP("STEP 8: Parent + child render"))
        _run_step_render(parent, db, force=args.force_render)
        for c in children:
            _run_step_render(c, db, force=args.force_render)
        _print_final_summary(parent, children, channel, db)
        db.close()
        return

    if args.from_visuals:
        content_id = uuid.UUID(args.from_visuals)
        if args.dry_run:
            _dry_run_report(content_id)
            return
        db = _db()
        from app.models import Content
        content, channel, config = _load_content_channel_config(content_id, db)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return
        parent = content if not content.is_short_episode else db.get(Content, content.parent_content_id)
        children = _existing_children(parent.id, db) if not content.is_short_episode else []
        print(STEP("STEP 6: Parent visuals"))
        _run_step_visuals(parent, db, force=args.force_visuals)
        print(STEP("STEP 7: Child visuals"))
        for c in children:
            _run_step_visuals(c, db, force=args.force_visuals)
        print(STEP("STEP 8: Parent + child render"))
        _run_step_render(parent, db, force=args.force_render)
        for c in children:
            _run_step_render(c, db, force=args.force_render)
        _print_final_summary(parent, children, channel, db)
        db.close()
        return

    if args.from_audio:
        content_id = uuid.UUID(args.from_audio)
        if args.dry_run:
            _dry_run_report(content_id)
            return
        db = _db()
        from app.models import Content
        content, channel, config = _load_content_channel_config(content_id, db)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return
        parent = content if not content.is_short_episode else db.get(Content, content.parent_content_id)
        children = _existing_children(parent.id, db) if not content.is_short_episode else []
        _execute_audio_through_render(
            parent, children, channel, db, args.force_audio, args.force_visuals, args.force_render
        )
        db.close()
        return

    # ── --from-validation: resume after parent source script, before/at multilingual ──
    if args.from_validation:
        content_id = uuid.UUID(args.from_validation)
        if args.dry_run:
            _dry_run_report(content_id)
            return
        db = _db()
        content, channel, config = _load_content_channel_config(content_id, db)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return
        passed = _run_step_scripts(content, channel, config, db, force=args.force_scripts)
        children = []
        if passed:
            children = _run_step_shorts_planning(content, channel, config, db, force=args.force_scripts)
            _execute_audio_through_render(content, children, channel, db, args.force_audio, args.force_visuals, args.force_render)
        else:
            print("\n  ⚠ Skipping shorts planning + Agents 3/4/5 — script set is incomplete.")
            _print_final_summary(content, children, channel, db)
        db.close()
        return

    # ── --from-content / fresh discovery ──────────────────────────────────────
    if args.dry_run and args.from_content:
        _dry_run_report(uuid.UUID(args.from_content))
        return
    if args.dry_run and not args.from_content:
        _dry_run_report_fresh(args.channel_id)
        return

    db = _db()
    from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelSource, Content, ContentValidation
    from app.agents.agent2_discovery.services.discovery import run_discovery
    from app.agents.agent2_discovery.services.validation import send_for_validation

    story = None
    if args.from_content:
        content_id = uuid.UUID(args.from_content)
        content = db.get(Content, content_id)
        if not content:
            print(f"\nERROR: Content not found: {content_id}")
            db.close()
            return
        channel = db.get(Channel, content.channel_id)
        print(STEP("STEP 1: Existing content load"))
        print(f"  Reusing content {content_id}  status={content.status}")
        if content.status == "PENDING_APPROVAL":
            print(STEP("STEP 2: Approval (Telegram)"))
            content, channel = _run_telegram_approval(content, channel, db)
        else:
            print(STEP("STEP 2: Approval (Telegram)"))
            print(f"  REUSED — status is already {content.status!r}, skipping Telegram gate")
    else:
        print(STEP("STEP 1: Discovery"))
        channel = (
            db.get(Channel, uuid.UUID(args.channel_id)) if args.channel_id
            else db.query(Channel).filter(Channel.active.is_(True)).first()
        )
        if not channel:
            print("\nERROR: No active channel found. Activate one via the UI first.")
            db.close()
            return
        config = db.get(ChannelConfig, channel.id)
        sources = db.query(ChannelSource).filter(ChannelSource.channel_id == channel.id).all()
        print(f"  Channel : {channel.name}")
        print(f"  Niche   : {channel.niche}")
        print(f"  Sources : {[(s.source_type, s.source_value[:50]) for s in sources]}")

        result = run_discovery(channel.id, db)
        if not result:
            print("  ❌ No story found. Check: is the channel active? Are sources configured?")
            db.close()
            return
        content, story, assessment = result
        print(f"  Story   : {content.title[:70]}")
        print(f"  Score   : {assessment.get('overall_score', '?') if assessment else '?'}")

        print(STEP("STEP 2: Approval (Telegram)"))
        content, channel = _run_telegram_approval(content, channel, db, assessment=assessment)

    config = db.get(ChannelConfig, channel.id)
    content_reloaded = db.get(Content, content.id)
    if content_reloaded.status != "APPROVED":
        print(f"\n  ❌ Content is {content_reloaded.status!r}, not APPROVED — stopping before script generation.")
        db.close()
        return

    passed = _run_step_scripts(content_reloaded, channel, config, db, story=story, force=args.force_scripts)
    children = []
    if passed:
        children = _run_step_shorts_planning(content_reloaded, channel, config, db, force=args.force_scripts)
        _execute_audio_through_render(
            content_reloaded, children, channel, db, args.force_audio, args.force_visuals, args.force_render
        )
    else:
        print("\n  ⚠ Skipping shorts planning + Agents 3/4/5 — script set is incomplete.")
        _print_final_summary(content_reloaded, children, channel, db)
    db.close()


def _run_telegram_approval(content, channel, db, assessment: dict | None = None):
    from app.config import settings
    from app.models import Channel, ChannelLanguage, Content, ContentValidation
    from app.agents.agent2_discovery.services.validation import send_for_validation

    target_languages = [
        cl.language for cl in db.query(ChannelLanguage).filter(ChannelLanguage.channel_id == channel.id).all()
    ]
    send_for_validation(content, channel, db, assessment=assessment, target_languages=target_languages)
    val = db.query(ContentValidation).filter(ContentValidation.content_id == content.id).first()
    msg_id = val.telegram_message_id if val else None

    if not msg_id:
        print("  ❌ Telegram send failed — check TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
        print(f"     When fixed, re-run: python test_full_pipeline.py --confirm --from-content {content.id}")
        return content, channel

    print(f"  ✅ Sent (message_id={msg_id})")
    decision, from_user = poll_telegram(settings.telegram_bot_token, msg_id, timeout_sec=300)
    reason = f"approved by @{from_user}" if from_user != "timeout" else "auto-approved (timeout)"

    content = db.get(Content, content.id)
    val = db.query(ContentValidation).filter(ContentValidation.content_id == content.id).first()
    if val:
        val.status = "APPROVED"
        val.approved_at = datetime.now(timezone.utc)
    content.status = "APPROVED"
    db.commit()
    print(f"  ✅ Content {reason}")
    return content, channel


# ── Dry-run reporting (no paid calls, no DB writes) ──────────────────────────

def _dry_run_report_fresh(channel_id_str: str | None) -> None:
    from app.models import Channel, ChannelConfig, ChannelSource

    db = _db()
    channel = (
        db.get(Channel, uuid.UUID(channel_id_str)) if channel_id_str
        else db.query(Channel).filter(Channel.active.is_(True)).first()
    )
    if not channel:
        print("\n  DRY RUN: no active channel found — nothing to plan.")
        db.close()
        return
    sources = db.query(ChannelSource).filter(ChannelSource.channel_id == channel.id).all()
    print(f"\n  DRY RUN would run for channel: {channel.name} (niche={channel.niche})")
    print(f"  Sources configured: {[(s.source_type, s.source_value[:50]) for s in sources]}")
    print("  Would call: run_discovery() [paid: Claude] -> Telegram approval -> "
          "Agent 2 scripts [paid: Claude] -> Agent 3 audio [paid: Cartesia/Whisper] -> "
          "Agent 4 visuals [paid: Claude/fal.ai] -> Agent 5 render [Remotion, local].")
    db.close()


def _dry_run_report(content_id: uuid.UUID) -> None:
    from app.models import Channel, ChannelConfig, Content

    db = _db()
    content, channel, config = _load_content_channel_config(content_id, db)
    if not content:
        print(f"\n  DRY RUN: content {content_id} not found.")
        db.close()
        return

    parent = content if not content.is_short_episode else db.get(Content, content.parent_content_id)
    children = _existing_children(parent.id, db) if not content.is_short_episode else []

    print(f"\n  DRY RUN report for content {content_id}")
    print(f"  Status: {content.status}")

    state = _script_state(parent, channel, db)
    print(f"\n  Parent script state: source={'validated' if state.source_validated else 'pending'} "
          f"required={state.required_languages} validated={sorted(state.validated_languages)}")
    print(f"  {'REUSE' if state.complete else 'WOULD GENERATE'} parent scripts")

    print(f"  Existing child rows: {len(children)}")
    for c in children:
        cstate = _script_state(c, channel, db)
        print(f"    part {c.short_part_number}: status={c.status} scripts_complete={cstate.complete}")

    for c in [parent] + children:
        n_audio = _audio_exists(c.id, db)
        n_sections = _visual_sections_count(c.id, db)
        n_renders = _render_count(c.id, db)
        label = _label(c)
        print(f"\n  [{label}]")
        print(f"    audio files: {n_audio}   {'REUSE' if n_audio else 'WOULD GENERATE'}")
        print(f"    video sections: {n_sections}   {'REUSE' if n_sections else 'WOULD GENERATE'}")
        print(f"    video renders: {n_renders}   {'REUSE' if n_renders else 'WOULD GENERATE'}")

    print("\n  No paid API was called. No DB row was written.")
    db.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Full Agent 2 -> Agent 3 -> Agent 4 -> Agent 5 operator entrypoint.",
    )
    parser.add_argument("channel_id", nargs="?", help="Optional channel UUID. If omitted, first active channel is used.")

    parser.add_argument("--dry-run", action="store_true", help="Inspect DB/config/env and print planned actions. No paid API calls, no DB writes.")
    parser.add_argument("--confirm", action="store_true", help="Required for any real (non-dry-run) run, since it may call paid APIs.")

    parser.add_argument("--from-content", dest="from_content", metavar="CONTENT_ID", help="Resume from an existing parent content row.")
    parser.add_argument("--from-validation", dest="from_validation", metavar="CONTENT_ID", help="Resume after parent source script generation, at multilingual + SCRIPTS_VALIDATED + shorts planning.")
    parser.add_argument("--from-audio", dest="from_audio", metavar="CONTENT_ID", help="Resume at Agent 3 audio. Requires/checks SCRIPTS_VALIDATED.")
    parser.add_argument("--from-visuals", dest="from_visuals", metavar="CONTENT_ID", help="Resume at Agent 4 visuals. Requires/checks AUDIO_DONE.")
    parser.add_argument("--from-render", dest="from_render", metavar="CONTENT_ID", help="Resume at Agent 5 render. Requires/checks PARENT_VISUALS_DONE or CHILD_SHORT_VISUALS_DONE.")

    parser.add_argument("--force-scripts", action="store_true", help="Regenerate scripts even if valid scripts exist. Never deletes existing rows.")
    parser.add_argument("--force-audio", action="store_true", help="Regenerate audio even if AudioFile exists. Never deletes existing rows.")
    parser.add_argument("--force-visuals", action="store_true", help="Regenerate visuals even if VideoSections/status exist. Never deletes existing rows.")
    parser.add_argument("--force-render", action="store_true", help="Rerender even if VideoRender exists. Never deletes existing rows.")

    run(parser.parse_args())
