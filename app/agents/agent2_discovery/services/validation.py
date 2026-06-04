import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from app.database import _get_session_factory
from app.models import Channel, ChannelConfig, Content, ContentValidation, Script, User
from app.services import telegram_client
from app.agents.agent2_discovery.system_prompt import (
    generate_revised_scripts,
    generate_telegram_summary,
)
from app.agents.agent3_validation.services.minor_handler import (
    check_fix_reply,
    check_major_decision_reply,
)

logger = logging.getLogger(__name__)


# ── Public: called from Celery tasks (sync) ───────────────────────────────────

def send_for_validation(
    content: Content,
    channel: Channel,
    scripts: dict,
    db: Session,
) -> None:
    """Send discovered content to the channel owner on Telegram for approval.

    Generates a Telegram summary via Claude, sends it, and stores the returned
    ``message_id`` in the ``content_validations`` record.

    Args:
        content:  Content ORM object (source_url, title, id).
        channel:  Channel ORM object (name, niche, user_id).
        scripts:  Output of ``generate_scripts()`` — provides generated title + excerpt.
        db:       SQLAlchemy session managed by the caller.
    """
    user: User | None = db.get(User, channel.user_id)
    if not user:
        logger.error("User %s not found for channel %s — cannot send validation", channel.user_id, channel.id)
        return

    chat_id = user.telegram_chat_id
    if not chat_id:
        logger.error("User %s has no telegram_chat_id", user.id)
        return

    # Generate the Telegram summary (Claude, user's primary language)
    try:
        message = generate_telegram_summary(content, channel, scripts, user.primary_language)
    except Exception as exc:
        logger.error("generate_telegram_summary failed for content %s: %s", content.id, exc)
        return

    # Send — sync HTTP call, safe for Celery prefork workers
    message_id = telegram_client.send_message_sync(chat_id, message)
    if not message_id:
        logger.error("Telegram send failed for content %s — validation not recorded", content.id)
        return

    # Persist telegram_message_id and sent_at
    validation: ContentValidation | None = (
        db.query(ContentValidation)
        .filter(ContentValidation.content_id == content.id)
        .first()
    )
    if validation:
        validation.telegram_message_id = message_id
        validation.sent_at = datetime.now(timezone.utc)
        validation.status = "PENDING"
        db.commit()

    logger.info("Content %s sent for validation (message_id=%s)", content.id, message_id)


def check_validation_timeouts(db: Session) -> int:
    """Auto-approve or mark NEEDS_REVIEW for every expired PENDING validation.

    Called by Celery beat every 15 minutes. Applies the channel's
    ``validation_on_limit_reached`` policy (``auto_approve`` | ``needs_review``).

    Args:
        db: SQLAlchemy session managed by the Celery task caller.

    Returns:
        Number of validations processed.
    """
    now = datetime.now(timezone.utc)
    expired: list[ContentValidation] = (
        db.query(ContentValidation)
        .filter(ContentValidation.status == "PENDING", ContentValidation.timeout_at < now)
        .all()
    )

    count = 0
    for validation in expired:
        content: Content | None = db.get(Content, validation.content_id)
        if not content:
            continue
        channel: Channel | None = db.get(Channel, content.channel_id)
        config: ChannelConfig | None = db.get(ChannelConfig, channel.id) if channel else None
        policy = config.validation_on_limit_reached if config else "auto_approve"

        _apply_limit_policy(validation, content, policy, db)
        count += 1

    if count:
        logger.info("Timeout sweep: %d validation(s) processed", count)
    return count


# ── Public: async handler registered with python-telegram-bot ─────────────────

