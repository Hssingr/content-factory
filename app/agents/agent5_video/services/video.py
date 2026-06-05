"""Agent 5 — Video Generation orchestration service.

Orchestrates the full per-language video pipeline:
  1. Section Splitter        — parse script → timed sections (Claude enriches visuals)
  2. Section Validator       — validate/enrich sections, up to 3 correction rounds
  3. Save video_sections     — persist to DB
  4. Stock fetcher           — fetch actual media URLs per section
  5. Assembly Validator      — validate media relevance + overall assembly (Claude, 1 pass)
  6. Shorts Cutter           — group sections into Short segments
  7. Subtitles generator     — standard (main) + karaoke (Shorts) from Whisper timestamps
  8. Remotion builder        — write JSON props files
  9. Remotion renderer       — call Remotion CLI, save VideoRender records

Status transitions:
  AUDIO_DONE  → GENERATING_VIDEO  (set at start, guards against double-processing)
  GENERATING_VIDEO → VIDEO_DONE   (set on success)
  GENERATING_VIDEO → FAILED       (set if all languages fail)
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.models import (
    AudioFile, Channel, ChannelConfig, Content, Script, VideoRender, VideoSection,
)
from app.agents.agent5_video.subagents.section_splitter import split_into_sections
from app.agents.agent5_video.subagents.section_validator import validate_sections
from app.agents.agent5_video.subagents.assembly_validator import validate_assembly
from app.agents.agent5_video.subagents.shorts_cutter import cut_shorts
from app.agents.agent5_video.services.stock_fetcher import fetch_all_sections
from app.agents.agent5_video.services.subtitles import (
    build_standard_subtitles, build_karaoke_subtitles,
)
from app.agents.agent5_video.services.remotion_builder import build_main_props, build_short_props
from app.agents.agent5_video.services.renderer import render_main_video, render_short

logger = logging.getLogger(__name__)


def run_video_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 5 video pipeline for one piece of content.

    Processes each language independently. A single-language failure is logged
    and skipped — the pipeline continues for remaining languages.

    Args:
        content_id: UUID of content with status ``AUDIO_DONE``.
        db:         SQLAlchemy session managed by the caller.

    Returns:
        ``True``  — at least one language was successfully rendered.
        ``False`` — all languages failed.
    """
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.error("Content %s not found", content_id)
        return False

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content_id)
        return False

    # Guard against double-processing
    if content.status not in ("AUDIO_DONE", "GENERATING_VIDEO"):
        logger.debug(
            "Content %s status=%s — skipping video generation",
            content_id, content.status,
        )
        return False

    if content.status == "AUDIO_DONE":
        content.status = "GENERATING_VIDEO"
        db.commit()

    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    runway_enabled      = config.runway_enabled      if config else False
    channel_style       = config.video_style_type    if config else "documentary"
    channel_color_grade = config.video_color_grade   if config else "desaturated"
    karaoke_color       = config.subtitle_karaoke_active_color if config else "#FFD700"
    shorts_label_style  = config.shorts_part_label_style       if config else "default"

    # Load all validated scripts and audio files for this content
    scripts_by_lang: dict[str, Script] = {
        s.language: s
        for s in db.query(Script)
        .filter(Script.content_id == content_id, Script.validated.is_(True))
        .all()
    }
    audio_by_lang: dict[str, AudioFile] = {
        a.language: a
        for a in db.query(AudioFile)
        .filter(AudioFile.content_id == content_id)
        .all()
    }

    if not scripts_by_lang:
        logger.error("No validated scripts for content %s", content_id)
        content.status = "FAILED"
        db.commit()
        return False

    successful = 0

    for language, script in scripts_by_lang.items():
        audio = audio_by_lang.get(language)
        if not audio:
            logger.warning("No audio file for language=%s, content=%s — skipping", language, content_id)
            continue

        try:
            ok = _process_language(
                content_id=content_id,
                language=language,
                script=script,
                audio=audio,
                channel=channel,
                runway_enabled=runway_enabled,
                channel_style=channel_style,
                channel_color_grade=channel_color_grade,
                karaoke_color=karaoke_color,
                shorts_label_style=shorts_label_style,
                db=db,
            )
            if ok:
                successful += 1
        except Exception as exc:
            logger.error(
                "Video generation failed for language=%s, content=%s: %s",
                language, content_id, exc,
            )
            db.rollback()

    if successful > 0:
        content.status = "VIDEO_DONE"
        logger.info("Video generation complete for content %s (%d language(s))", content_id, successful)
    else:
        content.status = "FAILED"
        logger.error("Video generation failed for ALL languages — content %s", content_id)

    db.commit()
    return successful > 0


# ── Per-language pipeline ──────────────────────────────────────────────────────

