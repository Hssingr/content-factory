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


def run_script_workflow(content: Content, db: Session) -> None:
    """Generate, validate, persist, and plan scripts for approved parent content."""
    context = _load_script_workflow_context(content, db)
    if context is None:
        return

    story = _build_story(content)
    _mark_generating_scripts(content, db, context)

    blueprint = generate_story_blueprint(
        story,
        context.channel,
        script_format=context.script_format,
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
    )

    hook_excerpt = scripts.get("voice_script", "").strip()[:300].replace("\n", " ")
    logger.info("Script hook (first 300 chars) for content %s: %r", content.id, hook_excerpt)

    scripts = run_script_quality_gate(
        scripts,
        context.channel,
        content=content,
        db=db,
        blueprint=blueprint,
        script_format=context.script_format,
        language=content.source_language,
        tts_model=context.tts_model,
        tts_provider=context.tts_provider,
    )
    _script_trace("tasks_post_quality_gate", scripts.get("voice_script", ""))

    src_voice_script = _persist_source_script(content, scripts, db)
    _merge_visual_intent_history(content, scripts, db)

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

    return ScriptWorkflowContext(
        channel=channel,
        config=config,
        script_format=script_format,
        audio_tags_enabled=audio_tags_enabled,
        source_voice=src_voice,
        tts_model=tts_model,
        tts_provider=tts_provider,
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


def _persist_source_script(content: Content, scripts: dict, db: Session) -> str:
    content.title = scripts.get("title", content.title)
    src_voice_script = scripts.get("voice_script", "")
    src_dur_sec = estimate_duration_sec(src_voice_script, content.source_language)

    script_record = Script(
        content_id=content.id,
        language=content.source_language,
        voice_script=src_voice_script,
        version=1,
        validated=True,
        estimated_duration_sec=src_dur_sec,
    )
    db.add(script_record)
    db.commit()
    logger.info(
        "Source script saved for content %s — lang=%s dur=%.1fs",
        content.id,
        content.source_language,
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
        dur = estimate_duration_sec(script.voice_script, script.language)
        script.estimated_duration_sec = dur
        script.validated = True
        logger.info(
            "Duration set for lang=%s content %s: %.1fs",
            script.language,
            content.id,
            dur,
        )
    db.commit()
