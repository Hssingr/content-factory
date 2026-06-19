import hashlib
import logging
import uuid as _uuid_module
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelSource, Content, ContentValidation, User
from app.agents.agent2_discovery.services.fetcher import fetch_batch, _MAX_NUCLEAR_EXCLUSION
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent2_discovery.services.scoring import (
    score_story_assessment,
    decide_story_acceptance,
)
from app.agents.agent2_discovery.system_prompt import score_story_for_gate

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_HOURS = 24

# 1 initial attempt + 2 targeted retries (accumulated rejected stories) +
# 1 nuclear retry (full channel history as exclusion list) = 4 max web_search calls.
_MAX_DEDUP_RETRIES = 2


def run_discovery(
    channel_id: _uuid_module.UUID,
    db: Session,
    rejected_stories: list[dict] | None = None,
) -> tuple[Content, Story, dict] | None:
    """Main entry point for Agent 2 — discover the best story for a channel.

    Retry escalation on duplicate detection:
      1. Initial fetch (clean or with caller-supplied ``rejected_stories``)
      2. Retry 1 — accumulated rejected list injected into fetch prompt
      3. Retry 2 — accumulated rejected list
      4. Nuclear retry — ALL existing channel content titles+URLs sent as exclusion list
      5. Manual Telegram fallback — operator asked to submit a story manually

    Args:
        channel_id:       UUID of an active channel.
        db:               SQLAlchemy session — caller is responsible for lifecycle.
        rejected_stories: Pre-seeded exclusion list (used when resuming after a manual
                          duplicate — the operator's rejected story is folded in here
                          so the retry context is complete from the first call).

    Returns:
        ``(Content, Story, assessment)`` on success, or ``None`` if all retries exhausted
        (manual fallback Telegram message already sent in that case).
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

    # ── Dedup retry loop ──────────────────────────────────────────────────────
    # accumulated_rejected grows each time a candidate is blocked by dedup.
    # Starts with any pre-seeded list (e.g. from a previous manual-duplicate pass).
    accumulated_rejected: list[dict] = list(rejected_stories or [])
    story: Story | None = None

    for attempt in range(_MAX_DEDUP_RETRIES + 1):  # 0, 1, 2
        candidates = fetch_batch(
            sources_list,
            niche=channel.niche,
            rejected_stories=accumulated_rejected if accumulated_rejected else None,
        )
        if not candidates:
            logger.info(
                "Discovery: fetch returned no story (attempt=%d/%d) — stopping",
                attempt + 1, _MAX_DEDUP_RETRIES + 1,
            )
            break

        candidate = candidates[0]
        logger.info(
            "Discovery: fetched candidate attempt=%d/%d (title=%r url=%s)",
            attempt + 1, _MAX_DEDUP_RETRIES + 1,
            candidate.title[:80], candidate.url,
        )

        if _is_duplicate(candidate, db):
            logger.info(
                "Discovery: duplicate on attempt %d — adding to rejected list and retrying",
                attempt + 1,
            )
            accumulated_rejected.append({"title": candidate.title, "url": candidate.url})
            continue

        story = candidate
        break

    # ── Nuclear retry: full channel history as exclusion list ─────────────────
    if story is None:
        logger.warning(
            "Discovery: %d targeted attempt(s) exhausted for channel %s — "
            "running nuclear retry with full channel history",
            _MAX_DEDUP_RETRIES + 1, channel_id,
        )
        story = _nuclear_retry(
            channel, sources_list, accumulated_rejected, db
        )

    # ── All retries exhausted — send manual Telegram fallback ─────────────────
    if story is None:
        logger.warning(
            "Discovery: all retries exhausted for channel %s — sending manual fallback",
            channel_id,
        )
        _create_manual_fallback(
            channel, config, accumulated_rejected, timeout_hours, db
        )
        return None

    # ── Score the accepted story ──────────────────────────────────────────────
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

    # ── Persist Content + ContentValidation ──────────────────────────────────
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


# ── Private helpers ───────────────────────────────────────────────────────────

def _is_duplicate(story: Story, db: Session) -> bool:
    """Return True if this story's URL or content hash is already in the DB."""
    url_exists = db.query(Content.id).filter(Content.source_url == story.url).first()
    if url_exists:
        logger.info(
            "Discovery: URL already seen (content=%s) title=%r",
            url_exists[0], story.title[:60],
        )
        return True

    hash_exists = db.query(Content.id).filter(
        Content.content_hash == story.content_hash
    ).first()
    if hash_exists:
        logger.info(
            "Discovery: hash already seen (content=%s) title=%r",
            hash_exists[0], story.title[:60],
        )
        return True

    return False


