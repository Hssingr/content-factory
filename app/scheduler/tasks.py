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
    """Generate, validate, and adapt scripts for approved content.

    Only processes content with ``status="APPROVED"``. Script-generation flow:
      1. Reconstruct story proxy from Content.source_excerpt
      2. Generate narrative blueprint (hook, major_turns, payoff, comment_trigger)
      3. Persist blueprint to content.story_blueprint
      4. Generate source-language scripts section-by-section via generate_script_sections()
         (per-section TTS/hook checks + retry loop inside)
      5. Script Quality Gate (retention review + optional rewrite)
      6. Persist source Script (estimated duration, validated=True)
      7. Merge visual_intent_history into content.story_blueprint
      8. Generate multilingual scripts for all target languages
      9. Set duration + validated=True on all language scripts
      10. content.status = "SCRIPTS_VALIDATED"

    Args:
        content_id: UUID string of content with status ``"APPROVED"``.
    """
    from app.database import _get_session_factory
    from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script
    from app.agents.agent2_discovery.services.scripts import (
        run_script_quality_gate,
        generate_multilingual_scripts,
        generate_script_sections,
        run_shorts_planner,
        _script_trace,
    )
    from app.agents.agent2_discovery.services.story import Story
    from app.agents.agent2_discovery.system_prompt import generate_story_blueprint
    from app.services.script_estimator import estimate_duration_sec

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
        script_format      = config.script_format      if config else "youtube_long"
        audio_tags_enabled = config.audio_tags_enabled if config else False

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
        tts_provider = src_voice.provider  if src_voice else "cartesia"

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

        # ── Mark as in-progress ───────────────────────────────────────────────
        content.status = "GENERATING_SCRIPTS"
        db.commit()

        logger.info(
            "Generating scripts for content %s… (format=%s provider=%s model=%s)",
            content.id, script_format, tts_provider, tts_model,
        )

        # ── Step 1: Generate narrative blueprint ──────────────────────────────
        blueprint = generate_story_blueprint(story, channel, script_format=script_format)
        logger.info(
            "Blueprint generated for content %s — %d major_turns, suggested_sections=%d",
            content.id, len(blueprint.get("major_turns", [])),
            blueprint.get("suggested_section_count", 3),
        )

        # ── Step 2: Persist blueprint ─────────────────────────────────────────
        content.story_blueprint = blueprint
        db.commit()

        # ── Step 3: Generate scripts section-by-section ───────────────────────
        scripts = generate_script_sections(
            story=story,
            blueprint=blueprint,
            channel=channel,
            channel_voice=src_voice,
            script_format=script_format,
            audio_tags_enabled=audio_tags_enabled,
        )

        hook_excerpt = scripts.get("voice_script", "").strip()[:300].replace("\n", " ")
        logger.info("Script hook (first 300 chars) for content %s: %r", content.id, hook_excerpt)

        # ── Step 4: Script Quality Gate (retention review) ────────────────────
        scripts = run_script_quality_gate(
            scripts, channel,
            script_format=script_format,
            language=content.source_language,
            tts_model=tts_model,
            tts_provider=tts_provider,
        )
        _script_trace("tasks_post_quality_gate", scripts.get("voice_script", ""))

        # ── Step 5: Persist source Script ─────────────────────────────────────
        content.title = scripts.get("title", content.title)
        src_voice_script = scripts.get("voice_script", "")
        src_dur_sec = estimate_duration_sec(src_voice_script, content.source_language)

        script_record = Script(
            content_id=content.id,
            language=content.source_language,
            video_script=scripts["video_script"],
            voice_script=src_voice_script,
            version=1,
            validated=True,
            estimated_duration_sec=src_dur_sec,
        )
        db.add(script_record)
        db.commit()
        logger.info(
            "Source script saved for content %s — lang=%s dur=%.1fs",
            content.id, content.source_language, src_dur_sec,
        )

        # ── Step 6: Merge visual_intent_history into story_blueprint ──────────
        visual_history = scripts.get("visual_intent_history")
        if visual_history and content.story_blueprint:
            content.story_blueprint = {
                **content.story_blueprint,
                "visual_intent_history": visual_history,
            }
            db.commit()

        # ── Step 7: Generate multilingual scripts ─────────────────────────────
        # sha256 here must match quality_gate_final trace — different hash = stale script bug
        _script_trace("tasks_entering_multilingual", src_voice_script)
        generate_multilingual_scripts(content, channel, db, audio_tags_enabled=audio_tags_enabled)

        # ── Step 8: Duration for all multilingual scripts ─────────────────────
        db.refresh(content)
        all_scripts: list[Script] = (
            db.query(Script).filter(Script.content_id == content.id).all()
        )
        for s in all_scripts:
            if s.language == content.source_language:
                continue   # already set above
            dur = estimate_duration_sec(s.voice_script, s.language)
            s.estimated_duration_sec = dur
            s.validated              = True
            logger.info(
                "Duration set for lang=%s content %s: %.1fs",
                s.language, content.id, dur,
            )
        db.commit()

        # ── Step 9: Set final status ──────────────────────────────────────────
        content.status = "SCRIPTS_VALIDATED"
        db.commit()
        logger.info("Content %s — SCRIPTS_VALIDATED", content.id)

        # ── Step 10: Shorts Planner (non-blocking — failure never affects parent) ──
        try:
            run_shorts_planner(content.id, channel, config, db)
        except Exception as shorts_exc:
            logger.error(
                "run_shorts_planner failed for content %s (non-blocking): %s",
                content.id, shorts_exc,
            )

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


