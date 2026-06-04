import logging
import uuid

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, Content, ContentValidation, Script
from app.agents.agent3_validation.system_prompt import auto_correct_script, validate_scripts
from app.agents.agent3_validation.services.estimator import (
    compute_shorts_breakpoints,
    estimate_duration_sec,
)

logger = logging.getLogger(__name__)

_MAX_CORRECTIONS = 3


def run_validation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 3 validation pipeline for one piece of content.

    Pipeline:
      1. Load channel + config + all scripts (latest version per language)
      2. Validate all language scripts simultaneously via Claude
      3. Auto-correct MAJOR issues (up to _MAX_CORRECTIONS rounds)
      4. Estimate duration + compute Shorts breakpoints for every script
      5. Persist results: scripts.validated / estimated_duration_sec /
         shorts_breakpoints, content_validations.script_validation_status /
         script_issues_log / self_correction_attempts, content.status

    MINOR issues are logged to ``script_issues_log``; Step 4
    (minor_handler) sends the Telegram notification from the Celery task.

    Args:
        content_id: UUID of content in status ``SCRIPTS_READY``.
        db:         SQLAlchemy session managed by the caller.

    Returns:
        ``True``  — validation passed (clean or auto-corrected) → ``SCRIPTS_VALIDATED``
        ``False`` — MAJOR issues remain after all corrections → ``NEEDS_REVIEW``
    """
    # ── Load entities ─────────────────────────────────────────────────────────
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.error("Content %s not found", content_id)
        return False

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content_id)
        return False

    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    shorts_rule = config.shorts_rule if config else "auto"

    validation_rec: ContentValidation | None = (
        db.query(ContentValidation)
        .filter(ContentValidation.content_id == content_id)
        .first()
    )

    all_scripts = _load_latest_scripts(content_id, db)
    if not all_scripts:
        logger.warning("No scripts found for content %s — skipping validation", content_id)
        return False

    content.status = "VALIDATING_SCRIPTS"
    db.commit()

    # ── Build working dict (language → {video_script, voice_script}) ──────────
    scripts_by_lang: dict[str, dict] = {
        lang: {"video_script": s.video_script, "voice_script": s.voice_script}
        for lang, s in all_scripts.items()
    }

    # ── Initial validation ────────────────────────────────────────────────────
    result = validate_scripts(scripts_by_lang, channel)
    logger.info(
        "Initial validation for content %s: %s (%d issues)",
        content_id, result["overall_status"], len(result["issues"]),
    )

    major_issues = [i for i in result["issues"] if i["severity"] == "MAJOR"]
    accumulated_issues: list[dict] = list(result["issues"])
    correction_count = 0

    # ── Auto-correction loop (MAJOR only) ─────────────────────────────────────
    while major_issues and correction_count < _MAX_CORRECTIONS:
        correction_count += 1
        logger.info(
            "Auto-correction round %d/%d for content %s",
            correction_count, _MAX_CORRECTIONS, content_id,
        )

        # Group MAJOR issues by language
        by_lang: dict[str, list] = {}
        for issue in major_issues:
            by_lang.setdefault(issue["language"], []).append(issue)

        for lang, lang_issues in by_lang.items():
            if lang not in scripts_by_lang:
                continue
            logger.info("Correcting %s script for content %s", lang, content_id)
            try:
                corrected = auto_correct_script(scripts_by_lang[lang], lang_issues, lang, channel)
            except Exception as exc:
                logger.error("auto_correct_script failed lang=%s: %s", lang, exc)
                continue

            # Persist corrected script as a new version
            new_version = all_scripts[lang].version + 1
            new_script = Script(
                content_id=content_id,
                language=lang,
                video_script=corrected["video_script"],
                voice_script=corrected["voice_script"],
                version=new_version,
                validated=False,
            )
            db.add(new_script)
            db.flush()   # populate new_script.id

            all_scripts[lang] = new_script
            scripts_by_lang[lang] = {
                "video_script": corrected["video_script"],
                "voice_script": corrected["voice_script"],
            }

        # Re-validate all languages after corrections
        result = validate_scripts(scripts_by_lang, channel)
        accumulated_issues.extend(result["issues"])
        major_issues = [i for i in result["issues"] if i["severity"] == "MAJOR"]
        logger.info(
            "Re-validation after correction %d: %s",
            correction_count, result["overall_status"],
        )

    db.commit()

    # ── Determine final validation status ─────────────────────────────────────
    still_major = bool(major_issues)
    minor_issues = [i for i in result["issues"] if i["severity"] == "MINOR"]

    if still_major:
        script_val_status = "NEEDS_REVIEW"
        content_status    = "NEEDS_REVIEW"
        logger.warning(
            "Content %s has unresolved MAJOR issues after %d correction(s)",
            content_id, correction_count,
        )
    elif correction_count > 0:
        script_val_status = "AUTO_CORRECTED"
        content_status    = "SCRIPTS_VALIDATED"
    else:
        script_val_status = "PASSED"
        content_status    = "SCRIPTS_VALIDATED"

    # ── Compute duration + breakpoints, mark scripts validated ────────────────
    for lang, s in all_scripts.items():
        duration = estimate_duration_sec(s.voice_script, lang)
        breakpoints = compute_shorts_breakpoints(s.voice_script, duration, shorts_rule)

        s.estimated_duration_sec = duration
        s.shorts_breakpoints = breakpoints
        if not still_major:
            s.validated = True

        logger.debug(
            "Script lang=%s duration=%.1fs breakpoints=%d validated=%s",
            lang, duration, len(breakpoints), not still_major,
        )

    # ── Persist validation record ─────────────────────────────────────────────
    if validation_rec:
        validation_rec.script_validation_status = script_val_status
        validation_rec.script_issues_log        = accumulated_issues
        validation_rec.self_correction_attempts = correction_count

    content.status = content_status
    db.commit()

    logger.info(
        "Validation done for content %s — status=%s corrections=%d minor_issues=%d",
        content_id, content_status, correction_count, len(minor_issues),
    )
    return not still_major


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_latest_scripts(content_id: uuid.UUID, db: Session) -> dict[str, Script]:
    """Load the highest-version Script per language for a content item."""
    rows: list[Script] = (
        db.query(Script)
        .filter(Script.content_id == content_id)
        .order_by(Script.language, Script.version.desc())
        .all()
    )
    latest: dict[str, Script] = {}
    for s in rows:
        if s.language not in latest:
            latest[s.language] = s
    return latest
