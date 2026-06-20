"""Agent 5 — Rendering orchestration service.

Orchestrates the full video pipeline in two passes:

  Visual pass (ONCE per content item):
    1. Storyboard Agent  — Claude designs visual beats from the source-language
                           narration and real Whisper timestamps.
    2. Flux generation   — generate_all_beat_images() calls fal.ai Flux Schnell
                           once per beat; images saved to local cache/.
    3. Save shared beats — stored in video_sections with language="__visual__".

  Render pass (once per language, using shared beats):
    4. Re-map timing     — scale beat timestamps proportionally to this language's
                           audio duration.
    5. Save per-lang sections — (for DB audit; same Flux images, different timing).
    6. Subtitles         — standard (main) + karaoke from Whisper timestamps.
    7. Remotion builder  — write JSON props files.
    8. Remotion renderer — call Remotion CLI, save VideoRender(format="main").

  Standalone shorts — standalone short episode Content rows (is_short_episode=True):
    Short-form videos are produced ONLY by child Content rows created by
    run_shorts_planner(). Each child row has its own script, audio, Whisper, and
    storyboard remap (remap_beats_for_short()). They render with Short.tsx at
    1080×1920 (9:16) and store VideoRender(format="short",
    short_order=short_part_number-1).
    Agent 6 queries:
      Long videos: VideoRender.format=="main" WHERE content.is_short_episode==False
      Shorts:      VideoRender.format=="short" WHERE content.is_short_episode==True

Re-entrancy — each step is skipped when its output already exists:
  • Main MP4 on disk + VideoRender in DB  → language fully done, skip all
  • Props JSON on disk                    → skip steps 4-8, go directly to render
  • Shared beats in DB (language=__visual__)
                                          → skip steps 1-3, re-use stored beats

Status transitions:
  AUDIO_DONE       → GENERATING_VIDEO  (set at start, guards against double-processing)
  GENERATING_VIDEO → VIDEO_DONE        (set on full success)
  GENERATING_VIDEO → FAILED            (set if all languages fail)
"""

import json
import logging
import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AudioFile, Channel, ChannelConfig, Content, Script, VideoRender, VideoSection,
)
from app.agents.agent4_visuals.subagents.section_splitter import split_into_sections
from app.agents.agent4_visuals.subagents.storyboard import split_into_beats, remap_beats_for_short
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard
from app.agents.agent4_visuals.services.flux_generator import generate_all_beat_images
from app.agents.agent5_render.services.subtitles import (
    build_standard_subtitles, build_karaoke_subtitles,
)
from app.agents.agent5_render.services.remotion_builder import build_main_props, build_short_props
from app.agents.agent5_render.services.renderer import (
    render_main_video, render_main_video_chunked, render_short,
    ensure_bundle,
    RemotionCrashError, RemotionRenderError,
)
from app.agents.agent5_render.services.verify import verify_render
from app.agents.agent4_visuals.system_prompt import (
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION,
    enrich_sections_with_visuals,
)

logger = logging.getLogger(__name__)


class VerifyFailedError(RuntimeError):
    """Post-render verification caught a broken render (black frames / silence / bad resolution)."""


# Language sentinel used to store the shared visual-pass beats (generated once,
# shared by all language renders). Must match the migration's widened varchar(16).
_VISUAL_LANGUAGE = "__visual__"

# text_card sentinel — set on a beat's media_url when Flux generation failed.
_TEXT_CARD_SENTINEL = "__text_card__"

# Beats whose media_url is one of these are not counted as "real media" for the
# technical-blocker check (but text_card is a valid visual type, not an error).
_PLACEHOLDER_URLS: frozenset[str] = frozenset({_TEXT_CARD_SENTINEL})

# Technical-blocker threshold: >50% of beats with no real media → block render.
_MISSING_MEDIA_BLOCK_RATIO = 0.50