# ── Agent 2→3 gate: release Short episode scripts when parent reaches AUDIO_DONE ──

@celery_app.task(name="app.scheduler.tasks.pickup_short_episodes_awaiting_parent")
def pickup_short_episodes_awaiting_parent() -> int:
    """Flip/enqueue Short episodes whose parent has reached AUDIO_DONE.

    Handles two cases in a single pass:
      - ``SCRIPTS_VALIDATED_AWAITING_PARENT`` → flip to ``SCRIPTS_VALIDATED``, enqueue
        Agent 3 with ``CHILD_SHORT_AUDIO_ENQUEUED``.
      - ``SCRIPTS_VALIDATED`` (pre-existing stranded children) → enqueue Agent 3
        directly with ``CHILD_SHORT_AUDIO_ENQUEUED``.

    Children already in ``GENERATING_AUDIO``, ``AUDIO_DONE``, or further are not
    included in the query and are never re-enqueued.

    Condition: parent status in (AUDIO_DONE, GENERATING_VIDEO, VIDEO_DONE).

    Returns:
        Number of Short episodes flipped from AWAITING_PARENT → SCRIPTS_VALIDATED.
    """
    from app.database import _get_session_factory
    from app.models import Content

    db = _get_session_factory()()
    released = 0
    try:
        # Single query: all short episodes that are ready to move forward.
        actionable: list[Content] = (
            db.query(Content)
            .filter(
                Content.is_short_episode.is_(True),
                Content.status.in_([
                    "SCRIPTS_VALIDATED_AWAITING_PARENT",
                    "SCRIPTS_VALIDATED",
                ]),
            )
            .all()
        )

        newly_released_ids: set = set()
        to_enqueue: list[Content] = []

        for short in actionable:
            if not short.parent_content_id:
                logger.warning(
                    "pickup_short_episodes_awaiting_parent: Short episode content=%s "
                    "has no parent_content_id — cannot gate; leaving in current status",
                    short.id,
                )
                continue
            parent: Content | None = db.get(Content, short.parent_content_id)
            if not parent or parent.status not in ("AUDIO_DONE", "GENERATING_VIDEO", "VIDEO_DONE"):
                continue

            if short.status == "SCRIPTS_VALIDATED_AWAITING_PARENT":
                short.status = "SCRIPTS_VALIDATED"
                released += 1
                newly_released_ids.add(short.id)
                logger.info(
                    "CHILD_SHORT_RELEASED child_content_id=%s part=%d/%d "
                    "(parent=%s status=%s) → SCRIPTS_VALIDATED",
                    short.id,
                    short.short_part_number or 0,
                    short.short_total_parts or 0,
                    short.parent_content_id,
                    parent.status,
                )

            to_enqueue.append(short)

        db.commit()

        # Log newly released grouped by parent
        if newly_released_ids:
            from collections import defaultdict as _defaultdict
            _by_parent: dict = _defaultdict(list)
            for s in to_enqueue:
                if s.id in newly_released_ids:
                    _by_parent[str(s.parent_content_id)].append(s)
            for _parent_id, _shorts in _by_parent.items():
                logger.info(
                    "CHILD_SHORTS_RELEASED parent_content_id=%s count=%d",
                    _parent_id, len(_shorts),
                )

        # Enqueue Agent 3 immediately for all actionable children
        for s in to_enqueue:
            run_agent3_audio_for_content.delay(str(s.id))
            if s.id in newly_released_ids:
                logger.info(
                    "CHILD_SHORT_AUDIO_ENQUEUED child_content_id=%s part=%d/%d",
                    s.id,
                    s.short_part_number or 0,
                    s.short_total_parts or 0,
                )
            else:
                logger.info(
                    "CHILD_SHORT_AUDIO_ENQUEUED child_content_id=%s part=%d/%d",
                    s.id,
                    s.short_part_number or 0,
                    s.short_total_parts or 0,
                )

    finally:
        db.close()

    if released:
        logger.info("pickup_short_episodes_awaiting_parent: %d Short episode(s) released", released)
    return released


