import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelSource, Content, ContentValidation
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent2_discovery.services.scoring import run_story_scoring_gate

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_HOURS = 24


def run_discovery(channel_id: uuid.UUID, db: Session) -> tuple[Content, Story] | None:
    """Main entry point for Agent 2 — discover the best story for a channel.

    Pipeline:
      1. Load channel sources + config from DB
      2. Story Scoring Gate — fetch up to 3 candidates, score each across nine
         fixed dimensions, and accept only the first one that clears every gate
         (narrative tension, visual potential, retention, and overall quality).
         Rejected candidates never reach persistence or Telegram.
      3. Deduplicate the accepted story against ``content.content_hash``
      4. Persist ``Content`` (PENDING_APPROVAL) + ``ContentValidation`` (PENDING)

    Args:
        channel_id: UUID of an active channel.
        db:         SQLAlchemy session — caller is responsible for lifecycle.

    Returns:
        ``(Content, Story)`` so the next pipeline step (script generation) has
        both the DB record and the raw story body for Claude.
        Returns ``None`` on any blocking error or if no new story is found.
    """
    channel = db.get(Channel, channel_id)
    if not channel:
        logger.error("Channel %s not found", channel_id)
        return None

    if not channel.active:
        logger.info("Channel %s is not active — skipping discovery", channel_id)
        return None

    sources: list[ChannelSource] = (
        db.query(ChannelSource)
        .filter(ChannelSource.channel_id == channel_id)
        .all()
    )
    if not sources:
        logger.warning("Channel %s has no sources configured", channel_id)
        return None

    config: ChannelConfig | None = db.get(ChannelConfig, channel_id)
    timeout_hours = config.validation_timeout_hours if config else _DEFAULT_TIMEOUT_HOURS

    # ── 1. Story Scoring Gate — fetch + score candidates, accept only strong ones ─
    sources_list = [
        (s.source_value, s.source_type, float(s.trust_score))
        for s in sources
    ]
    script_format = config.script_format if config else "youtube_long"
    story = run_story_scoring_gate(
        sources=sources_list,
        niche=channel.niche,
        channel=channel,
        script_format=script_format,
    )

    if story is None:
        logger.info(
            "Story Scoring Gate: no story cleared the bar for channel %s this run — skipping",
            channel_id,
        )
        return None

    # ── 2. Deduplicate ────────────────────────────────────────────────────────
    existing = db.query(Content.content_hash).filter(
        Content.content_hash == story.content_hash
    ).first()

    if existing:
        logger.info(
            "Story '%s' is already in the DB (duplicate) — skipping",
            story.title[:60],
        )
        return None

    # ── 3. Persist Content + ContentValidation ────────────────────────────────
    content = Content(
        channel_id=channel_id,
        source_url=story.url,
        source_language=story.language,
        content_hash=story.content_hash,
        title=story.title,
        status="PENDING_APPROVAL",
    )
    db.add(content)
    db.flush()

    now = datetime.now(timezone.utc)
    validation = ContentValidation(
        content_id=content.id,
        status="PENDING",
        revision_count=0,
        timeout_at=now + timedelta(hours=timeout_hours),
    )
    db.add(validation)
    db.commit()
    db.refresh(content)

    logger.info(
        "Content %s created for channel %s — '%s'",
        content.id, channel_id, story.title[:80],
    )
    return content, story