def run_video_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the Agent 5 render pipeline for one piece of content.

    Visual pass (storyboard + Flux) runs ONCE per content, then each language
    gets its own render pass using the shared beat images. A single-language
    render failure is logged and skipped — the pipeline continues for remaining
    languages.

    Args:
        content_id: UUID of content with status ``AUDIO_DONE`` or ``GENERATING_VIDEO``.
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
    channel_style         = config.video_style_type              if config else "documentary"
    channel_color_grade   = config.video_color_grade             if config else "desaturated"
    karaoke_color         = config.subtitle_karaoke_active_color if config else "#FFD700"
    script_format         = config.script_format                 if config else "youtube_long"
    allow_legacy_fallback = config.allow_legacy_fallback         if config else False

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

    is_short_episode: bool = bool(getattr(content, "is_short_episode", False))
    parent_content_id = getattr(content, "parent_content_id", None)

    # Compute Short-episode render params once (used in per-language loop)
    short_order: int | None = None
    short_total_parts: int | None = None
    if is_short_episode:
        _part_num    = getattr(content, "short_part_number", None) or 1
        short_order  = _part_num - 1
        short_total_parts = getattr(content, "short_total_parts", None)

    # ── Visual pass — storyboard + Flux ONCE per content ──────────────────────
    # Short episodes skip this pass entirely — they remap parent beats per-language
    # inside the render loop via remap_beats_for_short().
    shared_beats: list[dict] | None
    source_duration_ms: int

    if is_short_episode:
        if not parent_content_id:
            logger.error(
                "Short episode content=%s has no parent_content_id — marking FAILED",
                content_id,
            )
            content.status = "FAILED"
            db.commit()
            return False

        # Gate: the remap pass requires the parent's __visual__ VideoSection rows.
        # Those rows are written at the end of _run_visual_pass() — they exist only
        # after the parent's storyboard+Flux generation is complete, independently of
        # whether the parent's final render has finished.  If they are not yet present,
        # defer this Short episode by reverting to AUDIO_DONE so that pickup_audio_done()
        # re-queues it on the next Beat cycle.  This is a normal wait, not an error.
        _parent_visual_ready: bool = (
            db.query(VideoSection)
            .filter(
                VideoSection.content_id == parent_content_id,
                VideoSection.language   == _VISUAL_LANGUAGE,
            )
            .limit(1)
            .first()
        ) is not None

        if not _parent_visual_ready:
            logger.warning(
                "CHILD_SHORT_VISUALS_DEFERRED content_id=%s reason=parent_visuals_missing "
                "parent_content_id=%s",
                content_id, parent_content_id,
            )
            content.status = "AUDIO_DONE"
            db.commit()
            return False

        logger.info(
            "Visual pass: SHORT EPISODE — parent __visual__ ready, "
            "will remap beats per-language (content=%s parent=%s)",
            content_id, parent_content_id,
        )
        shared_beats = []
        source_duration_ms = 0
    else:
        shared_beats = _load_shared_beats(content_id, db)

        # Beats saved after storyboard (before Flux) have media_url == "".
        # Detect this: if any beat is missing a media_url, Flux didn't finish last run.
        flux_incomplete = shared_beats and any(
            not beat.get("media_url") for beat in shared_beats
        )

        if not shared_beats:
            shared_beats, source_duration_ms = _run_visual_pass(
                content_id=content_id,
                scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang,
                channel=channel,
                script_format=script_format,
                allow_legacy_fallback=allow_legacy_fallback,
                db=db,
            )
            if shared_beats is None:
                content.status = "FAILED"
                db.commit()
                return False
        else:
            source_duration_ms = max(
                (b.get("audio_end_ms", 0) for b in shared_beats), default=0
            )
            if flux_incomplete:
                logger.info(
                    "Visual pass: %d beats loaded, Flux incomplete — re-running Flux only (content=%s)",
                    len(shared_beats), content_id,
                )
                shared_beats = generate_all_beat_images(shared_beats, str(content_id))
                _save_shared_beats(content_id, shared_beats, db)
                db.commit()
            else:
                logger.info(
                    "Visual pass: re-using %d stored beats from previous run (content=%s)",
                    len(shared_beats), content_id,
                )

        if source_duration_ms == 0:
            # Fallback: use source language audio duration
            src_audio = audio_by_lang.get(content.source_language)
            source_duration_ms = src_audio.duration_ms if src_audio else 0

    # ── Render pass — per language ────────────────────────────────────────────
    successful = 0

    for language, script in scripts_by_lang.items():
        audio = audio_by_lang.get(language)
        if not audio:
            logger.warning(
                "No audio for language=%s content=%s — skipping render",
                language, content_id,
            )
            continue

        if is_short_episode:
            # Short episodes remap parent beats directly to this language's narration
            if not parent_content_id:
                logger.error(
                    "Short episode content=%s lang=%s has no parent_content_id — cannot remap beats",
                    content_id, language,
                )
                continue
            logger.info(
                "CHILD_SHORT_VISUALS_START content_id=%s parent_content_id=%s language=%s",
                content_id, parent_content_id, language,
            )
            logger.info(
                "CHILD_SHORT_RENDER_START content_id=%s parent_content_id=%s "
                "part=%s/%s language=%s format=short resolution=1080x1920",
                content_id, parent_content_id,
                getattr(content, "short_part_number", None),
                short_total_parts,
                language,
            )
            beats_for_lang = remap_beats_for_short(
                short_content=content,
                short_voice_script=script.voice_script,
                short_audio_file=audio,
                parent_content_id=parent_content_id,
                db=db,
            )
            if not beats_for_lang:
                logger.error(
                    "Agent5 [FAIL] lang=%s content=%s status=SHORT_REMAP_EMPTY "
                    "reason=remap_beats_for_short returned no beats",
                    language, content_id,
                )
                continue
        else:
            # Scale beat timestamps to this language's audio duration
            beats_for_lang = _remap_beats_timing(
                shared_beats, audio.duration_ms, source_duration_ms
            )

        try:
            ok = _process_language(
                content_id=content_id,
                language=language,
                script=script,
                audio=audio,
                beats=beats_for_lang,
                channel=channel,
                channel_style=channel_style,
                channel_color_grade=channel_color_grade,
                karaoke_color=karaoke_color,
                script_format=script_format,
                db=db,
                is_short_episode=is_short_episode,
                short_order=short_order,
                short_total_parts=short_total_parts,
            )
            if ok:
                successful += 1
        except (RemotionCrashError, RemotionRenderError) as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=%s details=%s",
                language, content_id, type(exc).__name__, str(exc)[:200],
            )
            db.rollback()
        except Exception as exc:
            logger.error(
                "Video generation failed for language=%s, content=%s: %s",
                language, content_id, exc,
            )
            db.rollback()

    if successful > 0:
        content.status = "VIDEO_DONE"
        logger.info(
            "Video generation complete for content %s (%d language(s))",
            content_id, successful,
        )
    else:
        content.status = "FAILED"
        logger.error("Video generation failed for ALL languages — content %s", content_id)

    db.commit()
    return successful > 0


# ── Visual pass helpers ────────────────────────────────────────────────────────