def _process_language(
    content_id: uuid.UUID,
    language: str,
    script: Script,
    audio: AudioFile,
    channel: Channel,
    runway_enabled: bool,
    channel_style: str,
    channel_color_grade: str,
    karaoke_color: str,
    shorts_label_style: str,
    db: Session,
) -> bool:
    """Run the full video pipeline for a single language.

    Returns:
        True on success, False on any critical failure.
    """
    logger.info("Agent 5: processing language=%s for content %s", language, content_id)

    # ── 1. Section Splitter ───────────────────────────────────────────────────
    sections = split_into_sections(
        video_script=script.video_script,
        voice_script=script.voice_script,
        duration_ms=audio.duration_ms,
        channel_niche=channel.niche or "",
        channel_tone=channel.tone or "",
        whisper_transcript=audio.whisper_transcript or [],
    )
    if not sections:
        logger.error("Section Splitter produced no sections for language=%s", language)
        return False

    # ── 2. Section Validator ──────────────────────────────────────────────────
    sections = validate_sections(
        sections=sections,
        channel_niche=channel.niche or "",
        channel_tone=channel.tone or "",
        runway_enabled=runway_enabled,
    )

    # ── 3. Save video_sections to DB ──────────────────────────────────────────
    _save_video_sections(content_id, language, sections, db)

    # ── 4. Stock fetcher ──────────────────────────────────────────────────────
    sections = fetch_all_sections(sections)

    # ── 5. Assembly Validator ─────────────────────────────────────────────────
    sections = validate_assembly(
        sections=sections,
        total_duration_ms=audio.duration_ms,
        channel_niche=channel.niche or "",
        channel_tone=channel.tone or "",
        channel_style=channel_style,
    )

    # ── 6. Shorts Cutter ──────────────────────────────────────────────────────
    breakpoints = audio.shorts_breakpoints or []
    shorts = cut_shorts(
        sections=sections,
        shorts_breakpoints=breakpoints,
        language=language,
        label_style=shorts_label_style,
    )

    # ── 7. Subtitles ──────────────────────────────────────────────────────────
    whisper = audio.whisper_transcript or []
    standard_subs = build_standard_subtitles(whisper)
    karaoke_subs  = build_karaoke_subtitles(whisper, active_color=karaoke_color)

    # ── 8. Remotion builder ───────────────────────────────────────────────────
    cid_str = str(content_id)
    main_props_path = build_main_props(
        content_id=cid_str,
        language=language,
        audio_file_path=audio.file_path,
        duration_ms=audio.duration_ms,
        sections=sections,
        standard_subtitles=standard_subs,
        shorts=shorts,
        karaoke_subtitles=karaoke_subs,
        channel_style=channel_style,
        channel_color_grade=channel_color_grade,
    )

    short_props_paths = []
    for short in shorts:
        path = build_short_props(
            content_id=cid_str,
            language=language,
            audio_file_path=audio.file_path,
            short=short,
            karaoke_subtitles=karaoke_subs,
            channel_style=channel_style,
            channel_color_grade=channel_color_grade,
        )
        short_props_paths.append((short, path))

    # ── 9. Remotion renderer ──────────────────────────────────────────────────
    main_result = render_main_video(
        content_id=cid_str,
        language=language,
        props_path=main_props_path,
        duration_ms=audio.duration_ms,
    )
    db.add(VideoRender(
        content_id=content_id,
        language=language,
        format="main",
        short_order=None,
        duration_seconds=main_result["duration_seconds"],
        hook_modified=False,
        render_time_seconds=main_result["render_time_seconds"],
    ))

    for short, props_path in short_props_paths:
        short_result = render_short(
            content_id=cid_str,
            language=language,
            short_index=short["short_index"],
            props_path=props_path,
            duration_ms=int(short["duration_sec"] * 1000),
            hook_modified=True,
        )
        db.add(VideoRender(
            content_id=content_id,
            language=language,
            format="short",
            short_order=short["short_index"],
            duration_seconds=short_result["duration_seconds"],
            hook_modified=True,
            render_time_seconds=short_result["render_time_seconds"],
        ))

    db.commit()
    logger.info(
        "Language %s done: 1 main + %d short(s) rendered for content %s",
        language, len(short_props_paths), content_id,
    )
    return True


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _save_video_sections(
    content_id: uuid.UUID,
    language: str,
    sections: list[dict],
    db: Session,
) -> None:
    """Persist validated sections to video_sections table (upsert by order)."""
    # Delete any existing rows for this content+language (re-run safety)
    db.query(VideoSection).filter(
        VideoSection.content_id == content_id,
        VideoSection.language == language,
    ).delete()

    for s in sections:
        db.add(VideoSection(
            content_id=content_id,
            language=language,
            section_order=s["section_order"],
            script_text=s.get("script_text", ""),
            audio_start_ms=s.get("audio_start_ms", 0),
            audio_end_ms=s.get("audio_end_ms", 0),
            visual_source=s.get("visual_source", "pexels"),
            search_query=s.get("search_query"),
            generation_prompt=None,
            effect=s.get("effect"),
            color_grade=s.get("color_grade"),
            runway_used=s.get("visual_source") == "runway",
            subagent_rounds=s.get("subagent_rounds", 1),
            best_attempt_used=s.get("best_attempt_used", False),
        ))

    db.flush()
    logger.info(
        "Saved %d video section(s) for language=%s, content=%s",
        len(sections), language, content_id,
    )
