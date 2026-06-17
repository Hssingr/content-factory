"""Celery task definitions for the content pipeline.

All tasks use lazy imports inside their function bodies to:
  - Avoid circular imports (tasks → scheduler → tasks)
  - Ensure DB connections are created fresh per worker process
  - Keep startup fast when the Celery app is imported for other purposes

Beat schedule is defined in app/scheduler/__init__.py.

Workers start with:
    celery -A app.scheduler worker --loglevel=info

Beat starts with:
    celery -A app.scheduler beat --loglevel=info
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings
from app.scheduler import celery_app

logger = logging.getLogger(__name__)

_MAX_CORRECTIONS = 3   # auto-correct rounds before escalating to quality rewrite


# ── Periodic: dispatch discovery ─────────────────────────────────────────────

@celery_app.task(name="app.scheduler.tasks.dispatch_discovery")
def dispatch_discovery() -> int:
    """Find active channels that are due for content discovery and fire a task for each.

    A channel is considered *due* when it has no content yet, or when the time
    elapsed since its last Content record exceeds its configured inter-run interval
    (7 × 24h ÷ videos_per_week).

    Returns:
        Number of discovery tasks dispatched.
    """
    from app.database import _get_session_factory
    from app.models import Channel, ChannelConfig, Content

    db = _get_session_factory()()
    dispatched = 0
    try:
        channels = db.query(Channel).filter(Channel.active.is_(True)).all()
        now = datetime.now(timezone.utc)

        for channel in channels:
            config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
            vpw = config.videos_per_week if config else 3
            interval_hours = (7 * 24) / max(vpw, 1)

            latest: Content | None = (
                db.query(Content)
                .filter(Content.channel_id == channel.id)
                .order_by(Content.created_at.desc())
                .first()
            )

            if latest is None:
                due = True
            else:
                age_h = (now - latest.created_at).total_seconds() / 3600
                due = age_h >= interval_hours

            if due:
                run_agent2_for_channel.delay(str(channel.id))
                dispatched += 1
                logger.info("Discovery dispatched for channel %s", channel.id)

    finally:
        db.close()

    logger.info("dispatch_discovery: %d channel(s) triggered", dispatched)
    return dispatched


# ── Periodic: validation timeout sweep ───────────────────────────────────────

@celery_app.task(name="app.scheduler.tasks.check_validation_timeouts")
def check_validation_timeouts() -> int:
    """Auto-approve or mark NEEDS_REVIEW for every expired PENDING validation.

    Returns:
        Number of validations processed.
    """
    from app.database import _get_session_factory
    from app.agents.agent2_discovery.services.validation import (
        check_validation_timeouts as _sweep,
    )

    db = _get_session_factory()()
    try:
        return _sweep(db)
    finally:
        db.close()


# ── Periodic: pick up APPROVED content ───────────────────────────────────────

@celery_app.task(name="app.scheduler.tasks.pickup_approved_content")
def pickup_approved_content() -> int:
    """Trigger script generation for every content still in APPROVED status.

    ``run_agent2_scripts_for_content`` sets status → GENERATING_SCRIPTS atomically
    at its start, so concurrent workers won't double-process the same content.

    Returns:
        Number of tasks dispatched.
    """
    from app.database import _get_session_factory
    from app.models import Content

    db = _get_session_factory()()
    dispatched = 0
    try:
        approved = (
            db.query(Content)
            .filter(Content.status == "APPROVED")
            .all()
        )
        for content in approved:
            run_agent2_scripts_for_content.delay(str(content.id))
            dispatched += 1
    finally:
        db.close()

    if dispatched:
        logger.info("pickup_approved_content: %d task(s) dispatched", dispatched)
    return dispatched


# ── On-demand: Agent 2 Phase A — discovery + Telegram ────────────────────────

@celery_app.task(
    name="app.scheduler.tasks.run_agent2_for_channel",
    bind=True,
    max_retries=2,
    default_retry_delay=300,   # 5 minutes between retries
)
def run_agent2_for_channel(self, channel_id: str) -> None:
    """Run Agent 2 discovery phase for one channel.

    Steps:
      1. Fetch the best new story (fetch → dedup → score → save Content)
      2. Send to Telegram for user approval (deterministic Python message, no Claude)

    Script generation happens in run_agent2_scripts_for_content after user approval.

    Args:
        channel_id: UUID string of the target channel.
    """
    from app.database import _get_session_factory
    from app.models import Channel, ChannelLanguage
    from app.agents.agent2_discovery.services.discovery import run_discovery
    from app.agents.agent2_discovery.services.validation import send_for_validation

    cid = uuid.UUID(channel_id)
    db = _get_session_factory()()
    try:
        channel: Channel | None = db.get(Channel, cid)
        if not channel or not channel.active:
            logger.info("Channel %s not found or inactive — skipping", channel_id)
            return

        # ── 1. Discover (fetch → dedup → score → persist) ────────────────────
        result = run_discovery(cid, db)
        if result is None:
            logger.info("No new story found for channel %s", channel_id)
            return

        content, story, story_assessment = result

        # ── 2. Send to Telegram — no scripts generated yet ───────────────────
        target_languages = [
            cl.language
            for cl in db.query(ChannelLanguage)
            .filter(ChannelLanguage.channel_id == channel.id)
            .all()
        ]
        send_for_validation(
            content, channel, db,
            assessment=story_assessment,
            target_languages=target_languages,
        )

    except Exception as exc:
        logger.error("run_agent2_for_channel error for %s: %s", channel_id, exc)
        db.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Max retries reached for channel %s — giving up", channel_id)
    finally:
        db.close()


# ── On-demand: Agent 2 Phase B — script generation + validation ───────────────

@celery_app.task(
    name="app.scheduler.tasks.run_agent2_scripts_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def run_agent2_scripts_for_content(self, content_id: str) -> None:
    """Generate, validate, and adapt scripts for approved content.

    Only processes content with ``status="APPROVED"``. Steps:
      1. Reconstruct story proxy from Content.source_excerpt
      2. Generate source-language scripts (Claude)
      3. Script Quality Gate (retention review + optional rewrite)
      4. Intro Optimizer (best hook)
      5. Deterministic checks + auto-correct loop (up to 3 rounds, Sonnet)
      6. If still MAJOR after 3 rounds: one final quality rewrite
      7. Persist source Script (with estimated duration, breakpoints, validated=True)
      8. Generate multilingual scripts for all target languages
      9. Set duration + breakpoints + validated=True on all language scripts
      10. content.status = "SCRIPTS_VALIDATED"

    Args:
        content_id: UUID string of content with status ``"APPROVED"``.
    """
    from app.database import _get_session_factory
    from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script
    from app.agents.agent2_discovery.services.scripts import (
        run_script_quality_gate,
        generate_multilingual_scripts,
    )
    from app.agents.agent2_discovery.services.story import Story
    from app.agents.agent2_discovery.system_prompt import (
        generate_scripts,
        optimize_intro,
        auto_correct_script,
    )
    from app.services.script_checks import run_deterministic_checks
    from app.services.script_estimator import estimate_duration_sec, compute_shorts_breakpoints

    cid = uuid.UUID(content_id)
    db = _get_session_factory()()
    try:
        content: Content | None = db.get(Content, cid)
        if not content:
            logger.warning("Content %s not found — skipping", content_id)
            return
        if content.status != "APPROVED":
            logger.debug(
                "Content %s status=%s — skipping script generation",
                content_id, content.status,
            )
            return

        channel: Channel | None = db.get(Channel, content.channel_id)
        if not channel:
            logger.error("Channel not found for content %s", content_id)
            return

        config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
        script_format = config.script_format if config else "youtube_long"
        audio_tags_enabled = config.audio_tags_enabled if config else False
        shorts_rule = config.shorts_rule if config else "auto"

        # ── Resolve source-language voice ─────────────────────────────────────
        src_voice: ChannelVoice | None = (
            db.query(ChannelVoice)
            .filter(
                ChannelVoice.channel_id == channel.id,
                ChannelVoice.language == content.source_language,
            )
            .first()
        )
        if not src_voice:
            src_voice = (
                db.query(ChannelVoice)
                .filter(ChannelVoice.channel_id == channel.id)
                .first()
            )
            if src_voice:
                logger.info(
                    "No voice for source lang=%s — using %s voice for TTS block",
                    content.source_language, src_voice.language,
                )

        tts_model    = src_voice.tts_model if src_voice else "sonic-2"
        tts_provider = src_voice.provider if src_voice else "cartesia"

        # ── Build voice map for multilingual duration estimates ───────────────
        voice_map: dict[str, ChannelVoice] = {
            v.language: v
            for v in db.query(ChannelVoice).filter(ChannelVoice.channel_id == channel.id).all()
        }

        # ── Reconstruct story proxy from Content fields ───────────────────────
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

        # ── Distributed lock: mark as in-progress ────────────────────────────
        content.status = "GENERATING_SCRIPTS"
        db.commit()

        logger.info(
            "Generating scripts for content %s… (format=%s provider=%s model=%s)",
            content.id, script_format, tts_provider, tts_model,
        )

        # ── Step 1: Generate source-language scripts ──────────────────────────
        scripts = generate_scripts(
            story, channel,
            script_format=script_format,
            audio_tags_enabled=audio_tags_enabled,
            tts_model=tts_model,
            tts_provider=tts_provider,
        )

        # ── Step 2: Script Quality Gate (retention review) ────────────────────
        scripts = run_script_quality_gate(
            scripts, channel,
            script_format=script_format,
            language=content.source_language,
            tts_model=tts_model,
            tts_provider=tts_provider,
        )

        # ── Step 3: Intro Optimizer ───────────────────────────────────────────
        scripts = optimize_intro(scripts, channel, script_format=script_format)

        hook_excerpt = scripts.get("voice_script", "").strip()[:300].replace("\n", " ")
        logger.info("Final script hook (first 300 chars) for content %s: %r", content.id, hook_excerpt)

        # ── Step 4: Deterministic checks + auto-correct loop ─────────────────
        scripts_by_lang = {
            content.source_language: {
                "video_script": scripts["video_script"],
                "voice_script": scripts["voice_script"],
            }
        }

        all_clear = False
        for round_n in range(1, _MAX_CORRECTIONS + 1):
            issues_by_lang = run_deterministic_checks(scripts_by_lang, script_format)
            lang_issues = issues_by_lang.get(content.source_language, [])
            major = [i for i in lang_issues if i["severity"] == "MAJOR"]

            if not major:
                logger.info(
                    "Det checks: PASSED on round %d for content %s", round_n, content.id
                )
                all_clear = True
                break

            logger.info(
                "Det checks: %d MAJOR issue(s) on round %d for content %s — auto-correcting",
                len(major), round_n, content.id,
            )
            try:
                corrected = auto_correct_script(
                    current_scripts=scripts_by_lang[content.source_language],
                    issues=major,
                    language=content.source_language,
                    channel=channel,
                    script_format=script_format,
                    source_excerpt=content.source_excerpt,
                    tts_model=tts_model,
                    tts_provider=tts_provider,
                )
                scripts_by_lang[content.source_language] = corrected
                scripts = {**scripts, **corrected}
            except Exception as exc:
                logger.error(
                    "Auto-correct round %d failed for content %s: %s — stopping correction loop",
                    round_n, content.id, exc,
                )
                break

        if not all_clear:
            # Still MAJOR after all rounds — one final quality rewrite
            logger.warning(
                "Still MAJOR after %d correction rounds for content %s — running quality rewrite",
                _MAX_CORRECTIONS, content.id,
            )
            try:
                scripts = run_script_quality_gate(
                    scripts, channel,
                    script_format=script_format,
                    language=content.source_language,
                    tts_model=tts_model,
                    tts_provider=tts_provider,
                )
                scripts_by_lang[content.source_language] = {
                    "video_script": scripts["video_script"],
                    "voice_script": scripts["voice_script"],
                }
            except Exception as exc:
                logger.error(
                    "Final quality rewrite failed for content %s: %s — proceeding with latest",
                    content.id, exc,
                )

        # ── Step 5: Persist source Script ─────────────────────────────────────
        content.title = scripts.get("title", content.title)
        src_voice_script = scripts.get("voice_script", "")
        src_dur_sec = estimate_duration_sec(src_voice_script, content.source_language)
        src_breakpoints = compute_shorts_breakpoints(src_voice_script, src_dur_sec, shorts_rule)

        script_record = Script(
            content_id=content.id,
            language=content.source_language,
            video_script=scripts["video_script"],
            voice_script=src_voice_script,
            version=1,
            validated=True,
            estimated_duration_sec=src_dur_sec,
            shorts_breakpoints=src_breakpoints,
        )
        db.add(script_record)
        db.commit()
        logger.info(
            "Source script saved for content %s — lang=%s dur=%.1fs",
            content.id, content.source_language, src_dur_sec,
        )

        # ── Step 6: Generate multilingual scripts ─────────────────────────────
        generate_multilingual_scripts(content, channel, db, audio_tags_enabled=audio_tags_enabled)

        # ── Step 7: Duration + breakpoints for all multilingual scripts ────────
        db.refresh(content)
        all_scripts: list[Script] = (
            db.query(Script).filter(Script.content_id == content.id).all()
        )
        for s in all_scripts:
            if s.language == content.source_language:
                continue   # already set above
            lang_voice = voice_map.get(s.language)
            dur = estimate_duration_sec(s.voice_script, s.language)
            bp  = compute_shorts_breakpoints(s.voice_script, dur, shorts_rule)
            s.estimated_duration_sec = dur
            s.shorts_breakpoints = bp
            s.validated = True
            logger.info(
                "Duration set for lang=%s content %s: %.1fs",
                s.language, content.id, dur,
            )
        db.commit()

        # ── Step 8: Set final status ──────────────────────────────────────────
        content.status = "SCRIPTS_VALIDATED"
        db.commit()
        logger.info("Content %s — SCRIPTS_VALIDATED", content.id)

    except Exception as exc:
        logger.error("run_agent2_scripts_for_content error for %s: %s", content_id, exc)
        db.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Max retries reached for content %s scripts — giving up", content_id)
    finally:
        db.close()


# ── Publish timing helpers ────────────────────────────────────────────────────

_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def next_publish_datetime(timing, now: datetime) -> datetime:
    """Return the next UTC datetime when a channel should publish.

    Iterates forward from ``now`` to find the earliest upcoming weekday listed
    in ``timing.optimal_days`` at hour ``optimal_hour_start`` in the timing's
    timezone.

    Args:
        timing: ``ChannelPublishTiming`` ORM instance.
        now:    Current UTC datetime (timezone-aware).

    Returns:
        Timezone-aware UTC datetime for the next publish slot.
        Falls back to ``now + 7 days`` if no matching day is found or the
        timezone is invalid.
    """
    try:
        tz = ZoneInfo(timing.timezone or "UTC")
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone '%s' — using UTC", timing.timezone)
        tz = ZoneInfo("UTC")

    local_now = now.astimezone(tz)
    target_days = {_WEEKDAY_MAP[d] for d in (timing.optimal_days or []) if d in _WEEKDAY_MAP}

    for offset in range(8):
        candidate = local_now + timedelta(days=offset)
        if candidate.weekday() in target_days:
            publish_local = candidate.replace(
                hour=timing.optimal_hour_start or 18,
                minute=0, second=0, microsecond=0,
            )
            if publish_local > local_now:
                return publish_local.astimezone(timezone.utc)

    return now + timedelta(days=7)   # fallback


# ── Periodic: schedule content creation (D-1) ────────────────────────────────

@celery_app.task(name="app.scheduler.tasks.schedule_content_creation")
def schedule_content_creation() -> int:
    """Trigger content discovery at the user's chosen D-1 hour, the day before each publish slot.

    Runs every hour. For each active channel:
      1. Load the channel owner's ``pipeline_run_hour`` and ``pipeline_timezone``.
      2. Check whether the current local hour matches ``pipeline_run_hour``.
      3. Check whether the next publish slot falls on tomorrow (local date).
      4. If both: fire ``run_agent2_for_channel`` and create a ``PublishSchedule`` placeholder.

    Returns:
        Number of discovery tasks dispatched.
    """
    from app.database import _get_session_factory
    from app.models import Channel, ChannelPublishTiming, Content, PublishSchedule, User

    db = _get_session_factory()()
    dispatched = 0
    now = datetime.now(timezone.utc)

    try:
        timings: list[ChannelPublishTiming] = (
            db.query(ChannelPublishTiming)
            .join(Channel, Channel.id == ChannelPublishTiming.channel_id)
            .filter(Channel.active.is_(True))
            .all()
        )

        seen_channels: set[uuid.UUID] = set()

        for timing in timings:
            channel_id = timing.channel_id
            if channel_id in seen_channels:
                continue

            channel: Channel | None = db.get(Channel, channel_id)
            if not channel:
                continue
            user: User | None = db.get(User, channel.user_id)
            if not user:
                continue

            try:
                user_tz = ZoneInfo(user.pipeline_timezone or "UTC")
            except ZoneInfoNotFoundError:
                user_tz = ZoneInfo("UTC")

            local_now = now.astimezone(user_tz)

            if local_now.hour != user.pipeline_run_hour:
                continue

            next_dt = next_publish_datetime(timing, now)
            local_tomorrow = (local_now + timedelta(days=1)).date()
            next_dt_local  = next_dt.astimezone(user_tz)

            if next_dt_local.date() != local_tomorrow:
                continue

            logger.info(
                "D-1 trigger: channel=%s user=%s run_hour=%dh (%s) → publish %s",
                channel_id, user.id, user.pipeline_run_hour,
                user.pipeline_timezone, next_dt_local.strftime("%A %Y-%m-%d %H:%M"),
            )

            in_progress = (
                db.query(Content)
                .filter(
                    Content.channel_id == channel_id,
                    Content.status.in_([
                        "PENDING_APPROVAL", "APPROVED",
                        "GENERATING_SCRIPTS", "SCRIPTS_VALIDATED",
                    ]),
                )
                .first()
            )
            if in_progress:
                logger.debug("Channel %s already has content in progress — skipping D-1", channel_id)
                continue

            run_agent2_for_channel.delay(str(channel_id))

            from app.models import ChannelPlatform
            platforms = (
                db.query(ChannelPlatform)
                .filter(
                    ChannelPlatform.channel_id == channel_id,
                    ChannelPlatform.language == timing.language,
                    ChannelPlatform.verified.is_(True),
                )
                .all()
            )
            for plat in platforms:
                existing = (
                    db.query(PublishSchedule)
                    .filter(
                        PublishSchedule.content_id == None,  # noqa: E711
                        PublishSchedule.platform == plat.platform,
                        PublishSchedule.language == timing.language,
                        PublishSchedule.scheduled_at == next_dt,
                    )
                    .first()
                )
                if not existing:
                    db.add(PublishSchedule(
                        content_id=None,
                        platform=plat.platform,
                        language=timing.language,
                        scheduled_at=next_dt,
                        proxy_region=timing.language,
                        status="SCHEDULED",
                    ))

            db.commit()
            seen_channels.add(channel_id)
            dispatched += 1
            logger.info("D-1 triggered for channel %s — publish at %s", channel_id, next_dt)

    finally:
        db.close()

    logger.info("schedule_content_creation: %d channel(s) dispatched", dispatched)
    return dispatched


# ── Periodic: dispatch publishing (D-day placeholder) ────────────────────────

@celery_app.task(name="app.scheduler.tasks.dispatch_publishing")
def dispatch_publishing() -> int:
    """Log publish_schedule rows due in the next 30 minutes.

    Placeholder for Agent 7 — actual platform uploads are not yet implemented.

    Returns:
        Number of rows found due for publishing.
    """
    from app.database import _get_session_factory
    from app.models import PublishSchedule

    db = _get_session_factory()()
    count = 0
    try:
        soon = datetime.now(timezone.utc) + timedelta(minutes=30)
        due: list[PublishSchedule] = (
            db.query(PublishSchedule)
            .filter(
                PublishSchedule.status == "SCHEDULED",
                PublishSchedule.scheduled_at <= soon,
                PublishSchedule.content_id.is_not(None),
            )
            .all()
        )
        for ps in due:
            logger.info(
                "TODO Agent 7: publish content=%s platform=%s lang=%s at %s",
                ps.content_id, ps.platform, ps.language, ps.scheduled_at,
            )
            count += 1
    finally:
        db.close()

    return count


# ── Agent 4 — Audio Generation tasks ─────────────────────────────────────────

@celery_app.task(name="app.scheduler.tasks.pickup_scripts_validated")
def pickup_scripts_validated() -> int:
    """Trigger Agent 4 audio generation for every content with status SCRIPTS_VALIDATED.

    Runs every 15 minutes. Atomically transitions each item to GENERATING_AUDIO
    inside ``run_agent4_for_content`` so concurrent beats cannot double-process.

    Returns:
        Number of audio generation tasks dispatched.
    """
    from app.database import _get_session_factory
    from app.models import Content

    db = _get_session_factory()()
    dispatched = 0
    try:
        validated = db.query(Content).filter(Content.status == "SCRIPTS_VALIDATED").all()
        for content in validated:
            run_agent4_for_content.delay(str(content.id))
            dispatched += 1
    finally:
        db.close()

    if dispatched:
        logger.info("pickup_scripts_validated: %d task(s) dispatched", dispatched)
    return dispatched


@celery_app.task(
    name="app.scheduler.tasks.run_agent4_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def run_agent4_for_content(self, content_id: str) -> None:
    """Run the full Agent 4 audio generation pipeline for one content item.

    For each validated script language:
      1. ElevenLabs TTS → mp3 bytes
      2. Save to disk + measure exact duration with mutagen
      3. Whisper transcription → word-level timestamps
      4. Recalculate Shorts breakpoints from real timestamps
      5. Persist AudioFile record; update Script with real values

    Sets ``content.status = "AUDIO_DONE"`` on full success,
    ``"FAILED"`` if all languages fail.

    Args:
        content_id: UUID string of content with status ``SCRIPTS_VALIDATED``.
    """
    from app.database import _get_session_factory
    from app.models import Content
    from app.agents.agent4_audio.services.audio import run_audio_generation

    cid = uuid.UUID(content_id)
    db = _get_session_factory()()
    try:
        content: Content | None = db.get(Content, cid)
        if not content:
            logger.warning("Content %s not found — skipping", content_id)
            return
        if content.status not in ("SCRIPTS_VALIDATED", "AUDIO_DONE"):
            logger.debug(
                "Content %s status=%s — skipping audio generation",
                content_id, content.status,
            )
            return

        run_audio_generation(cid, db)

    except Exception as exc:
        logger.error("run_agent4_for_content error for %s: %s", content_id, exc)
        db.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Max retries reached for Agent 4 audio of %s", content_id)
    finally:
        db.close()


# ── Agent 5 — Video Generation tasks ─────────────────────────────────────────

@celery_app.task(name="app.scheduler.tasks.pickup_audio_done")
def pickup_audio_done() -> int:
    """Trigger Agent 5 video generation for every content with status AUDIO_DONE.

    Runs every 15 minutes. Atomically transitions each item to GENERATING_VIDEO
    inside ``run_agent5_for_content`` so concurrent beats cannot double-process.

    Returns:
        Number of video generation tasks dispatched.
    """
    from app.database import _get_session_factory
    from app.models import Content

    db = _get_session_factory()()
    dispatched = 0
    try:
        ready = db.query(Content).filter(Content.status == "AUDIO_DONE").all()
        for content in ready:
            run_agent5_for_content.delay(str(content.id))
            dispatched += 1
    finally:
        db.close()

    if dispatched:
        logger.info("pickup_audio_done: %d task(s) dispatched", dispatched)
    return dispatched


@celery_app.task(
    name="app.scheduler.tasks.run_agent5_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def run_agent5_for_content(self, content_id: str) -> None:
    """Run the full Agent 5 video generation pipeline for one content item.

    For each validated audio language:
      1. Section Splitter   — parse script → timed sections
      2. Section Validator  — validate/enrich sections (Claude, up to 3 rounds)
      3. Save video_sections to DB
      4. Stock fetcher      — fetch actual image/video URLs
      5. Assembly Validator — validate media relevance (Claude, 1 pass)
      6. Shorts Cutter      — group sections into Short segments
      7. Subtitles          — standard (main) + karaoke (Shorts)
      8. Remotion builder   — write JSON props files
      9. Remotion renderer  — render MP4s, save VideoRender records

    Sets ``content.status = "VIDEO_DONE"`` on full success,
    ``"FAILED"`` if all languages fail.

    Args:
        content_id: UUID string of content with status ``AUDIO_DONE``.
    """
    from app.database import _get_session_factory
    from app.models import Content
    from app.agents.agent5_video.services.video import run_video_generation

    cid = uuid.UUID(content_id)
    db = _get_session_factory()()
    try:
        content: Content | None = db.get(Content, cid)
        if not content:
            logger.warning("Content %s not found — skipping", content_id)
            return
        if content.status not in ("AUDIO_DONE", "GENERATING_VIDEO"):
            logger.debug(
                "Content %s status=%s — skipping video generation",
                content_id, content.status,
            )
            return

        run_video_generation(cid, db)

    except Exception as exc:
        logger.error("run_agent5_for_content error for %s: %s", content_id, exc)
        db.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Max retries reached for Agent 5 video of %s", content_id)
    finally:
        db.close()
