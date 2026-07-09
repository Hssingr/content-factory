import hashlib
import logging
import uuid as _uuid_module
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelSource, Content, ContentValidation, User
from app.agents.agent2_discovery.services.fetcher import fetch_batch, _MAX_NUCLEAR_EXCLUSION
from app.agents.agent2_discovery.services.story import MAX_SOURCE_EXCERPT_CHARS, Story
from app.agents.agent2_discovery.services.story_generator import generate_story_premise
from app.agents.agent2_discovery.services.scoring import (
    score_story_assessment,
    decide_story_acceptance,
)
from app.agents.agent2_discovery.system_prompt import score_story_for_gate
from app.services.script_checks import check_source_material_floor
from app.services.script_source import normalize_script_source

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

    Branches on ``ChannelConfig.script_source`` (AI-Generated Story Discovery —
    see ``code_report/ai_generated_story_discovery_design.md``):
      - ``"reddit"`` (default): fetches a real candidate via ``fetch_batch()``
        (web_search against configured ``ChannelSource`` rows).
      - ``"ai_generated"``: synthesizes a short, cheap premise via
        ``generate_story_premise()`` — no ``ChannelSource`` requirement, no
        web_search cost. The premise goes through the same dedup/scoring/
        Telegram-approval machinery below unchanged; the expensive full-story
        expansion only happens after operator APPROVE, in
        ``script_workflow._ensure_ai_story_expanded()``.

    Retry escalation on duplicate detection OR source-material floor failure
    (prompt engineering follow-up — a candidate whose ``source_excerpt`` cannot
    ground a full script, e.g. a wiki/fandom summary page relayed instead of
    the primary post, is rejected and retried exactly like a duplicate, so a
    too-thin story never reaches Telegram approval in the first place).
    The source-material floor check does not apply to ``ai_generated``
    premises — a short premise is short by design at this stage; see
    ``script_workflow._ensure_ai_story_expanded()`` for where the floor check
    runs for this path instead:
      1. Initial fetch (clean or with caller-supplied ``rejected_stories``)
      2. Retry 1 — accumulated rejected list injected into fetch prompt
      3. Retry 2 — accumulated rejected list
      4. Nuclear retry — ALL existing channel content titles+URLs sent as
         exclusion list. Skipped entirely for ``ai_generated``: synthetic
         per-attempt-unique URLs mean duplicate detection is structurally
         moot for this path, and the exclusion list is already proactively
         seeded with the channel's recent content before attempt 1.
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

    config: ChannelConfig | None = db.get(ChannelConfig, channel_id)
    timeout_hours = config.validation_timeout_hours if config else _DEFAULT_TIMEOUT_HOURS
    script_format = config.script_format if config else "youtube_long"
    script_source = normalize_script_source(config.script_source if config else "reddit")
    is_ai_generated = script_source == "ai_generated"

    sources_list: list[tuple[str, str, float]] = []
    if is_ai_generated:
        ai_language = _resolve_ai_story_language(channel_id, db)
    else:
        sources: list[ChannelSource] = (
            db.query(ChannelSource)
            .filter(ChannelSource.channel_id == channel_id)
            .all()
        )
        if not sources:
            logger.warning("Channel %s has no sources configured", channel_id)
            return None
        sources_list = [
            (s.source_value, s.source_type, float(s.trust_score))
            for s in sources
        ]

    # ── Dedup retry loop ──────────────────────────────────────────────────────
    # accumulated_rejected grows each time a candidate is blocked by dedup.
    # Starts with any pre-seeded list (e.g. from a previous manual-duplicate pass).
    accumulated_rejected: list[dict] = list(rejected_stories or [])
    if is_ai_generated and not accumulated_rejected:
        # Synthetic per-attempt-unique URLs mean _is_duplicate() can never
        # fire true for this path — the prompt-level exclusion list is the
        # ONLY anti-repetition mechanism available, so it must be seeded
        # proactively rather than growing only from in-loop rejections.
        accumulated_rejected = _seed_ai_generated_exclusions(channel_id, db)

    story: Story | None = None

    for attempt in range(_MAX_DEDUP_RETRIES + 1):  # 0, 1, 2
        if is_ai_generated:
            candidates = generate_story_premise(
                channel,
                language=ai_language,
                rejected_stories=accumulated_rejected if accumulated_rejected else None,
            )
        else:
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
            logger.warning(
                "Discovery: duplicate on attempt %d — adding to rejected list and retrying",
                attempt + 1,
            )
            accumulated_rejected.append({"title": candidate.title, "url": candidate.url})
            continue

        floor_reason = None if is_ai_generated else _fails_source_material_floor(candidate, script_format)
        if floor_reason:
            logger.warning(
                "Discovery: candidate too thin on attempt %d (title=%r) — %s — "
                "adding to rejected list and retrying",
                attempt + 1, candidate.title[:60], floor_reason,
            )
            accumulated_rejected.append({
                "title": candidate.title,
                "url": candidate.url,
                "feedback": (
                    "Source material was too thin (e.g. a wiki/fandom page or other summary "
                    "ABOUT the post, not the full post itself) — find a different story and "
                    "relay the actual primary source text, not a page that describes it."
                ),
            })
            continue

        story = candidate
        break

    # ── Nuclear retry: full channel history as exclusion list ─────────────────
    # Skipped entirely for ai_generated — see the docstring's step 4.
    if story is None and not is_ai_generated:
        logger.warning(
            "Discovery: %d targeted attempt(s) exhausted for channel %s — "
            "running nuclear retry with full channel history",
            _MAX_DEDUP_RETRIES + 1, channel_id,
        )
        story = _nuclear_retry(
            channel, sources_list, accumulated_rejected, db, script_format
        )
    elif story is None and is_ai_generated:
        logger.warning(
            "Discovery: %d targeted attempt(s) exhausted for ai_generated channel %s — "
            "nuclear retry does not apply to this path (dedup is structurally moot for "
            "synthetic premises) — falling through to manual fallback",
            _MAX_DEDUP_RETRIES + 1, channel_id,
        )

    # ── All retries exhausted — send manual Telegram fallback ─────────────────
    if story is None:
        logger.warning(
            "Discovery: all retries exhausted for channel %s — sending manual fallback",
            channel_id,
        )
        _create_manual_fallback(
            channel, config, accumulated_rejected, timeout_hours, db,
            is_ai_generated=is_ai_generated,
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
        "Discovery: title=%r overall_score=%.1f rights_ip_risk=%d decision=%s reason=%s",
        story.title[:60], story_score["overall_score"],
        story_score.get("dimension_scores", {}).get("rights_ip_risk", 0),
        "ACCEPTED" if accepted else "REJECTED", reason,
    )
    for flag in story_score.get("operator_review_flags", []):
        logger.warning(
            "Discovery: operator review flag title=%r flag=%s",
            story.title[:60], flag,
        )

    if not accepted:
        logger.warning("Discovery: story rejected — exiting cleanly, no Content created")
        return None

    # ── Persist Content + ContentValidation ──────────────────────────────────
    content = Content(
        channel_id=channel_id,
        source_url=story.url,
        source_language=story.language,
        content_hash=story.content_hash,
        title=story.title,
        status="PENDING_APPROVAL",
        source_excerpt=story.body[:MAX_SOURCE_EXCERPT_CHARS] if story.body else None,
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

def _resolve_ai_story_language(channel_id: _uuid_module.UUID, db: Session) -> str:
    """Resolve the target language for an ``ai_generated`` premise.

    Uses the first ``ChannelLanguage`` row for this channel — the same
    informal "first = primary" convention the frontend already relies on for
    lack of a schema-guaranteed "primary language" concept (documented soft
    spot — see ``code_report/ai_generated_story_discovery_design.md`` §5.1).
    Falls back to ``"en"`` when no ``ChannelLanguage`` row exists at all.
    """
    first_language: ChannelLanguage | None = (
        db.query(ChannelLanguage)
        .filter(ChannelLanguage.channel_id == channel_id)
        .first()
    )
    return first_language.language if first_language else "en"


def _seed_ai_generated_exclusions(channel_id: _uuid_module.UUID, db: Session) -> list[dict]:
    """Proactively seed the exclusion list for an ``ai_generated`` channel.

    Synthetic per-attempt-unique ``discovery://ai_generated/...`` URLs mean
    ``_is_duplicate()`` can structurally never fire true for this path — the
    prompt-level "avoid similar plots" list is the only anti-repetition
    mechanism available, so it must start from the channel's real history
    instead of growing only from in-loop rejections (which would let attempt
    1 repeat any premise the channel already published).
    """
    existing_rows = (
        db.query(Content.title, Content.source_url)
        .filter(
            Content.channel_id == channel_id,
            Content.status.notin_(["AWAITING_MANUAL_STORY"]),
        )
        .order_by(Content.created_at.desc())
        .limit(_MAX_NUCLEAR_EXCLUSION)
        .all()
    )
    return [{"title": row.title, "url": row.source_url} for row in existing_rows]


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


def _fails_source_material_floor(story: Story, script_format: str) -> str | None:
    """Deterministic source-material floor check, run at discovery time.

    Reuses ``check_source_material_floor()`` (the same check
    ``generate_parent_source_script()`` runs post-approval, roadmap 4b /
    audit P1-5) so a too-thin candidate is caught before a Telegram approval
    is ever sent for it, instead of only failing after the operator has
    already approved it. Returns the human-readable rejection description on
    failure, or ``None`` when the candidate has enough material.
    """
    issues = check_source_material_floor(story.body or "", story.language, script_format)
    return issues[0]["description"] if issues else None


def _nuclear_retry(
    channel: Channel,
    sources_list: list[tuple[str, str, float]],
    accumulated_rejected: list[dict],
    db: Session,
    script_format: str,
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

    floor_reason = _fails_source_material_floor(candidate, script_format)
    if floor_reason:
        logger.warning(
            "Discovery: nuclear retry candidate too thin (title=%r) — %s",
            candidate.title[:60], floor_reason,
        )
        return None

    return candidate


def _create_manual_fallback(
    channel: Channel,
    config: ChannelConfig | None,
    rejected_stories: list[dict],
    timeout_hours: int,
    db: Session,
    is_ai_generated: bool = False,
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

    if is_ai_generated:
        # Small honesty fix (design doc §3.3): an ai_generated channel has no
        # ChannelSource — asking for "a Reddit post URL" would be irrelevant.
        message = (
            f"⚠️ *No AI-generated story premise was found automatically.*\n\n"
            f"*Channel:* {channel.name}\n"
            f"*Niche:* {channel.niche}\n"
            f"{rejected_block}\n"
            f"Please send a story premise or full story text manually.\n\n"
            f"*Reply to this message* with the story text "
            f"(title on the first line, story/premise below)."
        )
    else:
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
        logger.warning(
            "Manual fallback sent for channel %s (message_id=%s placeholder_content=%s)",
            channel.id, message_id, content.id,
        )
    else:
        logger.error(
            "Failed to send manual fallback Telegram message for channel %s",
            channel.id,
        )

    db.commit()