def _run_visual_pass(
    content_id: uuid.UUID,
    scripts_by_lang: dict[str, Script],
    audio_by_lang: dict[str, AudioFile],
    channel: Channel,
    script_format: str,
    allow_legacy_fallback: bool,
    db: Session,
) -> tuple[list[dict] | None, int]:
    """Generate storyboard + Flux images once for this content item.

    Uses the source language script/audio for storyboard generation (so hints
    are in the same language as the Whisper transcript). All language renders
    share the resulting beat images; timing is re-scaled per language.

    Returns:
        ``(beats, source_duration_ms)`` on success, ``(None, 0)`` on failure.
    """
    cid_str = str(content_id)

    # Pick source language (prefer content.source_language, fall back to any)
    source_lang   = next(iter(scripts_by_lang))
    source_script = scripts_by_lang[source_lang]
    source_audio  = audio_by_lang.get(source_lang)

    # If source language has no audio, try any language that has audio
    if not source_audio:
        for lang, audio in audio_by_lang.items():
            if lang in scripts_by_lang:
                source_lang   = lang
                source_script = scripts_by_lang[lang]
                source_audio  = audio
                break

    if not source_audio:
        logger.error("No audio available for any language in content %s", content_id)
        return None, 0

    source_duration_ms = source_audio.duration_ms
    logger.info(
        "PARENT_VISUALS_START content_id=%s source_lang=%s source_duration_ms=%d",
        content_id, source_lang, source_duration_ms,
    )
    logger.info(
        "Agent5 [VISUAL_PASS] content=%s source_lang=%s "
        "source_duration_ms=%d schema_version=%s",
        content_id, source_lang, source_duration_ms, _STORYBOARD_SCHEMA_VERSION,
    )

    # ── 1. Storyboard ─────────────────────────────────────────────────────────
    beats = split_into_beats(
        voice_script=source_script.voice_script,
        duration_ms=source_audio.duration_ms,
        channel=channel,
        script_format=script_format,
        whisper_transcript=source_audio.whisper_transcript or [],
        allow_legacy_fallback=allow_legacy_fallback,
        language=source_lang,
    )

    if beats is None:
        if allow_legacy_fallback:
            logger.warning(
                "Storyboard failed for source_lang=%s — allow_legacy_fallback=True, "
                "using section splitter",
                source_lang,
            )
            beats = _legacy_section_fallback(
                source_script, source_audio, channel, script_format,
            )
            if not beats:
                logger.error(
                    "Agent5 [FAIL] content=%s status=STORYBOARD_FAILED "
                    "reason=legacy_splitter_empty",
                    content_id,
                )
                return None, 0
        else:
            logger.error(
                "Agent5 [FAIL] content=%s status=STORYBOARD_FAILED "
                "reason=storyboard_generation_failed (allow_legacy_fallback=False)",
                content_id,
            )
            return None, 0

    beats = _cleanup_micro_beats(beats, script_format)
    logger.info(
        "Agent5 [STORYBOARD] content=%s beats=%d source_lang=%s",
        content_id, len(beats), source_lang,
    )

    # ── 1b. Storyboard validation gate ────────────────────────────────────────
    # Runs after storyboard is complete and before any fal.ai calls.
    beats = _run_storyboard_validation(
        beats=beats,
        voice_script=source_script.voice_script,
        source_audio=source_audio,
        channel=channel,
        script_format=script_format,
        allow_legacy_fallback=allow_legacy_fallback,
        source_lang=source_lang,
    )
    if beats is None:
        logger.error(
            "Agent5 [FAIL] content=%s status=STORYBOARD_VALIDATION_FAILED "
            "reason=storyboard_validation_gate_returned_None (allow_legacy_fallback=False)",
            content_id,
        )
        return None, 0

    # ── 2. Save storyboard beats before Flux — protects storyboard work ─────────
    # If Flux crashes mid-run, --from-video can reload these beats and skip straight
    # to Flux retry (file cache handles already-generated images).
    _save_shared_beats(content_id, beats, db)
    db.commit()

    # ── 3. Flux generation ────────────────────────────────────────────────────
    beats = generate_all_beat_images(beats, cid_str)

    succeeded = sum(1 for b in beats if (b.get("media_url") or "").startswith("cache/"))
    text_card_count = len(beats) - succeeded
    logger.info(
        "Agent5 [FLUX_DONE] content=%s beats=%d flux_ok=%d text_card=%d",
        content_id, len(beats), succeeded, text_card_count,
    )

    # ── 4. Update saved beats with Flux media_url ─────────────────────────────
    _save_shared_beats(content_id, beats, db)
    db.commit()
    logger.info("PARENT_VISUALS_DONE content_id=%s beats=%d", content_id, len(beats))

    return beats, source_duration_ms


def _run_storyboard_validation(
    beats: list[dict],
    voice_script: str,
    source_audio: "AudioFile",
    channel: Channel,
    script_format: str,
    allow_legacy_fallback: bool,
    source_lang: str,
) -> list[dict] | None:
    """Run the storyboard validation gate; retry once on MAJOR issues.

    MAJOR issues trigger one full storyboard re-run with the issues appended
    as constraints in the user message. If still MAJOR after retry: log ERROR
    and proceed — the pipeline is never blocked. MINOR issues are logged at
    WARNING only.

    Returns the (possibly re-generated) beat list, or None on catastrophic
    validation failure (only when allow_legacy_fallback=False and storyboard
    retry also fails to produce any beats).
    """
    issues = validate_storyboard(beats)
    minor_issues = [i for i in issues if i["severity"] == "MINOR"]
    major_issues = [i for i in issues if i["severity"] == "MAJOR"]

    for issue in minor_issues:
        logger.warning(
            "Storyboard MINOR: beat=%d check=%s — %s",
            issue["beat_order"], issue["check"], issue["description"][:200],
        )

    if not major_issues:
        return beats

    # MAJOR issues found — build constraint text and retry the storyboard once.
    # NOTE: this is a FULL-storyboard retry (all segments), not segment-level, because
    # beat-to-segment provenance is discarded after the merge step. Monitor
    # "full-storyboard retry" in operator logs; if it fires frequently, implement
    # segment-level retry by tracking provenance through split_into_beats().
    constraint_lines = "\n".join(
        f"- [{iss['check']}] beat_order={iss['beat_order']}: {iss['description']}"
        for iss in major_issues
    )
    n_segments = max(1, len(re.findall(
        r"^\s*\[(?:INTRO|OUTRO|SECTION[^\]]*)\]", voice_script,
        re.IGNORECASE | re.MULTILINE,
    )))
    logger.warning(
        "Full-storyboard retry triggered due to %d MAJOR issue(s) — "
        "re-running all %d segment(s). Consider segment-level retry if this fires frequently. "
        "checks=%s",
        len(major_issues), n_segments, [i["check"] for i in major_issues],
    )
    logger.error(
        "Storyboard MAJOR issue(s) found — retrying storyboard with constraints. "
        "MAJOR_count=%d checks=%s",
        len(major_issues), [i["check"] for i in major_issues],
    )

    retry_beats = split_into_beats(
        voice_script=voice_script,
        duration_ms=source_audio.duration_ms,
        channel=channel,
        script_format=script_format,
        whisper_transcript=source_audio.whisper_transcript or [],
        allow_legacy_fallback=allow_legacy_fallback,
        language=source_lang,
        storyboard_constraints=constraint_lines,
    )

    if retry_beats is None:
        logger.error(
            "Storyboard retry failed to produce beats — proceeding with original storyboard "
            "despite MAJOR issues (pipeline not blocked per spec)"
        )
        return beats

    retry_beats = _cleanup_micro_beats(retry_beats, script_format)
    retry_issues = validate_storyboard(retry_beats)
    retry_majors = [i for i in retry_issues if i["severity"] == "MAJOR"]

    if retry_majors:
        logger.error(
            "Storyboard still has MAJOR issues after retry (%d remaining) — "
            "proceeding with retry result (pipeline not blocked per spec). "
            "checks=%s",
            len(retry_majors), [i["check"] for i in retry_majors],
        )
    else:
        logger.info(
            "Storyboard retry resolved all MAJOR issues — %d beats after retry",
            len(retry_beats),
        )

    return retry_beats