# ── Agent 3 — Audio Generation tasks ─────────────────────────────────────────


def ensure_child_short_audio_enqueued(
    parent_content_id: "uuid.UUID",
    db: "Session",
) -> int:
    """Release/enqueue Agent 3 for every child short episode of a parent.

    Called immediately after the parent reaches AUDIO_DONE inside
    ``run_agent3_audio_for_content``, reusing the parent's own DB session to avoid
    transaction-isolation races that arise with a second session.

    Child status handling:
      - ``SCRIPTS_VALIDATED_AWAITING_PARENT`` → flip to ``SCRIPTS_VALIDATED``,
        then enqueue (logs ``CHILD_SHORT_RELEASED``).
      - ``SCRIPTS_VALIDATED`` + no ``AudioFile`` → enqueue directly
        (logs ``CHILD_SHORT_AUDIO_ENQUEUED``).
      - ``SCRIPTS_VALIDATED`` + ``AudioFile`` exists → skip
        (logs ``CHILD_SHORT_AUDIO_ALREADY_EXISTS``).
      - Any other status (``GENERATING_AUDIO``, ``AUDIO_DONE``, etc.) → skip
        (logs ``CHILD_SHORT_AUDIO_SKIP``).

    Args:
        parent_content_id: UUID of the parent long-form content.
        db: SQLAlchemy session owned by the caller — no new session is opened.

    Returns:
        Number of child Agent 3 tasks enqueued.
    """
    from app.models import AudioFile, Content
    from sqlalchemy.orm import Session as _Session

    children: list = (
        db.query(Content)
        .filter(
            Content.parent_content_id == parent_content_id,
            Content.is_short_episode.is_(True),
        )
        .all()
    )

    total = len(children)
    if total == 0:
        return 0

    status_counts: dict[str, int] = {}
    for _c in children:
        status_counts[_c.status] = status_counts.get(_c.status, 0) + 1

    logger.info(
        "CHILD_SHORT_AUDIO_SCAN parent_content_id=%s total=%d statuses=%s",
        parent_content_id, total, status_counts,
    )

    enqueued = 0
    for child in children:
        # Flip AWAITING_PARENT → SCRIPTS_VALIDATED so the Agent 3 guard passes
        if child.status == "SCRIPTS_VALIDATED_AWAITING_PARENT":
            child.status = "SCRIPTS_VALIDATED"
            db.flush()
            logger.info(
                "CHILD_SHORT_RELEASED child_content_id=%s part=%d/%d",
                child.id,
                child.short_part_number or 0,
                child.short_total_parts or 0,
            )

        if child.status == "SCRIPTS_VALIDATED":
            audio_exists: bool = (
                db.query(AudioFile)
                .filter(AudioFile.content_id == child.id)
                .first()
            ) is not None

            if audio_exists:
                logger.info(
                    "CHILD_SHORT_AUDIO_ALREADY_EXISTS child_content_id=%s — skipping enqueue",
                    child.id,
                )
            else:
                run_agent3_audio_for_content.delay(str(child.id))
                enqueued += 1
                logger.info(
                    "CHILD_SHORT_AUDIO_ENQUEUED child_content_id=%s part=%d/%d",
                    child.id,
                    child.short_part_number or 0,
                    child.short_total_parts or 0,
                )
        else:
            logger.info(
                "CHILD_SHORT_AUDIO_SKIP child_content_id=%s status=%s",
                child.id, child.status,
            )

    if enqueued:
        db.commit()

    return enqueued


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

        # SQLAlchemy auto-expires content after run_audio_generation's commit;
        # accessing .status below triggers a fresh SELECT.
        if (
            content.status == "AUDIO_DONE"
            and not bool(getattr(content, "is_short_episode", False))
        ):
            logger.info(
                "run_agent3_audio_for_content: parent %s AUDIO_DONE — scanning child short episodes",
                content_id,
            )
            ensure_child_short_audio_enqueued(cid, db)

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
    from app.models import Content

    db = _get_session_factory()()
    dispatched = 0
    try:
        ready = db.query(Content).filter(Content.status == "AUDIO_DONE").all()
        for content in ready:
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
