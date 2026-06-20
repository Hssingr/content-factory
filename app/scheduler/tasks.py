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
def run_agent2_for_channel(
    self,
    channel_id: str,
    rejected_stories: list[dict] | None = None,
) -> None:
    """Run Agent 2 discovery for one channel.

    Steps:
      1. Fetch the best new story (fetch → dedup → score → save Content)
      2. Send to Telegram for user approval (deterministic Python message, no Claude)

    Script generation happens in run_agent2_scripts_for_content after user approval.

    ``rejected_stories`` is forwarded from a previous run when the operator's manual
    story was a duplicate and the task is being re-dispatched with an expanded
    exclusion list.

    Args:
        channel_id:       UUID string of the target channel.
        rejected_stories: Optional pre-seeded exclusion list forwarded from the
                          discovery retry or manual-fallback handler.
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
        result = run_discovery(cid, db, rejected_stories=rejected_stories)
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


# ── On-demand: Agent 2 script generation + validation ───────────────────────

@celery_app.task(
    name="app.scheduler.tasks.run_agent2_scripts_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def run_agent2_scripts_for_content(self, content_id: str) -> None:
    """Run Agent 2 script workflow for approved content."""
    from app.database import _get_session_factory
    from app.agents.agent2_discovery.services.script_workflow import run_script_workflow
    from app.models import Content

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

        run_script_workflow(content, db)

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


# ── Compatibility alias for removed child-audio parent gate ───────────────────

@celery_app.task(name="app.scheduler.tasks.pickup_short_episodes_awaiting_parent")
def pickup_short_episodes_awaiting_parent() -> int:
    """Compatibility no-op for old queued Beat messages.

    V2 standalone shorts are written as ``SCRIPTS_VALIDATED`` by Agent 2 and are
    picked up by ``pickup_scripts_validated`` without waiting for parent audio.
    """
    logger.info(
        "CHILD_SHORT_PARENT_AUDIO_GATE_REMOVED task=pickup_short_episodes_awaiting_parent"
    )
    return 0


# ── Agent 3 — Audio Generation tasks ─────────────────────────────────────────


def ensure_child_short_audio_enqueued(
    parent_content_id: "uuid.UUID",
    db: "Session",
) -> int:
    """Compatibility no-op for the removed parent-audio child release hook."""
    logger.info(
        "CHILD_SHORT_PARENT_AUDIO_GATE_REMOVED helper=ensure_child_short_audio_enqueued "
        "parent_content_id=%s",
        parent_content_id,
    )
    return 0


@celery_app.task(name="app.scheduler.tasks.pickup_scripts_validated")
def pickup_scripts_validated() -> int:
    """Trigger Agent 3 audio generation for every content with status SCRIPTS_VALIDATED.

    Runs every 15 minutes. Atomically transitions each item to GENERATING_AUDIO
    inside ``run_agent3_audio_for_content`` so concurrent beats cannot double-process.

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
            run_agent3_audio_for_content.delay(str(content.id))
            dispatched += 1
            logger.info(
                "AUDIO_PICKUP content_id=%s is_short_episode=%s",
                content.id, bool(getattr(content, "is_short_episode", False)),
            )
    finally:
        db.close()

    if dispatched:
        logger.info("pickup_scripts_validated: %d task(s) dispatched", dispatched)
    return dispatched


@celery_app.task(
    name="app.scheduler.tasks.run_agent3_audio_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def run_agent3_audio_for_content(self, content_id: str) -> None:
    """Run the full Agent 3 audio generation pipeline for one content item.

    For each validated script language:
      1. ElevenLabs TTS → mp3 bytes
      2. Save to disk + measure exact duration with mutagen
      3. Whisper transcription → word-level timestamps
      4. Persist empty Shorts breakpoints for standalone-short architecture
      5. Persist AudioFile record; update Script with real values

    Sets ``content.status = "AUDIO_DONE"`` on full success,
    ``"FAILED"`` if all languages fail.

    Args:
        content_id: UUID string of content with status ``SCRIPTS_VALIDATED``.
    """
    from app.database import _get_session_factory
    from app.models import Content
    from app.agents.agent3_audio.services.audio import run_audio_generation

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
        logger.error("run_agent3_audio_for_content error for %s: %s", content_id, exc)
        db.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Max retries reached for Agent 3 audio of %s", content_id)
    finally:
        db.close()



@celery_app.task(
    name="app.scheduler.tasks.run_agent4_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def run_agent4_for_content(self, content_id: str) -> None:
    """Compatibility alias for the old Agent 3 audio Celery task name."""
    return run_agent3_audio_for_content.run(content_id)


# ── Agent 5 — Rendering tasks ─────────────────────────────────────────

@celery_app.task(name="app.scheduler.tasks.pickup_audio_done")
def pickup_audio_done() -> int:
    """Trigger Agent 5 render generation for every content with status AUDIO_DONE.

    Runs every 15 minutes. Atomically transitions each item to GENERATING_VIDEO
    inside ``run_agent5_render_for_content`` so concurrent beats cannot double-process.

    Returns:
        Number of video generation tasks dispatched.
    """
    from app.database import _get_session_factory
    from app.models import AudioFile, Content, VideoRender

    db = _get_session_factory()()
    dispatched = 0
    try:
        ready = db.query(Content).filter(Content.status == "AUDIO_DONE").all()
        for content in ready:
            has_audio = (
                db.query(AudioFile)
                .filter(AudioFile.content_id == content.id)
                .limit(1)
                .first()
            ) is not None
            if not has_audio:
                logger.info("RENDER_PICKUP_SKIP content_id=%s reason=audio_missing", content.id)
                continue

            render_format = "short" if bool(getattr(content, "is_short_episode", False)) else "main"
            has_render = (
                db.query(VideoRender)
                .filter(VideoRender.content_id == content.id, VideoRender.format == render_format)
                .limit(1)
                .first()
            ) is not None
            if has_render:
                logger.info(
                    "RENDER_PICKUP_SKIP content_id=%s reason=render_exists format=%s",
                    content.id, render_format,
                )
                continue

            run_agent5_render_for_content.delay(str(content.id))
            dispatched += 1
    finally:
        db.close()

    if dispatched:
        logger.info("pickup_audio_done: %d task(s) dispatched", dispatched)
    return dispatched


@celery_app.task(
    name="app.scheduler.tasks.run_agent5_render_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def run_agent5_render_for_content(self, content_id: str) -> None:
    """Run the full Agent 5 render generation pipeline for one content item.

    For each validated audio language:
      1. Storyboard        — map script to timed visual beats
      2. Section Validator — validate/enrich visual beats
      3. Save video_sections to DB
      4. Flux generator    — create/cache local images
      5. Subtitles         — standard (main) + karaoke (Shorts)
      6. Remotion builder  — write JSON props files
      7. Remotion renderer — render MP4s, save VideoRender records

    Sets ``content.status = "VIDEO_DONE"`` on full success,
    ``"FAILED"`` if all languages fail.

    Args:
        content_id: UUID string of content with status ``AUDIO_DONE``.
    """
    from app.database import _get_session_factory
    from app.models import Content
    from app.agents.agent5_render.services.video import run_video_generation

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
        logger.error("run_agent5_render_for_content error for %s: %s", content_id, exc)
        db.rollback()
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Max retries reached for Agent 5 render of %s", content_id)
    finally:
        db.close()


@celery_app.task(
    name="app.scheduler.tasks.run_agent5_for_content",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def run_agent5_for_content(self, content_id: str) -> None:
    """Compatibility alias for the old Agent 5 video-generation Celery task name."""
    return run_agent5_render_for_content.run(content_id)