def _legacy_section_fallback(
    script: Script,
    audio: AudioFile,
    channel: Channel,
    script_format: str,
) -> list[dict] | None:
    """Build sections via the legacy splitter + enrichment when the storyboard fails.

    Returns a list of section dicts with ``flux_prompt`` synthesized from the
    section's search_query (so Flux generation can still run), or ``None`` on error.
    """
    try:
        sections = split_into_sections(
            video_script=script.video_script,
            voice_script=script.voice_script,
            duration_ms=audio.duration_ms,
            channel_niche=channel.niche or "",
            channel_tone=channel.tone or "",
            whisper_transcript=audio.whisper_transcript or [],
        )
        if not sections:
            return None
        sections = enrich_sections_with_visuals(
            sections, channel.niche or "", channel.tone or ""
        )
        return sections
    except Exception as exc:
        logger.error("Legacy section fallback failed: %s", exc)
        return None


def _load_shared_beats(content_id: uuid.UUID, db: Session) -> list[dict]:
    """Load the shared visual-pass beats stored under language='__visual__'."""
    return _load_sections_from_db(content_id, _VISUAL_LANGUAGE, db)


def _save_shared_beats(content_id: uuid.UUID, beats: list[dict], db: Session) -> None:
    """Persist visual-pass beats under language='__visual__'."""
    _save_video_sections(content_id, _VISUAL_LANGUAGE, beats, db)


def _remap_beats_timing(
    beats: list[dict],
    target_duration_ms: int,
    source_duration_ms: int,
) -> list[dict]:
    """Return a copy of beats with timestamps scaled to target_duration_ms.

    When all languages have identical audio duration (common for single-language
    channels), this is a no-op. For multilingual content the proportional scaling
    preserves relative beat pacing across language renders.

    Args:
        beats:              Source beats from the visual pass.
        target_duration_ms: This language's audio duration.
        source_duration_ms: Duration of the source audio used for storyboard generation.

    Returns:
        New list of beat dicts with re-scaled audio_start_ms / audio_end_ms.
    """
    if source_duration_ms == 0 or source_duration_ms == target_duration_ms:
        return list(beats)

    ratio = target_duration_ms / source_duration_ms
    result: list[dict] = []
    for b in beats:
        new_beat = dict(b)
        new_beat["audio_start_ms"] = int(b.get("audio_start_ms", 0) * ratio)
        new_beat["audio_end_ms"]   = int(b.get("audio_end_ms",   0) * ratio)
        new_beat["duration_sec"]   = (
            new_beat["audio_end_ms"] - new_beat["audio_start_ms"]
        ) / 1000
        result.append(new_beat)

    # Clamp last beat to exactly target_duration_ms
    if result:
        last = result[-1]
        last["audio_end_ms"] = target_duration_ms
        last["duration_sec"] = (target_duration_ms - last["audio_start_ms"]) / 1000

    return result


# ── Per-language render pass ───────────────────────────────────────────────────

# Part-label templates for Standalone short architecture child short episodes (Short.tsx overlay text).
_PART_LABEL_TEMPLATES: dict[str, str] = {
    "fr": "Partie {n}/{total}",
    "es": "Parte {n}/{total}",
    "it": "Parte {n}/{total}",
    "pt": "Parte {n}/{total}",
}
_PART_LABEL_DEFAULT = "Part {n} of {total}"


def _build_part_label(language: str, part_number: int, total_parts: int) -> str:
    """Return a localised part label for a Short episode overlay."""
    template = _PART_LABEL_TEMPLATES.get(language[:2].lower(), _PART_LABEL_DEFAULT)
    return template.format(n=part_number, total=total_parts)


