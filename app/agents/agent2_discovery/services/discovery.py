import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelSource, Content, ContentValidation
from app.agents.agent2_discovery.services.fetcher import fetch_batch
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent2_discovery.services.scoring import (
    score_story_assessment,
    decide_story_acceptance,
)
from app.agents.agent2_discovery.system_prompt import score_story_for_gate

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_HOURS = 24


def run_discovery(channel_id: uuid.UUID, db: Session) -> tuple[Content, Story, dict] | None:
    """Main entry point for Agent 2 — discover the best story for a channel.

    Pipeline:
      1. Load channel sources + config from DB
      2. Fetch highest-engagement story via web search (story_research / Sonnet)
      3. Deduplicate immediately — URL check first (fastest), then hash check as fallback.
         Both gates run before any Claude call is made.
      4. Score the story (story_gate_scoring / Sonnet, 18 dimensions)
      5. Apply gate (Python: weighted score + hard floors)
      6. Persist ``Content`` (PENDING_APPROVAL) + ``ContentValidation`` (PENDING)

    Args:
        channel_id: UUID of an active channel.
        db:         SQLAlchemy session — caller is responsible for lifecycle.

    Returns:
        ``(Content, Story, assessment)`` so the next pipeline step has the DB
        record, the raw story body, and the full Claude scoring assessment dict
        (used to populate the top-2 scoring dimensions in the Telegram summary).
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
    script_format = config.script_format if config else "youtube_long"

    sources_list = [
        (s.source_value, s.source_type, float(s.trust_score))
        for s in sources
    ]

    # ── 1. Fetch single highest-engagement story ──────────────────────────────
    stories: list[Story] = fetch_batch(sources_list, niche=channel.niche)
    if not stories:
        logger.info("Discovery: fetch returned no story for channel %s — exiting cleanly", channel_id)
        return None

    story = stories[0]
    logger.info("Discovery: fetched story (title=%r url=%s)", story.title[:80], story.url)

    # ── 2a. URL dedup — fastest check, runs before any Claude call ───────────
    # Catches re-fetches of the same URL even when the title differs between runs.
    url_exists = db.query(Content.id).filter(Content.source_url == story.url).first()
    if url_exists:
        logger.info(
            "Discovery: URL already seen (content=%s) — skipping, no Claude call",
            url_exists[0],
        )
        return None

    # ── 2b. Hash dedup — catches same content posted at a different URL ───────
    hash_exists = db.query(Content.id).filter(
        Content.content_hash == story.content_hash
    ).first()
    if hash_exists:
        logger.info(
            "Discovery: story '%s' already in DB by hash (content=%s) — skipping",
            story.title[:60], hash_exists[0],
        )
        return None

    # ── 3. Score the story ────────────────────────────────────────────────────
    try:
        assessment = score_story_for_gate(
            story=story,
            channel=channel,
            script_format=script_format,
        )
    except Exception as exc:
        logger.error(
            "Discovery: story scoring failed for %r: %s — exiting cleanly",
            story.title[:80], exc,
        )
        return None

    try:
        story_score = score_story_assessment(assessment)
    except Exception as exc:
        logger.error("Discovery: score_story_assessment failed: %s — skipping", exc)
        return None

    accepted, reason = decide_story_acceptance(story_score)
    logger.info(
        "Discovery: title=%r overall_score=%.1f decision=%s reason=%s",
        story.title[:60], story_score["overall_score"],
        "ACCEPTED" if accepted else "REJECTED", reason,
    )

    if not accepted:
        logger.info("Discovery: story rejected — exiting cleanly, no Content created")
        return None

    # ── 4. Persist Content + ContentValidation ────────────────────────────────
    content = Content(
        channel_id=channel_id,
        source_url=story.url,
        source_language=story.language,
        content_hash=story.content_hash,
        title=story.title,
        status="PENDING_APPROVAL",
        source_excerpt=story.body[:8000] if story.body else None,
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
    return content, story, assessment
