import logging

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelLanguage, Content, Script
from app.agents.agent2_discovery.system_prompt import (
    assess_script_quality,
    generate_native_script,
    rewrite_script_for_quality,
)

logger = logging.getLogger(__name__)

_MAX_QUALITY_REWRITES = 2


def run_script_quality_gate(scripts: dict, channel: Channel, script_format: str = "youtube_long") -> dict:
    """Run the Script Quality Gate — assess retention quality, rewrite if needed.

    Distinct from Agent 3's technical validator: this checks whether a normal
    YouTube viewer would actually keep watching (hook, clarity, pacing, generic
    AI phrasing, TTS readability) using fixed editorial criteria. Runs BEFORE
    persistence/Telegram so the user only ever sees retention-worthy scripts.

    Loops at most ``_MAX_QUALITY_REWRITES`` times: assess → if NEEDS_REWRITE,
    rewrite the FULL script preserving facts/markers, then re-assess. If still
    NEEDS_REWRITE after the limit, the latest version is kept and a warning is
    logged — the pipeline never blocks on this check.

    Args:
        scripts:       Dict with ``title``, ``video_script``, ``voice_script``
                       (source-language, output of ``generate_scripts()``).
        channel:       Channel ORM object (provides niche and tone as context).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        The final scripts dict — rewritten if the gate required it, otherwise
        the original. Always has ``title``, ``video_script``, ``voice_script``.
    """
    current = scripts
    for attempt in range(1, _MAX_QUALITY_REWRITES + 1):
        try:
            review = assess_script_quality(current, channel, script_format=script_format)
        except Exception as exc:
            logger.error("Script Quality Gate assessment failed (attempt %d): %s — keeping script as-is", attempt, exc)
            return current

        status = review.get("status", "PASSED")
        issues = review.get("issues", [])
        high = sum(1 for i in issues if i.get("severity") == "HIGH")
        logger.info(
            "Script Quality Gate: status=%s issues=%d (high=%d) attempt=%d",
            status, len(issues), high, attempt,
        )
        for issue in issues:
            logger.info(
                "Script quality issue [%s/%s]: %s -> %s",
                issue.get("severity", "?"), issue.get("category", "?"),
                issue.get("description", ""), issue.get("fix", ""),
            )

        if status == "PASSED" or not issues:
            return current

        try:
            current = rewrite_script_for_quality(current, issues, channel, script_format=script_format)
        except Exception as exc:
            logger.error("Script Quality Gate rewrite failed (attempt %d): %s — keeping prior script", attempt, exc)
            return current

    logger.warning(
        "Script Quality Gate: still NEEDS_REWRITE after %d attempt(s) — proceeding with latest version",
        _MAX_QUALITY_REWRITES,
    )
    return current


def generate_multilingual_scripts(
    content: Content,
    channel: Channel,
    db: Session,
) -> list[Script]:
    """Generate culturally adapted scripts for every channel target language.

    The source-language script must already exist in the DB (written by the
    discovery Celery task after ``generate_scripts()``). For each target
    language that differs from the source, ``generate_native_script()`` is
    called and a new ``Script`` record is persisted.

    On completion, ``content.status`` is updated to ``SCRIPTS_READY``.
    Partial failures (one language fails) are logged and skipped — the batch
    continues so other languages still get their scripts.

    Args:
        content:  Content ORM object with ``status="APPROVED"``.
        channel:  Channel ORM object (provides ``niche`` and ``tone``).
        db:       SQLAlchemy session managed by the caller.

    Returns:
        All ``Script`` records that exist for this content after the run,
        covering both the source language and successfully adapted languages.
        Returns the source script alone if adaptation fails for all languages.
        Returns an empty list and sets ``status="FAILED"`` if no source script exists.
    """
    content.status = "GENERATING_SCRIPTS"
    db.commit()

    # ── Load source script ────────────────────────────────────────────────────
    source_script: Script | None = (
        db.query(Script)
        .filter(
            Script.content_id == content.id,
            Script.language == content.source_language,
        )
        .order_by(Script.version.desc())
        .first()
    )

    if not source_script:
        logger.error(
            "No source script found for content %s (language=%s) — cannot generate multilingual",
            content.id, content.source_language,
        )
        content.status = "FAILED"
        db.commit()
        return []

    # ── Load channel script format ────────────────────────────────────────────
    config: ChannelConfig | None = (
        db.query(ChannelConfig)
        .filter(ChannelConfig.channel_id == channel.id)
        .first()
    )
    script_format = config.script_format if config else "youtube_long"

    # ── Load channel target languages ─────────────────────────────────────────
    channel_langs: list[ChannelLanguage] = (
        db.query(ChannelLanguage)
        .filter(ChannelLanguage.channel_id == channel.id)
        .all()
    )
    target_codes = [cl.language for cl in channel_langs]

    if not target_codes:
        logger.warning(
            "Channel %s has no languages configured — using source language only",
            channel.id,
        )
        content.status = "SCRIPTS_READY"
        db.commit()
        return [source_script]

    # ── Detect which languages already have scripts (safe for retries) ────────
    already_done: set[str] = {
        lang
        for (lang,) in db.query(Script.language)
        .filter(Script.content_id == content.id)
        .all()
    }

    # ── Generate per-language scripts ─────────────────────────────────────────
    result: list[Script] = []

    for lang in target_codes:
        if lang == content.source_language:
            # Source script already exists — include as-is
            result.append(source_script)
            continue

        if lang in already_done:
            # Previously generated (e.g. retry after partial failure)
            existing = (
                db.query(Script)
                .filter(Script.content_id == content.id, Script.language == lang)
                .order_by(Script.version.desc())
                .first()
            )
            if existing:
                result.append(existing)
                logger.debug("Script for lang=%s already exists — skipping", lang)
            continue

        logger.info("Generating %s script for content %s…", lang, content.id)
        try:
            adapted = generate_native_script(
                video_script=source_script.video_script,
                voice_script=source_script.voice_script,
                target_language=lang,
                niche=channel.niche,
                tone=channel.tone,
                script_format=script_format,
            )
        except Exception as exc:
            logger.error(
                "Native script generation failed (lang=%s, content=%s): %s",
                lang, content.id, exc,
            )
            continue   # partial failure — other languages still proceed

        script = Script(
            content_id=content.id,
            language=lang,
            video_script=adapted["video_script"],
            voice_script=adapted["voice_script"],
            version=1,
            validated=False,
            # estimated_duration_sec and shorts_breakpoints set by Agent 3
        )
        db.add(script)
        db.flush()    # populate script.id before next iteration
        result.append(script)
        logger.debug("Script saved: lang=%s id=%s", lang, script.id)

    # ── Finalise ──────────────────────────────────────────────────────────────
    content.status = "SCRIPTS_READY"
    db.commit()

    languages = [s.language for s in result]
    logger.info(
        "Multilingual scripts ready for content %s — %d language(s): %s",
        content.id, len(result), languages,
    )
    return result