def _process_language(
    content_id: uuid.UUID,
    language: str,
    script: Script,
    audio: AudioFile,
    beats: list[dict],
    channel: Channel,
    channel_style: str,
    channel_color_grade: str,
    karaoke_color: str,
    script_format: str,
    db: Session,
    is_short_episode: bool = False,
    short_order: int | None = None,
    short_total_parts: int | None = None,
) -> bool:
    """Run the render pipeline for one language using pre-generated beat images.

    Beats arrive with ``media_url`` already set to a local cache/ path (or
    ``"__text_card__"`` for fallback). This function handles: per-language
    section DB storage, subtitles, Remotion props, render, and verification.

    For parent content (``is_short_episode=False``): renders MainVideo.tsx at
    1920×1080 (16:9) and stores ``VideoRender(format="main")``.

    For Standalone short architecture child short episodes (``is_short_episode=True``): renders
    Short.tsx at 1080×1920 (9:16) and stores ``VideoRender(format="short")``.

    Args:
        beats:            Beat dicts with media_url from the visual pass.
        is_short_episode: True for Standalone short architecture child short episode Content rows.
        short_order:      0-based short index (``short_part_number - 1``).
        short_total_parts: Total number of parts in the series.

    Returns:
        True on success, False on any critical failure.
    """
    cid_str    = str(content_id)
    media_root = Path(settings.media_path).resolve()
    props_dir  = media_root / "remotion_props"
    render_fmt = "short" if is_short_episode else "main"

    logger.info(
        "Agent5 [RENDER_START] language=%s content=%s "
        "beats=%d audio_duration_ms=%d format=%s",
        language, content_id, len(beats), audio.duration_ms, render_fmt,
    )

    # ── Phase check 1: already fully rendered? ─────────────────────────────────
    if _is_rendered(content_id, language, cid_str, media_root, db,
                    fmt=render_fmt, short_order=short_order):
        logger.info(
            "Agent5 [DONE] language=%s content=%s status=ALREADY_RENDERED",
            language, content_id,
        )
        return True

    # ── Phase check 2: props on disk → skip to render ─────────────────────────
    if is_short_episode:
        existing_props_file = props_dir / f"{cid_str}_{language}_short_{short_order}.json"
    else:
        existing_props_file = props_dir / f"{cid_str}_{language}_main.json"

    if existing_props_file.exists():
        logger.info("Props found on disk for language=%s — skipping to render", language)
        try:
            return _render_from_existing_props(
                content_id, language, audio, cid_str, props_dir, db,
                is_short_episode=is_short_episode,
                short_order=short_order,
                short_total_parts=short_total_parts,
            )
        except VerifyFailedError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=VERIFY_FAILED "
                "reason=POST_RENDER_VERIFICATION (existing-props path) details=%s",
                language, content_id, str(exc)[:300],
            )
            _content_row = db.get(Content, content_id)
            if _content_row:
                _content_row.status = "NEEDS_REVIEW"
                db.commit()
            return False

    # ── Save per-language sections (for DB audit/inspection) ──────────────────
    _save_video_sections(content_id, language, beats, db)
    db.commit()
    if is_short_episode:
        logger.info(
            "CHILD_SHORT_VISUALS_DONE content_id=%s language=%s beats=%d",
            content_id, language, len(beats),
        )

    # ── Technical blocker check ────────────────────────────────────────────────
    standard_subs = build_standard_subtitles(audio.whisper_transcript or [])
    karaoke_subs  = build_karaoke_subtitles(audio.whisper_transcript or [], active_color=karaoke_color)

    blockers = _collect_technical_blockers(beats, standard_subs, audio)
    if blockers:
        logger.error(
            "Agent5 [FAIL] language=%s content=%s status=RENDER_BLOCKED "
            "blockers=%s",
            language, content_id, blockers,
        )
        return False

    # ── Pre-render props sanity check ──────────────────────────────────────────
    _sanity_ok, _sanity_reason = _check_props_sanity(beats, audio.duration_ms)
    if not _sanity_ok:
        logger.error(
            "Agent5 [FAIL] language=%s content=%s status=INVALID_PROPS reason=%s",
            language, content_id, _sanity_reason,
        )
        return False

    # ── Remotion builder ───────────────────────────────────────────────────────
    if is_short_episode:
        # Standalone short architecture child short episode — Short.tsx (9:16, 1080×1920)
        part_number = (short_order or 0) + 1
        short_dict = {
            "short_index":  short_order,
            "start_ms":     0,
            "end_ms":       audio.duration_ms,
            "sections":     beats,
            "part_label":   _build_part_label(language, part_number, short_total_parts or 1),
            "total_parts":  short_total_parts or 1,
        }
        props_path = build_short_props(
            content_id=cid_str,
            language=language,
            audio_file_path=audio.file_path,
            short=short_dict,
            karaoke_subtitles=karaoke_subs,
            channel_style=channel_style,
            channel_color_grade=channel_color_grade,
        )
    else:
        # Parent content — MainVideo.tsx (16:9, 1920×1080)
        # Standalone short architecture only: parent content never generates short props.
        # Shorts are produced exclusively by standalone Standalone short architecture short episode Content rows.
        logger.info(
            "STANDALONE_SHORTS_ONLY content_id=%s language=%s "
            "parent_cut_shorts_removed=True shorts_path=standalone_episodes_only",
            content_id, language,
        )
        props_path = build_main_props(
            content_id=cid_str,
            language=language,
            audio_file_path=audio.file_path,
            duration_ms=audio.duration_ms,
            sections=beats,
            standard_subtitles=standard_subs,
            karaoke_subtitles=karaoke_subs,
            channel_style=channel_style,
            channel_color_grade=channel_color_grade,
        )

    # ── PRE_RENDER_ASSET_AUDIT — local images only ────────────────────────────
    _all_remote = _audit_props_for_remote_urls(props_path, render_fmt)

    if _all_remote:
        logger.error(
            "Agent5 [FAIL] language=%s content=%s status=REMOTE_URL_IN_PROPS "
            "reason=ASSET_AUDIT_FAILED remote_samples=%s",
            language, content_id, [u[:80] for u in _all_remote[:5]],
        )
        return False

    logger.debug(
        "Agent5 [PRE_RENDER] language=%s content=%s "
        "beats=%d duration_ms=%d "
        "standard_captions=%d karaoke_chunks=%d format=%s",
        language, content_id,
        len(beats), audio.duration_ms,
        len(standard_subs), len(karaoke_subs),
        render_fmt,
    )

    # ── Renderer ───────────────────────────────────────────────────────────────
    if is_short_episode:
        try:
            result = _run_short_render(
                content_id=content_id,
                language=language,
                cid_str=cid_str,
                audio=audio,
                short_order=short_order or 0,
                props_path=props_path,
                db=db,
            )
        except VerifyFailedError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=VERIFY_FAILED "
                "reason=POST_RENDER_VERIFICATION details=%s",
                language, content_id, str(exc)[:300],
            )
            _content_row = db.get(Content, content_id)
            if _content_row:
                _content_row.status = "NEEDS_REVIEW"
                db.commit()
            return False
        except (RemotionCrashError, RemotionRenderError) as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=%s details=%s",
                language, content_id, type(exc).__name__, str(exc)[:200],
            )
            return False
        logger.info(
            "CHILD_SHORT_RENDER_DONE path=%s duration_ms=%d",
            result["file_path"], audio.duration_ms,
        )
    else:
        try:
            _run_renders(
                content_id=content_id,
                language=language,
                cid_str=cid_str,
                audio=audio,
                main_props_path=props_path,
                db=db,
            )
        except VerifyFailedError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=VERIFY_FAILED "
                "reason=POST_RENDER_VERIFICATION details=%s",
                language, content_id, str(exc)[:300],
            )
            _content_row = db.get(Content, content_id)
            if _content_row:
                _content_row.status = "NEEDS_REVIEW"
                db.commit()
            return False
        except RemotionCrashError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=PAGE_CRASHED details=%s",
                language, content_id, str(exc)[:300],
            )
            return False
        except RemotionRenderError as exc:
            logger.error(
                "Agent5 [FAIL] language=%s content=%s status=REMOTION_FAILED "
                "reason=RENDER_ERROR details=%s",
                language, content_id, str(exc)[:300],
            )
            return False

    logger.info(
        "Agent5 [DONE] language=%s content=%s status=SUCCESS",
        language, content_id,
    )
    return True