def _nuclear_retry(
    channel: Channel,
    sources_list: list[tuple[str, str, float]],
    accumulated_rejected: list[dict],
    db: Session,
) -> Story | None:
    """One final fetch with the full channel content history as the exclusion list.

    Fetches the most recent ``_MAX_NUCLEAR_EXCLUSION`` title+URL pairs already in
    the DB for this channel and merges them with the accumulated rejected list before
    calling ``fetch_batch``.  This handles channels that have been running long enough
    that Claude keeps rediscovering previously used stories.
    """
    # Pull existing channel content — most recent first, capped to token budget
    existing_rows = (
        db.query(Content.title, Content.source_url)
        .filter(
            Content.channel_id == channel.id,
            Content.status.notin_(["AWAITING_MANUAL_STORY"]),
        )
        .order_by(Content.created_at.desc())
        .limit(_MAX_NUCLEAR_EXCLUSION)
        .all()
    )
    existing_exclusions = [
        {"title": row.title, "url": row.source_url}
        for row in existing_rows
    ]

    # Merge: existing history + anything already in accumulated_rejected (dedup by url)
    seen_urls: set[str] = {r["url"] for r in existing_exclusions}
    for r in accumulated_rejected:
        if r["url"] not in seen_urls:
            existing_exclusions.append(r)
            seen_urls.add(r["url"])

    logger.info(
        "Discovery: nuclear retry with %d total exclusions for channel %s",
        len(existing_exclusions), channel.id,
    )

    candidates = fetch_batch(
        sources_list,
        niche=channel.niche,
        rejected_stories=existing_exclusions,
    )
    if not candidates:
        logger.info("Discovery: nuclear retry returned no story")
        return None

    candidate = candidates[0]
    logger.info(
        "Discovery: nuclear retry candidate (title=%r url=%s)",
        candidate.title[:80], candidate.url,
    )

    if _is_duplicate(candidate, db):
        logger.warning(
            "Discovery: nuclear retry candidate is still a duplicate (title=%r)",
            candidate.title[:60],
        )
        return None

    return candidate


def _create_manual_fallback(
    channel: Channel,
    config: ChannelConfig | None,
    rejected_stories: list[dict],
    timeout_hours: int,
    db: Session,
) -> None:
    """Create a placeholder Content row and notify the operator via Telegram.

    The placeholder row (``status="AWAITING_MANUAL_STORY"``) acts as the anchor
    for the operator's reply: ``handle_telegram_update`` matches the Telegram reply
    to the linked ``ContentValidation.telegram_message_id`` and routes it to
    ``_handle_manual_story_input``.

    Does not raise on failure — discovery simply returns None and the pipeline
    continues on the next scheduled run.
    """
    from app.services import telegram_client

    user: User | None = db.get(User, channel.user_id)
    if not user or not user.telegram_chat_id:
        logger.error(
            "No telegram_chat_id for channel %s user — cannot send manual fallback",
            channel.id,
        )
        return

    now = datetime.now(timezone.utc)
    unique_suffix = str(_uuid_module.uuid4())[:8]
    placeholder_hash = hashlib.sha256(
        f"manual_{channel.id}_{unique_suffix}".encode()
    ).hexdigest()

    content = Content(
        channel_id=channel.id,
        source_url=f"discovery://manual/{channel.id}/{unique_suffix}",
        source_language="en",
        content_hash=placeholder_hash,
        title="Manual story requested",
        status="AWAITING_MANUAL_STORY",
        source_excerpt=None,
        story_blueprint={"rejected_stories": rejected_stories},
    )
    db.add(content)
    db.flush()

    validation = ContentValidation(
        content_id=content.id,
        status="PENDING",
        revision_count=0,
        timeout_at=now + timedelta(hours=timeout_hours),
    )
    db.add(validation)
    db.flush()

    # Build the Telegram message
    if rejected_stories:
        rejected_lines = "\n".join(
            f"  {i + 1}. _{r['title'][:80]}_"
            for i, r in enumerate(rejected_stories)
        )
        rejected_block = f"\n*Already tried (all duplicates):*\n{rejected_lines}\n"
    else:
        rejected_block = ""

    message = (
        f"⚠️ *No new story was found automatically.*\n\n"
        f"*Channel:* {channel.name}\n"
        f"*Niche:* {channel.niche}\n"
        f"{rejected_block}\n"
        f"Please send a Reddit story manually.\n\n"
        f"*Reply to this message* with:\n"
        f"• A Reddit post URL, OR\n"
        f"• The full story text (title on the first line, story below)"
    )

    message_id = telegram_client.send_message_sync(user.telegram_chat_id, message)
    if message_id:
        validation.telegram_message_id = message_id
        validation.sent_at = now
        logger.info(
            "Manual fallback sent for channel %s (message_id=%s placeholder_content=%s)",
            channel.id, message_id, content.id,
        )
    else:
        logger.error(
            "Failed to send manual fallback Telegram message for channel %s",
            channel.id,
        )

    db.commit()
