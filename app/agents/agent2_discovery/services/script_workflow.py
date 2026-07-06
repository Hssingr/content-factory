"""Agent 2 script workflow orchestration."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.agent2_discovery.services.scripts import (
    _script_trace,
    generate_multilingual_scripts,
    generate_script_sections,
    run_script_quality_gate,
    run_shorts_planner,
)
from app.agents.agent2_discovery.services.story import Story
from app.agents.agent2_discovery.system_prompt import generate_story_blueprint
from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script
from app.services.local_run_paths import ensure_run_dirs
from app.services.script_checks import check_source_material_floor
from app.services.script_estimator import estimate_duration_sec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScriptWorkflowContext:
    channel: Channel
    config: ChannelConfig | None
    script_format: str
    audio_tags_enabled: bool
    source_voice: ChannelVoice | None
    tts_model: str
    tts_provider: str
    visual_style: str
    image_style: str
    narration_pov: str


def generate_parent_source_script(
    content: Content,
    db: Session,
    story: Story | None = None,
    context: ScriptWorkflowContext | None = None,
) -> str | None:
    """Generate, quality-gate, and persist the parent source-language script.

    The single shared implementation of blueprint → sections → quality gate →
    source-`Script` persistence (roadmap 4.7 / audit AR-1) — used by BOTH
    `run_script_workflow()` (production/Celery) and
    `test_pipeline/test_full_pipeline.py`'s STEP 3 (operator harness), so the
    two paths can never diverge on generation parameters (visual/image style,
    audio tags, TTS block) or on persistence/versioning behavior.

    Sets ``Content.status = "GENERATING_SCRIPTS"``. Does NOT generate
    multilingual scripts, write ``SCRIPTS_VALIDATED``, or run the shorts
    planner — those remain caller-owned so the harness can interleave its own
    reuse checks and reporting.

    Source-material floor (roadmap 4b / audit P1-5): before any Claude call
    or status transition, fails the discovery→script handoff — sets
    ``Content.status = "FAILED"`` and returns ``None`` — when
    ``content.source_excerpt`` is too thin to ground a full script of the
    channel's configured ``script_format`` (see
    ``app.services.script_checks.check_source_material_floor``).

    Args:
        content: Approved parent `Content` row.
        db:      SQLAlchemy session managed by the caller.
        story:   Optional real discovery `Story`; when omitted, rebuilt from
                 the content row (`source_excerpt` as body).
        context: Optional preloaded `ScriptWorkflowContext` (saves the caller
                 a duplicate load); loaded from the DB when omitted.

    Returns:
        The persisted source voice_script text, or ``None`` when the channel
        context could not be loaded or the source-material floor failed.
    """
    context = context or _load_script_workflow_context(content, db)
    if context is None:
        return None

    if not _passes_source_material_floor(content, context, db):
        return None

    if story is None:
        story = _build_story(content)
    _mark_generating_scripts(content, db, context)

    blueprint = generate_story_blueprint(
        story,
        context.channel,
        script_format=context.script_format,
        visual_style=context.visual_style,
        image_style=context.image_style,
        narration_pov=context.narration_pov,
    )
    logger.info(
        "Blueprint generated for content %s — %d major_turns, suggested_sections=%d",
        content.id,
        len(blueprint.get("major_turns", [])),
        blueprint.get("suggested_section_count", 3),
    )

    content.story_blueprint = blueprint
    db.commit()

    scripts = generate_script_sections(
        story=story,
        blueprint=blueprint,
        channel=context.channel,
        channel_voice=context.source_voice,
        script_format=context.script_format,
        audio_tags_enabled=context.audio_tags_enabled,
        visual_style=context.visual_style,
        image_style=context.image_style,
        narration_pov=context.narration_pov,
    )

    hook_excerpt = scripts.get("voice_script", "").strip()[:300].replace("\n", " ")
    logger.info("Script hook (first 300 chars) for content %s: %r", content.id, hook_excerpt)

    scripts = run_script_quality_gate(
        scripts,
        script_format=context.script_format,
        language=content.source_language,
    )
    _script_trace("tasks_post_quality_gate", scripts.get("voice_script", ""))

    src_voice_script = _persist_source_script(content, scripts, db)
    _merge_visual_intent_history(content, scripts, db)
    return src_voice_script


def _passes_source_material_floor(
    content: Content,
    context: ScriptWorkflowContext,
    db: Session,
) -> bool:
    """Fail the discovery→script handoff when source_excerpt is too thin
    (roadmap 4b / audit P1-5) to ground a full script — before any Claude
    call or status transition into GENERATING_SCRIPTS is spent on it.
    """
    issues = check_source_material_floor(
        content.source_excerpt or "", content.source_language, context.script_format,
    )
    if not issues:
        return True

    issue = issues[0]
    logger.error(
        "SOURCE_MATERIAL_FLOOR_FAILED content=%s script_format=%s: %s",
        content.id, context.script_format, issue["description"],
    )
    content.status = "FAILED"
    db.commit()
    return False


def run_script_workflow(content: Content, db: Session) -> None:
    """Generate, validate, persist, and plan scripts for approved parent content."""
    ensure_run_dirs(content.id)
    context = _load_script_workflow_context(content, db)
    if context is None:
        return

    src_voice_script = generate_parent_source_script(content, db, context=context)
    if src_voice_script is None:
        return

    _script_trace("tasks_entering_multilingual", src_voice_script)
    required_scripts = generate_multilingual_scripts(
        content,
        context.channel,
        db,
        audio_tags_enabled=context.audio_tags_enabled,
    )
    if not required_scripts:
        logger.error(
            "Content %s script workflow stopped before SCRIPTS_VALIDATED — script set incomplete",
            content.id,
        )
        return
    _set_multilingual_durations(content, db)

    content.status = "SCRIPTS_VALIDATED"
    db.commit()
    logger.info("Content %s — SCRIPTS_VALIDATED", content.id)

    # output_mode="youtube_long_only" skips standalone Shorts entirely — the
    # first config-driven branch on ChannelConfig.output_mode (post-roadmap
    # deep audit; previously the value was accepted by the schema but nothing
    # read it, and run_shorts_planner() ran unconditionally for every parent).
    output_mode = getattr(context.config, "output_mode", "youtube_and_shorts") \
        if context.config else "youtube_and_shorts"
    if output_mode == "youtube_long_only":
        logger.info(
            "SHORTS_PLANNER_SKIPPED content=%s reason=output_mode_youtube_long_only",
            content.id,
        )
        return

    try:
        run_shorts_planner(content.id, context.channel, context.config, db)
    except Exception as shorts_exc:
        logger.warning(
            "run_shorts_planner failed for content %s (non-blocking): %s",
            content.id,
            shorts_exc,
        )


def _load_script_workflow_context(
    content: Content,
    db: Session,
) -> ScriptWorkflowContext | None:
    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content.id)
        return None

    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    script_format = config.script_format if config else "youtube_long"
    audio_tags_enabled = config.audio_tags_enabled if config else False

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
                content.source_language,
                src_voice.language,
            )

    tts_model = src_voice.tts_model if src_voice else "sonic-2"
    tts_provider = src_voice.provider if src_voice else "cartesia"
    visual_style = config.visual_style if config else ""
    image_style = config.image_style if config else ""
    narration_pov = config.narration_pov if config else "third_person"

    return ScriptWorkflowContext(
        channel=channel,
        config=config,
        script_format=script_format,
        audio_tags_enabled=audio_tags_enabled,
        source_voice=src_voice,
        tts_model=tts_model,
        tts_provider=tts_provider,
        visual_style=visual_style,
        image_style=image_style,
        narration_pov=narration_pov,
    )


def _build_story(content: Content) -> Story:
    return Story(
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


def _mark_generating_scripts(
    content: Content,
    db: Session,
    context: ScriptWorkflowContext,
) -> None:
    content.status = "GENERATING_SCRIPTS"
    db.commit()

    logger.info(
        "Generating scripts for content %s… (format=%s provider=%s model=%s)",
        content.id,
        context.script_format,
        context.tts_provider,
        context.tts_model,
    )


def _next_source_script_version(content: Content, db: Session) -> int:
    previous: Script | None = (
        db.query(Script)
        .filter(Script.content_id == content.id, Script.language == content.source_language)
        .order_by(Script.version.desc())
        .first()
    )
    return (int(previous.version) + 1) if previous else 1


def _persist_source_script(content: Content, scripts: dict, db: Session) -> str:
    content.title = scripts.get("title", content.title)
    src_voice_script = scripts.get("voice_script", "")
    src_dur_sec = estimate_duration_sec(src_voice_script, content.source_language, db=db)
    version = _next_source_script_version(content, db)

    script_record = Script(
        content_id=content.id,
        language=content.source_language,
        voice_script=src_voice_script,
        version=version,
        validated=True,
        estimated_duration_sec=src_dur_sec,
    )
    db.add(script_record)
    db.commit()
    logger.info(
        "Source script saved for content %s — lang=%s version=%d dur=%.1fs",
        content.id,
        content.source_language,
        version,
        src_dur_sec,
    )
    return src_voice_script


def _merge_visual_intent_history(content: Content, scripts: dict, db: Session) -> None:
    visual_history = scripts.get("visual_intent_history")
    if visual_history and content.story_blueprint:
        content.story_blueprint = {
            **content.story_blueprint,
            "visual_intent_history": visual_history,
        }
        db.commit()


def _set_multilingual_durations(content: Content, db: Session) -> None:
    db.refresh(content)
    all_scripts: list[Script] = (
        db.query(Script).filter(Script.content_id == content.id).all()
    )
    for script in all_scripts:
        if script.language == content.source_language:
            continue
        dur = estimate_duration_sec(script.voice_script, script.language, db=db)
        script.estimated_duration_sec = dur
        script.validated = True
        logger.info(
            "Duration set for lang=%s content %s: %.1fs",
            script.language,
            content.id,
            dur,
        )
    db.commit()