# ── Micro-beat cleanup ─────────────────────────────────────────────────────────

_MIN_BEAT_MS_NORMAL       = 2000
_MIN_BEAT_MS_TEXT_OVERLAY = 1500
_MIN_BEAT_MS_CUT_ACTION   = 500


def _cleanup_micro_beats(sections: list[dict], script_format: str) -> list[dict]:
    """Merge beats shorter than the minimum duration into their neighbour.

    Args:
        sections:     Beat-section dicts with timing fields.
        script_format: Format key — reserved for future format-aware floors.

    Returns:
        Possibly-shorter section list with no micro-beats (except cut+action).
    """
    if not sections:
        return sections

    result = list(sections)
    exception_budget = 1

    changed = True
    while changed and len(result) > 1:
        changed = False
        for i in range(len(result)):
            s      = result[i]
            dur_ms = s.get("audio_end_ms", 0) - s.get("audio_start_ms", 0)
            vtype  = s.get("visual_type", "b-roll")
            effect = s.get("effect", "slow_zoom")

            min_ms = _MIN_BEAT_MS_TEXT_OVERLAY if vtype == "text_overlay" else _MIN_BEAT_MS_NORMAL
            if dur_ms >= min_ms:
                continue

            if effect == "cut" and vtype == "action" and exception_budget > 0:
                exception_budget -= 1
                continue

            absorber_idx = (i - 1) if i > 0 else (i + 1)
            if absorber_idx >= len(result):
                continue

            absorber = result[absorber_idx]
            if absorber_idx < i:
                absorber["audio_end_ms"] = s["audio_end_ms"]
            else:
                absorber["audio_start_ms"] = s["audio_start_ms"]
            absorber["duration_sec"] = (
                (absorber["audio_end_ms"] - absorber["audio_start_ms"]) / 1000
            )
            result.pop(i)
            changed = True
            break

    for new_order, s in enumerate(result):
        s["section_order"] = new_order
        if "beat_order" in s:
            s["beat_order"] = new_order

    logger.info(
        "Micro-beat cleanup: beats_before=%d beats_after=%d merged=%d",
        len(sections), len(result), len(sections) - len(result),
    )
    return result


# ── Render decision helpers ────────────────────────────────────────────────────

def _collect_technical_blockers(
    sections: list[dict],
    standard_subs: list[dict],
    audio: "AudioFile",
) -> list[str]:
    """Return technical blocker descriptions preventing a viable render.

    Args:
        sections:     Beat/section dicts after Flux generation.
        standard_subs: Standard subtitle chunks.
        audio:        AudioFile ORM record.

    Returns:
        List of blocker strings (empty = render is viable).
    """
    blockers: list[str] = []

    if not sections:
        blockers.append("no_beats")
        return blockers

    # Beats without real media (neither a local Flux image nor a text_card)
    no_media = sum(
        1 for s in sections
        if s.get("visual_type") != "text_card"
        and not (s.get("media_url", "") or "").startswith("cache/")
    )
    if no_media / len(sections) > _MISSING_MEDIA_BLOCK_RATIO:
        blockers.append(
            f"missing_media_critical ({no_media}/{len(sections)} beats have no media)"
        )

    whisper = audio.whisper_transcript or []
    if not standard_subs and whisper:
        blockers.append("no_captions (Whisper transcript present but caption build failed)")

    return blockers


def _check_props_sanity(sections: list[dict], duration_ms: int) -> tuple[bool, str]:
    """Validate sections for structural correctness before building props.

    Args:
        sections:    Section dicts as they will appear in the props JSON.
        duration_ms: Expected audio duration in milliseconds.

    Returns:
        ``(True, "")`` if all checks pass; ``(False, reason_string)`` otherwise.
    """
    import math

    errors: list[str] = []
    seen_orders: set  = set()
    max_end_ms        = 0

    for i, s in enumerate(sections):
        order = s.get("section_order", i)

        if order in seen_orders:
            errors.append(f"Duplicate section_order={order}")
        seen_orders.add(order)

        start = s.get("audio_start_ms")
        end   = s.get("audio_end_ms")

        if start is None or end is None:
            errors.append(f"Section {order}: missing audio_start_ms or audio_end_ms")
        elif not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            errors.append(f"Section {order}: non-numeric timing")
        elif math.isnan(start) or math.isnan(end):
            errors.append(f"Section {order}: NaN timing")
        elif start < 0 or end <= 0:
            errors.append(f"Section {order}: negative/zero timing")
        elif end <= start:
            errors.append(f"Section {order}: end ≤ start ({end} ≤ {start})")
        else:
            max_end_ms = max(max_end_ms, int(end))

        url   = s.get("media_url", "")
        vtype = s.get("visual_type", "b-roll")
        if vtype != "text_card":
            if url.startswith("http"):
                errors.append(f"Section {order}: remote URL survived to props: {url[:80]!r}")
            elif url and not url.startswith("cache/"):
                errors.append(f"Section {order}: unexpected non-local media_url: {url[:80]!r}")

        if len(errors) >= 5:
            break

    if len(errors) < 5 and duration_ms > 0 and max_end_ms > 0:
        drift_pct = abs(max_end_ms - duration_ms) / duration_ms * 100
        if drift_pct > 20:
            errors.append(
                f"Duration drift {drift_pct:.1f}%: sections end at {max_end_ms}ms "
                f"but audio is {duration_ms}ms"
            )

    if errors:
        return False, "; ".join(errors[:5])
    return True, ""


