"""MINOR issue Telegram notification + 5-minute FIX timeout for Agent 3.

Flow:
  1. ``send_minor_notification()`` sends a Telegram message listing MINOR issues
     and stores the Telegram message_id as a special entry in script_issues_log.
  2. ``check_fix_reply()`` is called by the shared Telegram handler when a reply
     is received that doesn't match an Agent 2 validation.  If the reply matches
     a minor-notification message_id, it marks fix_requested=True in script_issues_log.
  3. ``apply_minor_fix_or_continue()`` is called by the Celery countdown task
     (5 min after the notification).  If the user requested a fix, Claude corrects
     the affected scripts; otherwise the issues are logged and the pipeline continues.
     Content.status is set to SCRIPTS_VALIDATED either way.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.database import _get_session_factory
from app.models import Channel, Content, ContentValidation, Script
from app.services import telegram_client

logger = logging.getLogger(__name__)

# Key used to identify minor-notification entries in script_issues_log
_MINOR_NOTIF_TYPE = "minor_notification"


# ── Public: called from the Celery task ──────────────────────────────────────

def send_minor_notification(
    content: Content,
    channel: Channel,
    minor_issues: list[dict],
    db: Session,
) -> None:
    """Send a Telegram message about MINOR script issues and record the message_id.

    Stores a ``{"type": "minor_notification", ...}`` entry in
    ``content_validations.script_issues_log`` so the FIX reply handler and the
    5-minute timeout task can look it up.

    Args:
        content:      Content ORM object (provides title, channel_id).
        channel:      Channel ORM object (provides name).
        minor_issues: List of issue dicts with severity="MINOR".
        db:           SQLAlchemy session managed by the caller.
    """
    user = _load_user(channel, db)
    if not user or not user.telegram_chat_id:
        logger.warning("No telegram_chat_id for channel %s — skipping minor notification", channel.id)
        return

    # Format the issue list
    issue_lines = "\n".join(
        f"• [{i['language']}] {i['category']}: {i['description']}"
        for i in minor_issues
    )
    message = (
        f"⚠️ *Script minor issues detected*\n\n"
        f"*Channel:* {channel.name}\n"
        f"*Content:* {content.title[:60]}\n\n"
        f"Issues found:\n{issue_lines}\n\n"
        f"Reply *FIX* to trigger automatic correction, "
        f"or the pipeline will continue in 5 minutes."
    )

    msg_id = telegram_client.send_message_sync(user.telegram_chat_id, message)
    if not msg_id:
        logger.error("Failed to send minor notification for content %s", content.id)
        return

    logger.info("Minor notification sent for content %s (message_id=%s)", content.id, msg_id)

    # Store the notification record in script_issues_log
    validation = _load_validation(content.id, db)
    if validation:
        log = list(validation.script_issues_log or [])
        log.append({
            "type":               _MINOR_NOTIF_TYPE,
            "telegram_message_id": msg_id,
            "content_id":         str(content.id),
            "fix_requested":      False,
            "fix_feedback":       None,
            "sent_at":            datetime.now(timezone.utc).isoformat(),
        })
        validation.script_issues_log = log
        db.commit()


def check_fix_reply(replied_to_id: str, message_text: str, db: Session) -> bool:
    """Check whether a Telegram reply is a FIX request for a minor notification.

    Called by ``handle_telegram_update`` in agent2 when the replied-to message_id
    does not match any Agent 2 story validation.  Searches ``script_issues_log``
    across all recent pending validations.

    Args:
        replied_to_id: Telegram message_id that the user replied to.
        message_text:  Text of the user's reply.
        db:            SQLAlchemy session managed by the caller.

    Returns:
        ``True`` if this was a FIX reply (handled), ``False`` otherwise.
    """
    if not message_text.upper().startswith("FIX"):
        return False

    # Search recent ContentValidation records for a matching minor notification
    validations = (
        db.query(ContentValidation)
        .filter(ContentValidation.script_validation_status.in_(["PASSED", "AUTO_CORRECTED", None]))
        .all()
    )

    for v in validations:
        for entry in (v.script_issues_log or []):
            if (
                isinstance(entry, dict)
                and entry.get("type") == _MINOR_NOTIF_TYPE
                and entry.get("telegram_message_id") == replied_to_id
                and not entry.get("fix_requested")
            ):
                # Mark FIX requested
                feedback = message_text[3:].strip()  # text after "FIX"
                entry["fix_requested"] = True
                entry["fix_feedback"]  = feedback or None
                v.script_issues_log = list(v.script_issues_log)   # trigger JSONB mutation
                db.commit()
                logger.info(
                    "FIX reply received for content %s (feedback=%r)",
                    entry.get("content_id"), feedback or "none",
                )
                return True

    return False


def send_major_blocked_notification(
    content: Content,
    channel: Channel,
    db: Session,
) -> None:
    """Send a Telegram decision message when MAJOR issues persist after 3 corrections.

    Asks the user to reply PROCEED (continue to Agent 4 as-is) or REVALIDATE
    (run 3 more auto-correction attempts). Stores the decision message_id in
    ``script_issues_log`` so ``check_major_decision_reply`` can match the reply.

    Args:
        content: Content ORM object (status will be set to MAJOR_BLOCKED).
        channel: Channel ORM object.
        db:      SQLAlchemy session managed by the caller.
    """
    user = _load_user(channel, db)
    if not user or not user.telegram_chat_id:
        logger.warning("No telegram_chat_id — cannot send MAJOR decision for content %s", content.id)
        return

    message = (
        f"⛔ *Major script issues persist*\n\n"
        f"*Channel:* {channel.name}\n"
        f"*Content:* {content.title[:60]}\n\n"
        f"After 3 auto-correction attempts, MAJOR issues remain.\n\n"
        f"Reply *PROCEED* to continue to audio generation as-is.\n"
        f"Reply *REVALIDATE* to attempt 3 more auto-corrections."
    )
    msg_id = telegram_client.send_message_sync(user.telegram_chat_id, message)
    if not msg_id:
        logger.error("Failed to send MAJOR decision notification for content %s", content.id)
        return

    content.status = "MAJOR_BLOCKED"

    validation = _load_validation(content.id, db)
    if validation:
        log = list(validation.script_issues_log or [])
        log.append({
            "type":               "major_decision",
            "telegram_message_id": msg_id,
            "content_id":         str(content.id),
            "decision":           None,
        })
        validation.script_issues_log = log

    db.commit()
    logger.info("MAJOR decision message sent for content %s (message_id=%s)", content.id, msg_id)


def check_major_decision_reply(replied_to_id: str, message_text: str, db: Session) -> bool:
    """Process a PROCEED or REVALIDATE reply to a major-blocked notification.

    Called by ``handle_telegram_update`` when the replied-to message_id does not
    match an Agent 2 story validation or a minor FIX notification.

    - PROCEED → sets content.status = "SCRIPTS_VALIDATED" so Agent 4 picks it up
    - REVALIDATE → sets content.status = "SCRIPTS_READY" and fires another
      ``run_agent3_validation`` task (3 more correction attempts)

    Args:
        replied_to_id: Telegram message_id the user replied to.
        message_text:  User's reply text.
        db:            SQLAlchemy session managed by the caller.

    Returns:
        ``True`` if this was a PROCEED/REVALIDATE reply (handled), ``False`` otherwise.
    """
    text_upper = message_text.strip().upper()
    if text_upper not in ("PROCEED", "REVALIDATE"):
        return False

    validations = (
        db.query(ContentValidation)
        .filter(ContentValidation.script_validation_status == "NEEDS_REVIEW")
        .all()
    )

    for v in validations:
        for entry in (v.script_issues_log or []):
            if (
                isinstance(entry, dict)
                and entry.get("type") == "major_decision"
                and entry.get("telegram_message_id") == replied_to_id
                and entry.get("decision") is None
            ):
                cid = uuid.UUID(entry["content_id"])
                entry["decision"] = text_upper
                v.script_issues_log = list(v.script_issues_log)

                content: Content | None = db.get(Content, cid)
                if content is None:
                    db.commit()
                    return True

                if text_upper == "PROCEED":
                    content.status = "SCRIPTS_VALIDATED"
                    db.commit()
                    logger.info("User chose PROCEED for content %s — continuing to Agent 4", cid)
                else:  # REVALIDATE
                    content.status = "SCRIPTS_READY"
                    db.commit()
                    # Re-trigger Agent 3 validation (3 more correction attempts)
                    from app.scheduler.tasks import run_agent3_validation
                    run_agent3_validation.delay(str(cid))
                    logger.info("User chose REVALIDATE for content %s — re-queuing validation", cid)

                return True

    return False


def apply_minor_fix_or_continue(content_id: uuid.UUID, db: Session) -> None:
    """Called 5 minutes after a minor notification is sent.

    If the user replied FIX: auto-correct the affected scripts with Claude.
    Regardless of the reply, sets ``content.status = "SCRIPTS_VALIDATED"``
    so the pipeline can continue to Agent 4.

    Args:
        content_id: UUID of the content whose minor notification timed out.
        db:         SQLAlchemy session managed by the Celery task caller.
    """
    content:    Content | None            = db.get(Content, content_id)
    validation: ContentValidation | None  = _load_validation(content_id, db)

    if not content or not validation:
        logger.warning("apply_minor_fix_or_continue: content or validation not found for %s", content_id)
        return

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        return

    # Find the minor notification entry
    notif_entry = _find_notif_entry(validation)
    if notif_entry is None:
        logger.warning("No minor_notification entry found for content %s — continuing", content_id)
        _finalize(content, db)
        return

    fix_requested = notif_entry.get("fix_requested", False)
    fix_feedback  = notif_entry.get("fix_feedback")

    if fix_requested:
        logger.info("FIX requested for content %s — applying corrections", content_id)
        _apply_fix(content_id, channel, validation, fix_feedback, db)
    else:
        logger.info("No FIX reply for content %s — minor issues logged, continuing", content_id)

    _finalize(content, db)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _apply_fix(
    content_id: uuid.UUID,
    channel: Channel,
    validation: ContentValidation,
    feedback: str | None,
    db: Session,
) -> None:
    """Run auto_correct_script on all languages that had MINOR issues."""
    from app.agents.agent3_validation.system_prompt import auto_correct_script

    minor_issues_by_lang: dict[str, list] = {}
    for entry in (validation.script_issues_log or []):
        if isinstance(entry, dict) and entry.get("severity") == "MINOR":
            lang = entry.get("language", "")
            minor_issues_by_lang.setdefault(lang, []).append(entry)

    for lang, issues in minor_issues_by_lang.items():
        script: Script | None = (
            db.query(Script)
            .filter(Script.content_id == content_id, Script.language == lang)
            .order_by(Script.version.desc())
            .first()
        )
        if not script:
            continue

        if feedback:
            # Append user feedback to the first issue's suggestion
            issues[0]["suggestion"] = (issues[0].get("suggestion", "") + f" — User: {feedback}").strip()

        try:
            corrected = auto_correct_script(
                {"video_script": script.video_script, "voice_script": script.voice_script},
                issues, lang, channel,
            )
            new_script = Script(
                content_id=content_id,
                language=lang,
                video_script=corrected["video_script"],
                voice_script=corrected["voice_script"],
                version=script.version + 1,
                validated=True,
            )
            db.add(new_script)
            logger.info("Applied FIX correction for lang=%s content=%s", lang, content_id)
        except Exception as exc:
            logger.error("FIX correction failed for lang=%s: %s", lang, exc)

    db.commit()


def _finalize(content: Content, db: Session) -> None:
    """Mark the content as SCRIPTS_VALIDATED so Agent 4 can pick it up."""
    if content.status not in ("SCRIPTS_VALIDATED", "NEEDS_REVIEW"):
        content.status = "SCRIPTS_VALIDATED"
    db.commit()
    logger.info("Content %s finalized → %s", content.id, content.status)


def _find_notif_entry(validation: ContentValidation) -> dict | None:
    for entry in (validation.script_issues_log or []):
        if isinstance(entry, dict) and entry.get("type") == _MINOR_NOTIF_TYPE:
            return entry
    return None


def _load_validation(content_id: uuid.UUID, db: Session) -> ContentValidation | None:
    return (
        db.query(ContentValidation)
        .filter(ContentValidation.content_id == content_id)
        .first()
    )


def _load_user(channel: Channel, db: Session):
    from app.models import User
    return db.get(User, channel.user_id)