async def handle_telegram_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Async Telegram update handler — registered via ``telegram_client.start_polling()``.

    Only processes replies to messages we sent (identified by ``telegram_message_id``).
    DB work is sync; the Telegram re-send on CHANGE is awaited on the event loop.
    """
    
    if not update.message or not update.message.reply_to_message:
        return   # ignore non-reply messages

    replied_to_id = str(update.message.reply_to_message.message_id)
    message_text  = (update.message.text or "").strip()

    db = _get_session_factory()()
    try:
        result = _find_validation(replied_to_id, db)
        if result is None:
            # Not an Agent 2 story validation — try Agent 3 handlers in order:
            # 1. Minor FIX reply  2. Major PROCEED / REVALIDATE decision
            if not check_fix_reply(replied_to_id, message_text, db):
                check_major_decision_reply(replied_to_id, message_text, db)
            return

        validation, content, channel = result

        if message_text.upper().startswith("APPROVE"):
            _approve(validation, content, db)
        else:
            # Sync: update DB, regenerate scripts; returns Telegram message to send
            pending_send = _handle_change(validation, content, channel, message_text, db)
            if pending_send:
                chat_id, text = pending_send
                new_msg_id = await telegram_client.send_message(chat_id, text)
                if new_msg_id:
                    validation.telegram_message_id = new_msg_id
                    validation.sent_at = datetime.now(timezone.utc)
                    db.commit()
    except Exception as exc:
        logger.error("Error handling Telegram update (replied_to=%s): %s", replied_to_id, exc)
    finally:
        db.close()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_validation(
    replied_to_id: str,
    db: Session,
) -> tuple[ContentValidation, Content, Channel] | None:
    """Look up the pending validation that matches a Telegram reply."""
    validation: ContentValidation | None = (
        db.query(ContentValidation)
        .filter(
            ContentValidation.telegram_message_id == replied_to_id,
            ContentValidation.status == "PENDING",
        )
        .first()
    )
    if not validation:
        logger.debug("No pending validation for message_id=%s", replied_to_id)
        return None

    content: Content | None = db.get(Content, validation.content_id)
    if not content:
        return None

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        return None

    return validation, content, channel


def _approve(validation: ContentValidation, content: Content, db: Session) -> None:
    """Mark validation and content as APPROVED. Celery beat picks it up next."""
    now = datetime.now(timezone.utc)
    validation.status  = "APPROVED"
    validation.approved_at = now
    content.status     = "APPROVED"
    db.commit()
    logger.info("Content %s approved by user", content.id)


def _handle_change(
    validation: ContentValidation,
    content: Content,
    channel: Channel,
    feedback: str,
    db: Session,
) -> tuple[str, str] | None:
    """Process a CHANGE request.

    Increments revision_count, checks limits, regenerates scripts if within limit.

    Returns:
        ``(chat_id, telegram_message)`` to be sent by the caller, or ``None``.
    """
    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    max_revisions = config.validation_max_revisions if config else 3
    on_limit      = config.validation_on_limit_reached if config else "auto_approve"

    validation.revision_count += 1

    # Log feedback
    issues = list(validation.script_issues_log or [])
    issues.append({"revision": validation.revision_count, "feedback": feedback})
    validation.script_issues_log = issues

    if validation.revision_count >= max_revisions:
        _apply_limit_policy(validation, content, on_limit, db)
        return None

    # Load the latest source-language script for revision context
    script: Script | None = (
        db.query(Script)
        .filter(Script.content_id == content.id, Script.language == content.source_language)
        .order_by(Script.version.desc())
        .first()
    )
    if not script:
        logger.warning("No source script for content %s — cannot regenerate", content.id)
        db.commit()
        return None

    # Regenerate
    try:
        current = {
            "title":        content.title,
            "video_script": script.video_script,
            "voice_script": script.voice_script,
        }
        revised = generate_revised_scripts(current, feedback, channel)
    except Exception as exc:
        logger.error("Script revision failed for content %s: %s", content.id, exc)
        db.commit()
        return None

    # Save new script version
    new_script = Script(
        content_id=content.id,
        language=content.source_language,
        video_script=revised["video_script"],
        voice_script=revised["voice_script"],
        version=script.version + 1,
        validated=False,
    )
    db.add(new_script)

    if "title" in revised:
        content.title = revised["title"]

    db.flush()

    # Build the re-send message
    user: User | None = db.get(User, channel.user_id)
    if not user or not user.telegram_chat_id:
        db.commit()
        return None

    try:
        message = generate_telegram_summary(content, channel, revised, user.primary_language)
    except Exception as exc:
        logger.error("generate_telegram_summary failed during revision: %s", exc)
        db.commit()
        return None

    db.commit()
    logger.info("Content %s revised (v%d) — re-queued for Telegram send", content.id, new_script.version)
    return user.telegram_chat_id, message


def _apply_limit_policy(
    validation: ContentValidation,
    content: Content,
    policy: str,
    db: Session,
) -> None:
    """Apply validation_on_limit_reached policy when max revisions or timeout is hit."""
    if policy == "auto_approve":
        validation.status      = "APPROVED"
        validation.approved_at = datetime.now(timezone.utc)
        content.status         = "APPROVED"
        logger.info("Content %s auto-approved (policy=%s)", content.id, policy)
    else:
        validation.status = "NEEDS_REVIEW"
        content.status    = "NEEDS_REVIEW"
        logger.info("Content %s set to NEEDS_REVIEW (policy=%s)", content.id, policy)
    db.commit()