def _audit_props_for_remote_urls(props_path: str, label: str) -> list[str]:
    """Scan a props JSON file and return all remote URLs found (http/https).

    Under the Flux architecture every URL should be a local cache/ path.

    Args:
        props_path: Absolute path to a props JSON file.
        label:      "main" or "short" — used only for log messages.

    Returns:
        List of remote URLs found (empty means audit passed).
    """
    try:
        with open(props_path, encoding="utf-8") as fh:
            props = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("PRE_RENDER_ASSET_AUDIT: cannot open %s props=%s: %s", label, props_path, exc)
        return []

    remote: list[str] = []

    def _scan_sections(secs: list) -> None:
        for sec in secs:
            for clip in sec.get("clips") or []:
                u = clip.get("url", "")
                if u.startswith("http"):
                    remote.append(u)
            u = sec.get("media_url", "")
            if u.startswith("http"):
                remote.append(u)

    _scan_sections(props.get("sections") or [])
    if remote:
        logger.warning(
            "PRE_RENDER_ASSET_AUDIT: %s props=%s remote_urls_found=%d samples=%s",
            label, props_path, len(remote), [u[:80] for u in remote[:3]],
        )
    return remote


# ── Phase-skip helpers ─────────────────────────────────────────────────────────

def _is_rendered(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    media_root: Path,
    db: Session,
    fmt: str = "main",
    short_order: int | None = None,
) -> bool:
    """Return True if the output MP4 exists on disk AND a VideoRender row is in DB."""
    q = db.query(VideoRender).filter(
        VideoRender.content_id == content_id,
        VideoRender.language   == language,
        VideoRender.format     == fmt,
    )
    if short_order is not None:
        q = q.filter(VideoRender.short_order == short_order)
    row = q.first()
    if not row:
        return False
    if fmt == "short" and short_order is not None:
        mp4 = media_root / "video" / cid_str / f"{language}_short_{short_order}.mp4"
    else:
        mp4 = media_root / "video" / cid_str / f"{language}_main.mp4"
    return mp4.exists()


def _run_short_render(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    audio: "AudioFile",
    short_order: int,
    props_path: str,
    db: Session,
    concurrency: int | None = None,
) -> dict:
    """Render a Standalone short architecture child short episode with Short.tsx (9:16, 1080×1920).

    Args:
        content_id:  UUID of the child short episode Content row.
        language:    Language code.
        cid_str:     str(content_id).
        audio:       AudioFile record for this language.
        short_order: 0-based index (short_part_number - 1).
        props_path:  Absolute path to the short props JSON file.
        db:          SQLAlchemy session.
        concurrency: Chromium tab concurrency override.

    Returns:
        Result dict with file_path, duration_seconds, render_time_seconds.

    Raises:
        RemotionCrashError:  Chromium crashed.
        RemotionRenderError: Remotion exited non-zero.
        VerifyFailedError:   Post-render verification failed.
    """
    bundle_dir = ensure_bundle()
    result = render_short(
        content_id=cid_str,
        language=language,
        short_index=short_order,
        props_path=props_path,
        duration_ms=audio.duration_ms,
        concurrency=concurrency,
        bundle_dir=bundle_dir,
    )

    if settings.verify_renders:
        issues = verify_render(
            mp4_path=result["file_path"],
            expected_duration_ms=None,   # Shorts skip duration check (no bookend padding)
            fmt="short",
        )
        if issues:
            raise VerifyFailedError(
                f"Short render verification failed for language={language} "
                f"short_order={short_order}: {issues}"
            )

    db.add(VideoRender(
        content_id=content_id,
        language=language,
        format="short",
        short_order=short_order,
        duration_seconds=result["duration_seconds"],
        render_time_seconds=result["render_time_seconds"],
    ))
    db.commit()
    logger.info(
        "Short %d rendered: language=%s duration=%.1fs render_time=%.1fs",
        short_order, language,
        result["duration_seconds"], result["render_time_seconds"],
    )
    return result


def _render_from_existing_props(
    content_id: uuid.UUID,
    language: str,
    audio: AudioFile,
    cid_str: str,
    props_dir: Path,
    db: Session,
    is_short_episode: bool = False,
    short_order: int | None = None,
    short_total_parts: int | None = None,
) -> bool:
    """Render main video or short from props files already on disk.

    Raises:
        VerifyFailedError: Post-render verification failed.
        RemotionCrashError: Chromium crashed.
        RemotionRenderError: Remotion exited non-zero.
    """
    if is_short_episode:
        short_props_path = str(props_dir / f"{cid_str}_{language}_short_{short_order}.json")
        if not _render_exists(content_id, language, "short", short_order, db):
            result = _run_short_render(
                content_id=content_id,
                language=language,
                cid_str=cid_str,
                audio=audio,
                short_order=short_order or 0,
                props_path=short_props_path,
                db=db,
            )
            logger.info(
                "CHILD_SHORT_RENDER_DONE path=%s duration_ms=%d (existing-props path)",
                result["file_path"], audio.duration_ms,
            )
        else:
            logger.info("Short render already done for language=%s — skipping", language)
        logger.info("Render from existing props complete for language=%s (short)", language)
        return True

    # ── Main video path ────────────────────────────────────────────────────────
    main_props_path = str(props_dir / f"{cid_str}_{language}_main.json")
    bundle_dir = ensure_bundle()

    if not _render_exists(content_id, language, "main", None, db):
        main_result = render_main_video(
            content_id=cid_str,
            language=language,
            props_path=main_props_path,
            duration_ms=audio.duration_ms,
            bundle_dir=bundle_dir,
        )
        if settings.verify_renders:
            issues = verify_render(
                mp4_path=main_result["file_path"],
                expected_duration_ms=audio.duration_ms,
                fmt="main",
            )
            if issues:
                raise VerifyFailedError(
                    f"Main render (existing-props) verification failed for language={language}: {issues}"
                )
        db.add(VideoRender(
            content_id=content_id,
            language=language,
            format="main",
            short_order=None,
            duration_seconds=main_result["duration_seconds"],
                render_time_seconds=main_result["render_time_seconds"],
        ))
        db.commit()
    else:
        logger.info("Main render already done for language=%s — skipping", language)

    logger.info("Render from existing props complete for language=%s", language)
    return True


def _render_exists(
    content_id: uuid.UUID,
    language: str,
    fmt: str,
    short_order: int | None,
    db: Session,
) -> bool:
    """Check if a VideoRender row already exists for this combination."""
    q = db.query(VideoRender).filter(
        VideoRender.content_id == content_id,
        VideoRender.language   == language,
        VideoRender.format     == fmt,
    )
    if short_order is not None:
        q = q.filter(VideoRender.short_order == short_order)
    return q.first() is not None


# ── Render execution ───────────────────────────────────────────────────────────

def _run_renders(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    audio: AudioFile,
    main_props_path: str,
    db: Session,
    concurrency: int | None = None,
) -> None:
    """Render the main video, verifying it before saving a VideoRender row.

    Standalone short architecture only: short renders are never triggered from here.
    Shorts are handled by standalone Standalone short architecture short episode Content rows.

    Raises:
        RemotionCrashError:  Chromium crashed.
        RemotionRenderError: Remotion exited non-zero.
        VerifyFailedError:   Render passed but failed post-render verification.
    """
    bundle_dir = ensure_bundle()

    use_chunked = (
        settings.chunked_render_enabled
        and audio.duration_ms > settings.chunk_duration_sec * 1000
    )
    if use_chunked:
        main_result = render_main_video_chunked(
            content_id=cid_str,
            language=language,
            props_path=main_props_path,
            duration_ms=audio.duration_ms,
            audio_file_path=audio.file_path,
            concurrency=concurrency,
            bundle_dir=bundle_dir,
        )
    else:
        main_result = render_main_video(
            content_id=cid_str,
            language=language,
            props_path=main_props_path,
            duration_ms=audio.duration_ms,
            concurrency=concurrency,
            bundle_dir=bundle_dir,
        )

    if settings.verify_renders:
        main_issues = verify_render(
            mp4_path=main_result["file_path"],
            expected_duration_ms=audio.duration_ms,
            fmt="main",
        )
        if main_issues:
            raise VerifyFailedError(
                f"Main render verification failed for language={language}: {main_issues}"
            )

    db.add(VideoRender(
        content_id=content_id,
        language=language,
        format="main",
        short_order=None,
        duration_seconds=main_result["duration_seconds"],
        render_time_seconds=main_result["render_time_seconds"],
    ))
    db.commit()

    logger.info(
        "language=%s done: 1 main + 0 parent-cut shorts (standalone shorts only) for content %s render_mode=%s",
        language, content_id,
        "chunked" if use_chunked else "single",
    )


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _save_video_sections(
    content_id: uuid.UUID,
    language: str,
    sections: list[dict],
    db: Session,
) -> None:
    """Persist beat dicts to video_sections (delete-then-insert).

    Beat fields not in the schema proper (visual_intent, visual_type, environment,
    motif, transition_to_next, overlay_text, overlay_position, media_url) are
    JSON-serialized into ``generation_prompt`` for re-entrant loading.
    """
    db.query(VideoSection).filter(
        VideoSection.content_id == content_id,
        VideoSection.language   == language,
    ).delete()

    for s in sections:
        db.add(VideoSection(
            content_id=content_id,
            language=language,
            section_order=s["section_order"],
            script_text=s.get("script_text", ""),
            audio_start_ms=s.get("audio_start_ms", 0),
            audio_end_ms=s.get("audio_end_ms", 0),
            flux_prompt=s.get("flux_prompt", ""),
            effect=s.get("effect"),
            color_grade=s.get("color_grade"),
            generation_prompt=json.dumps(_beat_extras(s), ensure_ascii=False),
            beat_intensity=s.get("beat_intensity"),
            suggested_duration_sec=s.get("suggested_duration_sec"),
            media_strategy=s.get("media_strategy"),
            text_card_style=s.get("text_card_style"),
        ))

    db.flush()
    logger.info(
        "Saved %d section(s) for language=%s, content=%s",
        len(sections), language, content_id,
    )


def _load_sections_from_db(
    content_id: uuid.UUID, language: str, db: Session
) -> list[dict]:
    """Load VideoSection rows as dicts compatible with the render pipeline."""
    rows = (
        db.query(VideoSection)
        .filter(
            VideoSection.content_id == content_id,
            VideoSection.language   == language,
        )
        .order_by(VideoSection.section_order)
        .all()
    )
    result = []
    for s in rows:
        section: dict = {
            "section_order":        s.section_order,
            "beat_order":           s.section_order,
            "script_text":          s.script_text,
            "audio_start_ms":       s.audio_start_ms,
            "audio_end_ms":         s.audio_end_ms,
            "duration_sec":         (s.audio_end_ms - s.audio_start_ms) / 1000,
            "flux_prompt":          s.flux_prompt or "",
            "effect":               s.effect or "slow_zoom",
            "color_grade":          s.color_grade or "desaturated",
            "beat_intensity":       s.beat_intensity or "medium",
            "suggested_duration_sec": s.suggested_duration_sec,
            "media_strategy":       s.media_strategy or "flux_generated",
            "text_card_style":      s.text_card_style or "default",
        }
        if s.generation_prompt:
            try:
                extras = json.loads(s.generation_prompt)
            except (json.JSONDecodeError, TypeError):
                extras = {}
            if isinstance(extras, dict):
                section.update(extras)
        result.append(section)
    return result


def _beat_extras(s: dict) -> dict:
    """Collect the fields stored in generation_prompt JSON for re-entrant loading."""
    return {
        "visual_intent":      s.get("visual_intent", ""),
        "visual_type":        s.get("visual_type", "b-roll"),
        "visual_category":    s.get("visual_category", "place"),
        "environment":        s.get("environment", "other"),
        "motif":              s.get("motif", "other"),
        "transition_to_next": s.get("transition_to_next", "cut"),
        "overlay_text":       s.get("overlay_text", ""),
        "overlay_position":   s.get("overlay_position", "none"),
        # Local Flux image path — the canonical media_url for re-entrant runs
        "media_url":          s.get("media_url", ""),
        "media_type":         s.get("media_type", "image"),
        "media_strategy":     s.get("media_strategy", "flux_generated"),
        "text_card_style":    s.get("text_card_style", "default"),
    }


def _props_contain_uhd_url(props_file: Path) -> bool:
    """Return True if any URL in the props JSON exceeds FHD resolution."""
    try:
        raw = props_file.read_text()
        uhd_markers = ("_4096_", "_2160_", "_3840_", "_uhd_", "_4k_", "2160p", "4096p")
        return any(m in raw for m in uhd_markers)
    except Exception:
        return False
