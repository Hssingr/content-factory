"""Agent 4 — visual generation orchestrator.

Agent 4 is the sole producer of visual-ready content. Its task-level
entrypoint is ``run_visual_generation_for_content()`` (called from the
``run_agent4_visual_generation_for_content`` Celery task) — it loads its own
preconditions (`Content`, `Channel`, `ChannelConfig`, validated `Script` rows,
`AudioFile` rows), runs ``run_visual_generation()``, and persists
`VideoSection` rows. Agent 4 is the sole writer of `Content.status` values
``GENERATING_VISUALS``, ``PARENT_VISUALS_DONE``, and
``CHILD_SHORT_VISUALS_DONE`` — those are the status-based readiness signal
Agent 5 polls on (see ``app.scheduler.tasks.pickup_visual_ready``). Agent 5
never calls into this module; `VideoSection` row existence is a defensive
validation check on Agent 5's side, not the primary discovery signal (see
CLAUDE.md "6A. Service Ownership Boundaries").

Owns:
  Parent — storyboard generation, storyboard validation, Flux prompt
    generation, Flux image generation/cache reuse, and
    `VideoSection(language="__visual__")` + per-language `VideoSection`
    persistence.
  Child short — parent visual readiness gating, narration remap to parent
    beats, media reuse/generation, and per-language `VideoSection`
    persistence.

Render preparation (subtitles, Remotion props, rendering, verification,
`VideoRender` persistence) is not part of this module and stays in
`app/agents/agent5_render/`.
"""

import hashlib
import json
import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.models import AudioFile, Channel, ChannelConfig, Content, Script, VideoSection
from app.agents.agent4_visuals.subagents.storyboard import (
    split_into_beats, remap_beats_for_short, generate_pending_beat_images,
    remediate_child_major_storyboard_issues,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    validate_storyboard, repair_duplicate_flux_prompts,
)
from app.agents.agent4_visuals.services.flux_generator import generate_all_beat_images
from app.agents.agent4_visuals.services.cinematic_prompts import apply_cinematic_prompts_to_beats
from app.agents.agent4_visuals.services.first15_validator import apply_first15_enhancement_and_validation
from app.agents.agent4_visuals.services.media_validation import validate_visual_media_assets
from app.agents.agent4_visuals.services.visual_bible import generate_visual_bible_for_content, load_visual_bible_for_content
from app.agents.agent4_visuals.services.visual_review import (
    generate_visual_review_html, save_beat_review_metadata,
)
from app.services.local_run_paths import ensure_run_dirs
from app.services.video_sections import load_video_sections
from app.agents.agent4_visuals.system_prompt import (
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

_FINAL_PROMPT_VALIDATION_CHECKS: frozenset[str] = frozenset({
    "forbidden_flux_word",
    "subject_presence",
    "environment_presence",
    "low_information_prompt",
    "flux_prompt_exact_duplicate",
    "flux_prompt_near_duplicate",
    "ai_text_rendering_requested",
})

# Language sentinel used to store the shared visual-pass beats (generated once,
# shared by all language renders). Must match the migration's widened varchar(16).
_VISUAL_LANGUAGE = "__visual__"


def run_visual_generation(
    content: Content,
    channel: Channel,
    scripts_by_lang: dict[str, Script],
    audio_by_lang: dict[str, AudioFile],
    script_format: str,
    allow_legacy_fallback: bool,
    db: Session,
    visual_style: str = "",
    image_style: str = "",
) -> dict:
    """Ensure visual readiness for one content item (parent or child short).

    Generates/loads storyboard beats (parent) or remaps parent beats to child
    narration (child short), persists the resulting `VideoSection` rows per
    language, and returns them so the caller can render without an extra DB
    round trip.

    Returns:
        dict with keys:
          ``status``: one of ``"PARENT_VISUALS_DONE"``,
            ``"CHILD_SHORT_VISUALS_DONE"``, ``"CHILD_SHORT_VISUALS_DEFERRED"``,
            ``"VISUALS_FAILED"``.
          ``beats_by_lang``: ``{language: [beat dict, ...]}`` — already
            persisted to `VideoSection`. Callers must treat these as read-only.
    """
    content_id = content.id
    is_short_episode = bool(getattr(content, "is_short_episode", False))

    if is_short_episode:
        return _run_child_short_visuals(content, scripts_by_lang, audio_by_lang, db)

    return _run_parent_visuals(
        content_id=content_id,
        content=content,
        scripts_by_lang=scripts_by_lang,
        audio_by_lang=audio_by_lang,
        channel=channel,
        script_format=script_format,
        allow_legacy_fallback=allow_legacy_fallback,
        db=db,
        visual_style=visual_style,
        image_style=image_style,
    )


def run_visual_generation_for_content(content_id: uuid.UUID, db: Session) -> bool:
    """Agent 4 task entrypoint — ensure visual readiness for one content item.

    Called from the ``run_agent4_visual_generation_for_content`` Celery task.
    Loads its own preconditions (`Content`, `Channel`, `ChannelConfig`,
    validated `Script` rows, `AudioFile` rows), transitions
    ``AUDIO_DONE`` -> ``GENERATING_VISUALS``, and runs `run_visual_generation()`.

    Agent 5 does not call this function or any other symbol in this module —
    it discovers readiness independently from `Content.status`
    (`app.scheduler.tasks.pickup_visual_ready`), which this function is the
    sole writer of (`PARENT_VISUALS_DONE` / `CHILD_SHORT_VISUALS_DONE`).

    Returns:
        ``True``  — visuals are ready for at least one language (parent or
                     child); `Content.status` is `PARENT_VISUALS_DONE` or
                     `CHILD_SHORT_VISUALS_DONE` and `VideoSection` rows are
                     already persisted.
        ``False`` — deferred (child waiting on parent visuals) or failed.
    """
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.error("Content %s not found", content_id)
        return False

    ensure_run_dirs(content_id)

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content_id)
        return False

    if content.status not in ("AUDIO_DONE", "GENERATING_VISUALS"):
        logger.debug(
            "Content %s status=%s — skipping visual generation",
            content_id, content.status,
        )
        return False

    if content.status == "AUDIO_DONE":
        content.status = "GENERATING_VISUALS"
        db.commit()
        logger.info("AGENT4_VISUALS_START content_id=%s", content_id)

    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    script_format         = config.script_format         if config else "youtube_long"
    allow_legacy_fallback = config.allow_legacy_fallback if config else False
    visual_style          = (config.visual_style  or "") if config else ""
    image_style           = (config.image_style   or "") if config else ""

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

    try:
        visual_bible = generate_visual_bible_for_content(content_id, db)
        logger.info(
            "AGENT4_VISUAL_BIBLE_READY content_id=%s characters=%d locations=%d motifs=%d",
            content_id,
            len(visual_bible.get("characters") or []),
            len(visual_bible.get("locations") or []),
            len(visual_bible.get("recurring_motifs") or []),
        )
    except Exception as exc:
        logger.error("AGENT4_VISUAL_BIBLE_FAILED content_id=%s error=%s", content_id, exc)
        content.status = "FAILED"
        db.commit()
        return False

    result = run_visual_generation(
        content=content,
        channel=channel,
        scripts_by_lang=scripts_by_lang,
        audio_by_lang=audio_by_lang,
        script_format=script_format,
        allow_legacy_fallback=allow_legacy_fallback,
        db=db,
        visual_style=visual_style,
        image_style=image_style,
    )

    status = result["status"]
    if status == "CHILD_SHORT_VISUALS_DEFERRED":
        content.status = "AUDIO_DONE"
        db.commit()
        return False
    if status == "VISUALS_FAILED":
        content.status = "FAILED"
        db.commit()
        return False

    validation = validate_visual_media_assets(
        content_id, db, beats_by_lang=result["beats_by_lang"],
    )
    for warning in validation.warnings:
        logger.warning(
            "AGENT4_MEDIA_VALIDATION_WARNING content_id=%s language=%s section=%s code=%s media=%s message=%s",
            content_id, warning.language, warning.section_order, warning.code,
            warning.media_path, warning.message,
        )
    if not validation.passed:
        logger.error(
            "AGENT4_MEDIA_VALIDATION_BLOCKED content_id=%s blocking=%d checked=%d codes=%s",
            content_id,
            len(validation.blocking_issues),
            validation.checked_count,
            [issue.code for issue in validation.blocking_issues],
        )
        for issue in validation.blocking_issues:
            logger.error(
                "AGENT4_MEDIA_VALIDATION_BLOCKING_ISSUE content_id=%s language=%s section=%s code=%s media=%s message=%s",
                content_id, issue.language, issue.section_order, issue.code,
                issue.media_path, issue.message,
            )
        content.status = "FAILED"
        db.commit()
        return False

    # status is "PARENT_VISUALS_DONE" or "CHILD_SHORT_VISUALS_DONE" — Agent 4
    # is the sole writer of these statuses; pickup_visual_ready reads them.
    content.status = status
    db.commit()

    try:
        review_path = generate_visual_review_html(content_id, db)
        logger.info("AGENT4_VISUAL_REVIEW_HTML content_id=%s path=%s", content_id, review_path)
    except Exception as exc:
        logger.warning(
            "AGENT4_VISUAL_REVIEW_HTML_FAILED content_id=%s error=%s",
            content_id, exc,
        )

    logger.info(
        "AGENT4_VISUAL_GENERATION_DONE content_id=%s status=%s languages=%d",
        content_id, status, len(result["beats_by_lang"]),
    )
    return True


# ── Parent visual readiness ────────────────────────────────────────────────────

def _run_parent_visuals(
    content_id: uuid.UUID,
    content: Content,
    scripts_by_lang: dict[str, Script],
    audio_by_lang: dict[str, AudioFile],
    channel: Channel,
    script_format: str,
    allow_legacy_fallback: bool,
    db: Session,
    visual_style: str = "",
    image_style: str = "",
) -> dict:
    shared_beats = _load_shared_beats(content_id, db)
    current_script_hash = _source_script_hash(content, scripts_by_lang)

    # Stale-visuals guard (audit V-6b): a script regenerated after the visual
    # pass already ran (operator --force-scripts, or any retry that changes
    # the source narration) must never let the OLD beats be silently reused.
    staleness = (
        _check_shared_beats_staleness(content_id, shared_beats, current_script_hash)
        if shared_beats else "fresh"
    )
    if staleness == "stale":
        shared_beats = []
    elif staleness == "backfill":
        logger.info(
            "STALE_VISUALS_CHECK_BACKFILL content_id=%s beats=%d — no baseline "
            "script hash stored on these beats yet; stamping current hash "
            "without forcing a regeneration",
            content_id, len(shared_beats),
        )
        _tag_beats_with_script_hash(shared_beats, current_script_hash)
        _save_shared_beats(content_id, shared_beats, db)
        db.commit()

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
            visual_style=visual_style,
            image_style=image_style,
            script_hash=current_script_hash,
        )
        if shared_beats is None:
            return {"status": "VISUALS_FAILED", "beats_by_lang": {}}
    else:
        source_duration_ms = max(
            (b.get("audio_end_ms", 0) for b in shared_beats), default=0
        )
        if flux_incomplete:
            logger.info(
                "Visual pass: %d beats loaded, Flux incomplete — re-running Flux only (content=%s)",
                len(shared_beats), content_id,
            )
            shared_beats = _repair_duplicate_prompts(shared_beats, content_id=content_id)
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

    beats_by_lang: dict[str, list[dict]] = {}
    for language, _script in scripts_by_lang.items():
        audio = audio_by_lang.get(language)
        if not audio:
            continue
        beats_for_lang = _remap_beats_timing(
            shared_beats, audio.duration_ms, source_duration_ms
        )
        _save_video_sections(content_id, language, beats_for_lang, db)
        db.commit()
        save_beat_review_metadata(content_id, language, beats_for_lang)
        beats_by_lang[language] = beats_for_lang

    return {"status": "PARENT_VISUALS_DONE", "beats_by_lang": beats_by_lang}


def _run_visual_pass(
    content_id: uuid.UUID,
    scripts_by_lang: dict[str, Script],
    audio_by_lang: dict[str, AudioFile],
    channel: Channel,
    script_format: str,
    allow_legacy_fallback: bool,
    db: Session,
    visual_style: str = "",
    image_style: str = "",
    script_hash: str | None = None,
) -> tuple[list[dict] | None, int]:
    """Generate storyboard + Flux images once for this content item.

    Uses the source language script/audio for storyboard generation (so hints
    are in the same language as the Whisper transcript). All language renders
    share the resulting beat images; timing is re-scaled per language.

    Args:
        script_hash: When provided (the source-language script's SHA-256,
            from `_source_script_hash()`), stamped onto every beat before the
            first save so a later run can detect script staleness (audit
            V-6b) — see `_check_shared_beats_staleness()`.

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
        "Agent4 [VISUAL_PASS] content=%s source_lang=%s "
        "source_duration_ms=%d schema_version=%s",
        content_id, source_lang, source_duration_ms, _STORYBOARD_SCHEMA_VERSION,
    )

    visual_bible = load_visual_bible_for_content(content_id)

    # ── 1. Storyboard ─────────────────────────────────────────────────────────
    beats = split_into_beats(
        voice_script=source_script.voice_script,
        duration_ms=source_audio.duration_ms,
        channel=channel,
        script_format=script_format,
        whisper_transcript=source_audio.whisper_transcript or [],
        allow_legacy_fallback=allow_legacy_fallback,
        language=source_lang,
        visual_style=visual_style,
        image_style=image_style,
        visual_bible_context=visual_bible,
        db=db,
    )

    if beats is None:
        # Roadmap 6.3 / audit §7: the legacy section-splitter fallback
        # (section_splitter.py) was reachable only via ChannelConfig
        # .allow_legacy_fallback, which defaults False and the UI never sets
        # — fail loud unconditionally, which was already the only real
        # production behavior.
        logger.error(
            "Agent4 [FAIL] content=%s status=STORYBOARD_FAILED "
            "reason=storyboard_generation_failed",
            content_id,
        )
        return None, 0

    beats = _cleanup_micro_beats(beats, script_format)
    logger.info(
        "Agent4 [STORYBOARD] content=%s beats=%d source_lang=%s",
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
        visual_style=visual_style,
        image_style=image_style,
        db=db,
        visual_bible=visual_bible,
    )
    if beats is None:
        logger.error(
            "Agent4 [FAIL] content=%s status=STORYBOARD_VALIDATION_FAILED "
            "reason=storyboard_validation_gate_returned_None (allow_legacy_fallback=False)",
            content_id,
        )
        return None, 0

    beats = apply_cinematic_prompts_to_beats(
        beats,
        visual_bible=visual_bible,
        content_kind="parent_long_form",
    )
    final_prompt_issues = _check_final_prompt_issues(
        beats,
        content_id=content_id,
        stage="parent_after_enrichment",
        language=source_lang,
    )
    if any(issue["severity"] == "MAJOR" for issue in final_prompt_issues):
        logger.error(
            "Agent4 [FAIL] content=%s status=FINAL_PROMPT_VALIDATION_FAILED "
            "stage=parent_after_enrichment",
            content_id,
        )
        return None, 0

    beats, first15_result = apply_first15_enhancement_and_validation(
        beats,
        visual_bible=visual_bible,
        content_kind="parent",
    )
    if first15_result.status == "FAIL_BLOCKING":
        logger.error(
            "Agent4 [FAIL] content=%s status=FIRST15_VISUAL_HOOK_FAILED "
            "checked=%d strong=%d generic=%d issues=%s",
            content_id,
            first15_result.checked_count,
            first15_result.strong_hook_count,
            first15_result.weak_generic_count,
            [issue.code for issue in first15_result.issues],
        )
        return None, 0
    logger.info(
        "Agent4 [FIRST15] content=%s status=%s checked=%d strong=%d warnings=%d",
        content_id,
        first15_result.status,
        first15_result.checked_count,
        first15_result.strong_hook_count,
        sum(1 for issue in first15_result.issues if issue.severity == "WARNING"),
    )
    final_prompt_issues = _check_final_prompt_issues(
        beats,
        content_id=content_id,
        stage="parent_after_first15",
        language=source_lang,
    )
    if any(issue["severity"] == "MAJOR" for issue in final_prompt_issues):
        logger.error(
            "Agent4 [FAIL] content=%s status=FINAL_PROMPT_VALIDATION_FAILED "
            "stage=parent_after_first15",
            content_id,
        )
        return None, 0

    # ── 1c. Duplicate-prompt repair (audit G-5.3) — last mutation of
    # flux_prompt before Flux; must run after enrichment/first15, both done. ──
    beats = _repair_duplicate_prompts(beats, content_id=content_id, language=source_lang)

    # Stale-visuals guard (audit V-6b): stamp the source-script fingerprint
    # onto every beat before the FIRST save, so it survives (in-place
    # mutation) through the post-Flux save below too.
    if script_hash:
        _tag_beats_with_script_hash(beats, script_hash)

    # ── 2. Save storyboard beats before Flux — protects storyboard work ─────────
    # If Flux crashes mid-run, --from-video can reload these beats and skip straight
    # to Flux retry (file cache handles already-generated images).
    _save_shared_beats(content_id, beats, db)
    db.commit()

    # ── 3. Flux generation ────────────────────────────────────────────────────
    beats = generate_all_beat_images(beats, cid_str)

    succeeded = sum(1 for b in beats if (b.get("media_url") or "").startswith("cache/"))
    missing_media = len(beats) - succeeded
    logger.info(
        "Agent4 [FLUX_DONE] content=%s beats=%d flux_ok=%d missing_media=%d",
        content_id, len(beats), succeeded, missing_media,
    )

    # ── 4. Update saved beats with Flux media_url ─────────────────────────────
    _save_shared_beats(content_id, beats, db)
    db.commit()
    logger.info("PARENT_VISUALS_DONE content_id=%s beats=%d", content_id, len(beats))

    return beats, source_duration_ms


def _check_final_prompt_issues(
    beats: list[dict],
    *,
    content_id: uuid.UUID,
    stage: str,
    language: str = "",
) -> list[dict]:
    """Validate the exact flux_prompt values that will be sent to Flux.

    Phase 2.2 reruns the prompt-only validator subset after visual-bible
    enrichment, before first15 and before any fal.ai call. This catches any
    additive continuity clause that reintroduces forbidden mood words,
    readable-text requests, low-information prompts, or duplicate prompts.
    """
    issues = [
        issue for issue in validate_storyboard(beats)
        if issue["check"] in _FINAL_PROMPT_VALIDATION_CHECKS
    ]
    if not issues:
        logger.info(
            "FINAL_PROMPT_VALIDATION_OK content=%s stage=%s language=%s beats=%d checks=%s",
            content_id, stage, language or "__visual__", len(beats),
            sorted(_FINAL_PROMPT_VALIDATION_CHECKS),
        )
        return []

    majors = [issue for issue in issues if issue["severity"] == "MAJOR"]
    minors = [issue for issue in issues if issue["severity"] == "MINOR"]
    for issue in minors:
        logger.warning(
            "FINAL_PROMPT_VALIDATION_MINOR content=%s stage=%s language=%s beat_order=%s check=%s description=%s",
            content_id, stage, language or "__visual__", issue["beat_order"],
            issue["check"], issue["description"],
        )
    for issue in majors:
        logger.error(
            "FINAL_PROMPT_VALIDATION_MAJOR content=%s stage=%s language=%s beat_order=%s check=%s description=%s",
            content_id, stage, language or "__visual__", issue["beat_order"],
            issue["check"], issue["description"],
        )
    logger.warning(
        "FINAL_PROMPT_VALIDATION_DONE content=%s stage=%s language=%s issues=%d majors=%d minors=%d",
        content_id, stage, language or "__visual__", len(issues), len(majors), len(minors),
    )
    return issues


def _repair_duplicate_prompts(
    beats: list[dict],
    *,
    content_id: uuid.UUID,
    language: str = "",
) -> list[dict]:
    """Deterministically vary surviving duplicate-prompt beats (audit G-5.3).

    The single call site for `repair_duplicate_flux_prompts()` — shared by
    the parent path (both fresh generation and flux-incomplete recovery) and
    the child remap path. Must run AFTER every prompt-shaping step (visual
    bible enrichment, first15 enhancement) and BEFORE any Flux call, so the
    repaired text is what actually reaches fal.ai — this is the last mutation
    of `flux_prompt` before generation.
    """
    repaired, repairs = repair_duplicate_flux_prompts(beats)
    if not repairs:
        logger.info(
            "DUPLICATE_PROMPT_REPAIR_OK content=%s language=%s beats=%d",
            content_id, language or "__visual__", len(beats),
        )
        return repaired

    for repair in repairs:
        logger.warning(
            "DUPLICATE_PROMPT_REPAIRED content=%s language=%s beat_order=%s "
            "check=%s variation=%r",
            content_id, language or "__visual__", repair["beat_order"],
            repair["check"], repair["variation"],
        )
    logger.info(
        "DUPLICATE_PROMPT_REPAIR_DONE content=%s language=%s beats=%d repaired=%d",
        content_id, language or "__visual__", len(beats), len(repairs),
    )
    return repaired


_SEGMENT_RETRY_SUPPORT_CHECKS: frozenset[str] = frozenset({
    "consecutive_same_environment",
    "ai_slideshow_risk",
    "document_saturation",
})


def _collect_storyboard_issues(beats: list[dict]) -> list[dict]:
    """Run validate_storyboard() once and log MINOR findings."""
    issues = validate_storyboard(beats)
    for issue in [i for i in issues if i["severity"] == "MINOR"]:
        logger.warning(
            "Storyboard MINOR: beat=%d check=%s — %s",
            issue["beat_order"], issue["check"], issue["description"][:200],
        )
    return issues


def _check_storyboard_issues(beats: list[dict]) -> list[dict]:
    """Run validate_storyboard() and log MINOR findings; return the MAJOR ones.

    This is the single call site for ``validate_storyboard()`` shared by both
    the parent storyboard path and the child remap path — neither caller
    forks or re-implements the validator itself, only what happens after a
    MAJOR finding differs (parent can retry via segment-level storyboard
    re-generation; child remap has no equivalent regeneration primitive and
    logs/proceeds immediately, the same terminal behavior the parent falls
    back to when its own retry still leaves MAJOR issues).

    Returns:
        The MAJOR issues found (empty list if the storyboard is clean).
    """
    return [i for i in _collect_storyboard_issues(beats) if i["severity"] == "MAJOR"]


def _storyboard_batch_labels(beats: list[dict]) -> list[str]:
    return sorted({
        str(beat.get("storyboard_batch_label") or "").strip()
        for beat in beats
        if str(beat.get("storyboard_batch_label") or "").strip()
    })


def _documentish_batch_labels(beats: list[dict]) -> set[str]:
    labels: set[str] = set()
    for beat in beats:
        if (
            str(beat.get("motif") or "").lower() == "document"
            or str(beat.get("visual_type") or "").lower() == "document"
        ):
            label = str(beat.get("storyboard_batch_label") or "").strip()
            if label:
                labels.add(label)
    return labels


def _issue_batch_labels(issue: dict, beats_by_order: dict[int, dict], beats: list[dict]) -> set[str]:
    order = int(issue.get("beat_order", -1))
    if order >= 0:
        label = str(beats_by_order.get(order, {}).get("storyboard_batch_label") or "").strip()
        return {label} if label else set()
    if issue.get("check") == "document_saturation":
        return _documentish_batch_labels(beats)
    return set()


def _storyboard_retry_constraints_by_batch(beats: list[dict], issues: list[dict]) -> dict[str, str]:
    """Build per-batch retry constraints from MAJOR validation findings."""
    major_issues = [issue for issue in issues if issue["severity"] == "MAJOR"]
    if not major_issues:
        return {}

    beats_by_order = {int(beat.get("beat_order", -1)): beat for beat in beats}
    all_labels = set(_storyboard_batch_labels(beats))
    by_label: dict[str, list[dict]] = {}

    for issue in major_issues:
        labels = _issue_batch_labels(issue, beats_by_order, beats)
        if issue.get("check") == "visual_monotony":
            labels = set()
            for support in issues:
                if support.get("check") not in _SEGMENT_RETRY_SUPPORT_CHECKS:
                    continue
                labels.update(_issue_batch_labels(support, beats_by_order, beats))
            labels = labels or all_labels
        if not labels:
            labels = all_labels
        for label in labels:
            by_label.setdefault(label, []).append(issue)

    if any(issue.get("check") == "visual_monotony" for issue in major_issues):
        for support in issues:
            if support.get("check") not in _SEGMENT_RETRY_SUPPORT_CHECKS:
                continue
            for label in _issue_batch_labels(support, beats_by_order, beats):
                if label in by_label:
                    by_label[label].append(support)

    constraints: dict[str, str] = {}
    for label, label_issues in by_label.items():
        seen: set[tuple[str, int, str]] = set()
        lines = [f"Segment-level storyboard retry constraints for {label}:"]
        for issue in label_issues:
            key = (issue["check"], int(issue.get("beat_order", -1)), issue["description"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"- [{issue['check']}] beat_order={issue['beat_order']}: {issue['description']}"
            )
        constraints[label] = "\n".join(lines)
    return constraints


    return issues


def _run_storyboard_validation(
    beats: list[dict],
    voice_script: str,
    source_audio: "AudioFile",
    channel: Channel,
    script_format: str,
    allow_legacy_fallback: bool,
    source_lang: str,
    visual_style: str = "",
    image_style: str = "",
    visual_bible: dict | None = None,
    db: Session | None = None,
) -> list[dict] | None:
    """Run the storyboard validation gate; retry once on MAJOR issues.

    MAJOR issues trigger one segment-level storyboard retry. Only batches whose
    provenance intersects the MAJOR issue, or the supporting findings behind an
    aggregate MAJOR such as ``visual_monotony``, are regenerated; unaffected
    batches are reused and the merged storyboard is remapped once. If still
    MAJOR after retry: log ERROR and proceed — the pipeline is never blocked.
    MINOR issues are logged at WARNING only.

    Returns the (possibly partially re-generated) beat list, or None on
    catastrophic validation failure (only when allow_legacy_fallback=False and
    storyboard retry also fails to produce any beats).
    """
    issues = _collect_storyboard_issues(beats)
    major_issues = [issue for issue in issues if issue["severity"] == "MAJOR"]

    if not major_issues:
        return beats

    retry_constraints = _storyboard_retry_constraints_by_batch(beats, issues)
    constraint_lines = "\n".join(
        f"- [{iss['check']}] beat_order={iss['beat_order']}: {iss['description']}"
        for iss in major_issues
    )

    if retry_constraints:
        logger.warning(
            "Segment-level storyboard retry triggered due to %d MAJOR issue(s) — "
            "retrying %d batch(es): %s checks=%s",
            len(major_issues), len(retry_constraints), sorted(retry_constraints),
            [i["check"] for i in major_issues],
        )
        retry_kwargs = {
            "retry_segment_constraints": retry_constraints,
            "existing_beats": beats,
            "storyboard_constraints": "",
        }
    else:
        n_segments = max(1, len(re.findall(
            r"^\s*\[(?:INTRO|OUTRO|SECTION[^\]]*)\]", voice_script,
            re.IGNORECASE | re.MULTILINE,
        )))
        logger.warning(
            "STORYBOARD_SEGMENT_RETRY_FALLBACK reason=no_batch_provenance "
            "major_count=%d — re-running all %d segment(s) checks=%s",
            len(major_issues), n_segments, [i["check"] for i in major_issues],
        )
        retry_kwargs = {
            "storyboard_constraints": constraint_lines,
        }

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
        visual_style=visual_style,
        image_style=image_style,
        visual_bible_context=visual_bible,
        db=db,
        **retry_kwargs,
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


def _load_shared_beats(content_id: uuid.UUID, db: Session) -> list[dict]:
    """Load the shared visual-pass beats stored under language='__visual__'."""
    return load_video_sections(content_id, _VISUAL_LANGUAGE, db)


def _save_shared_beats(content_id: uuid.UUID, beats: list[dict], db: Session) -> None:
    """Persist visual-pass beats under language='__visual__'."""
    _save_video_sections(content_id, _VISUAL_LANGUAGE, beats, db)
    save_beat_review_metadata(content_id, _VISUAL_LANGUAGE, beats)


# ── Stale-visuals guard (audit V-6b) ──────────────────────────────────────────
# The shared `__visual__` beats are keyed only by content_id — nothing ties
# them to the source-language narration they were built from. If the parent's
# source script is regenerated after the visual pass already ran (operator
# `--force-scripts`, or a retry that produces a materially different script),
# `_run_parent_visuals()` would otherwise reuse the OLD beats against NEW
# narration silently: hints, timings, and flux_prompt subject matter would
# all describe a script that no longer exists. `source_script_sha256` is
# stamped onto every `__visual__` beat's extras (same per-beat storage
# convention as every other shared field in `_beat_extras()` — see AR-3) so
# the next run can detect the mismatch before reusing anything.


def _source_script_hash(content: Content, scripts_by_lang: dict[str, Script]) -> str | None:
    """SHA-256 hex digest of the source-language voice_script — the staleness
    fingerprint for the shared `__visual__` beats. Returns ``None`` when the
    source-language script is unavailable (never treated as a mismatch;
    the caller must fail open, not force an unwarranted regeneration)."""
    source_script = scripts_by_lang.get(content.source_language)
    if not source_script or not source_script.voice_script:
        return None
    return hashlib.sha256(source_script.voice_script.encode("utf-8")).hexdigest()


def _tag_beats_with_script_hash(beats: list[dict], script_hash: str) -> None:
    """Stamp `script_hash` onto every beat in place — called once, before the
    first `_save_shared_beats()` of a (re)generation pass, so it survives
    through to every later save of the same (mutated-in-place) beat list."""
    for beat in beats:
        beat["source_script_sha256"] = script_hash


def _check_shared_beats_staleness(
    content_id: uuid.UUID,
    shared_beats: list[dict],
    current_hash: str | None,
) -> str:
    """Classify loaded `__visual__` beats against the current source script.

    Returns one of:
      ``"fresh"``    — stored hash matches (or nothing is comparable); reuse as-is.
      ``"stale"``    — stored hash present and differs; caller must discard
                       `shared_beats` and regenerate from scratch.
      ``"backfill"`` — beats predate this guard (no stored hash) and a
                       current hash is available; caller should stamp and
                       re-save `shared_beats` unchanged so future runs have a
                       baseline, without forcing a regeneration today.
    """
    if current_hash is None:
        logger.debug(
            "STALE_VISUALS_CHECK_SKIPPED content_id=%s reason=source_script_unavailable",
            content_id,
        )
        return "fresh"

    stored_hash = shared_beats[0].get("source_script_sha256") or ""
    if not stored_hash:
        return "backfill"

    if stored_hash == current_hash:
        return "fresh"

    logger.warning(
        "PARENT_VISUALS_STALE_SCRIPT_HASH content_id=%s stored=%s current=%s "
        "beats=%d — source script changed since these beats were generated; "
        "discarding and regenerating the visual pass",
        content_id, stored_hash[:12], current_hash[:12], len(shared_beats),
    )
    return "stale"


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


# ── Child short visual readiness ───────────────────────────────────────────────

def _run_child_short_visuals(
    content: Content,
    scripts_by_lang: dict[str, Script],
    audio_by_lang: dict[str, AudioFile],
    db: Session,
) -> dict:
    content_id = content.id
    parent_content_id = getattr(content, "parent_content_id", None)

    if not parent_content_id:
        logger.error(
            "Short episode content=%s has no parent_content_id — marking FAILED",
            content_id,
        )
        return {"status": "VISUALS_FAILED", "beats_by_lang": {}}

    # Gate: the remap pass requires the parent's __visual__ VideoSection rows.
    # Those rows are written at the end of _run_visual_pass() — they exist only
    # after the parent's storyboard+Flux generation is complete, independently of
    # whether the parent's final render has finished. If they are not yet present,
    # defer this Short episode so the caller reverts content to AUDIO_DONE and
    # pickup_audio_done() re-queues it on the next Beat cycle. This is a normal
    # wait, not an error.
    parent_visual_ready: bool = (
        db.query(VideoSection)
        .filter(
            VideoSection.content_id == parent_content_id,
            VideoSection.language   == _VISUAL_LANGUAGE,
        )
        .limit(1)
        .first()
    ) is not None

    if not parent_visual_ready:
        logger.warning(
            "CHILD_SHORT_VISUALS_DEFERRED content_id=%s reason=parent_visuals_missing "
            "parent_content_id=%s",
            content_id, parent_content_id,
        )
        return {"status": "CHILD_SHORT_VISUALS_DEFERRED", "beats_by_lang": {}}

    logger.info(
        "Visual pass: SHORT EPISODE — parent __visual__ ready, "
        "will remap beats per-language (content=%s parent=%s)",
        content_id, parent_content_id,
    )

    beats_by_lang: dict[str, list[dict]] = {}
    for language, script in scripts_by_lang.items():
        audio = audio_by_lang.get(language)
        if not audio:
            continue

        logger.info(
            "CHILD_SHORT_VISUALS_START content_id=%s parent_content_id=%s language=%s",
            content_id, parent_content_id, language,
        )
        beats = remap_beats_for_short(
            short_content=content,
            short_voice_script=script.voice_script,
            short_audio_file=audio,
            parent_content_id=parent_content_id,
            db=db,
        )
        if not beats:
            logger.error(
                "Agent4 [FAIL] lang=%s content=%s status=SHORT_REMAP_EMPTY "
                "reason=remap_beats_for_short returned no beats",
                language, content_id,
            )
            continue

        # Same storyboard validation gate the parent path runs (§ "Parent visual
        # readiness" above), applied to the remapped child beats. Child remap has
        # no Claude storyboard retry primitive, but beat-level prompt MAJORs can
        # be repaired deterministically: strip forbidden/readable-text language,
        # regenerate the prompt from visual_intent, revalidate, and only then
        # fall back to the existing log-and-proceed behavior for unresolved MAJORs.
        major_issues = _check_storyboard_issues(beats)
        if major_issues:
            beats, child_repairs = remediate_child_major_storyboard_issues(beats, major_issues)
            for repair in child_repairs:
                logger.warning(
                    "CHILD_STORYBOARD_MAJOR_REMEDIATED content=%s language=%s "
                    "beat_order=%s check=%s old_prompt=%r new_prompt=%r",
                    content_id, language, repair["beat_order"], repair["check"],
                    repair["old_prompt"][:160], repair["new_prompt"][:240],
                )
            if child_repairs:
                logger.info(
                    "CHILD_STORYBOARD_MAJOR_REMEDIATION_DONE content=%s language=%s "
                    "repaired=%d original_major_count=%d",
                    content_id, language, len(child_repairs), len(major_issues),
                )
                major_issues = _check_storyboard_issues(beats)

        if major_issues:
            logger.error(
                "Storyboard MAJOR issue(s) found in child short remap after deterministic remediation — "
                "proceeding (pipeline not blocked per spec). content=%s language=%s "
                "MAJOR_count=%d checks=%s",
                content_id, language, len(major_issues),
                [i["check"] for i in major_issues],
            )

        visual_bible = load_visual_bible_for_content(content_id)
        beats = apply_cinematic_prompts_to_beats(
            beats,
            visual_bible=visual_bible,
            content_kind="child_short",
        )
        final_prompt_issues = _check_final_prompt_issues(
            beats,
            content_id=content_id,
            stage="child_after_enrichment",
            language=language,
        )
        if any(issue["severity"] == "MAJOR" for issue in final_prompt_issues):
            logger.error(
                "Agent4 [FAIL] content=%s language=%s status=FINAL_PROMPT_VALIDATION_FAILED "
                "stage=child_after_enrichment",
                content_id, language,
            )
            continue

        beats, first15_result = apply_first15_enhancement_and_validation(
            beats,
            visual_bible=visual_bible,
            content_kind="child_short",
        )
        if first15_result.status == "FAIL_BLOCKING":
            logger.error(
                "Agent4 [FAIL] content=%s language=%s status=FIRST15_VISUAL_HOOK_FAILED "
                "checked=%d strong=%d generic=%d issues=%s",
                content_id,
                language,
                first15_result.checked_count,
                first15_result.strong_hook_count,
                first15_result.weak_generic_count,
                [issue.code for issue in first15_result.issues],
            )
            continue
        logger.info(
            "Agent4 [FIRST15] content=%s language=%s status=%s checked=%d strong=%d warnings=%d",
            content_id,
            language,
            first15_result.status,
            first15_result.checked_count,
            first15_result.strong_hook_count,
            sum(1 for issue in first15_result.issues if issue.severity == "WARNING"),
        )
        final_prompt_issues = _check_final_prompt_issues(
            beats,
            content_id=content_id,
            stage="child_after_first15",
            language=language,
        )
        if any(issue["severity"] == "MAJOR" for issue in final_prompt_issues):
            logger.error(
                "Agent4 [FAIL] content=%s language=%s status=FINAL_PROMPT_VALIDATION_FAILED "
                "stage=child_after_first15",
                content_id, language,
            )
            continue

        # Duplicate-prompt repair (audit G-5.3) — last mutation of flux_prompt
        # before Flux; only touches beats still pending (media_url empty), so
        # a deliberately reused parent image is never rewritten.
        beats = _repair_duplicate_prompts(beats, content_id=content_id, language=language)

        # Generation happens AFTER validation, not before (Phase 4E-E ordering
        # alignment) — the remap step above deliberately left any
        # below-threshold beat's media_url empty so the validation gate above
        # ran before any fal.ai call, mirroring the parent path's
        # validate-then-generate order. This call now regenerates EVERY beat
        # at portrait size (1080x1920, roadmap 3.1) — including beats that
        # were "reused" from the parent's landscape cache — since a Short
        # must never render a landscape image; see
        # generate_pending_beat_images()'s docstring.
        beats = generate_pending_beat_images(beats, str(content_id))

        _save_video_sections(content_id, language, beats, db)
        db.commit()
        save_beat_review_metadata(content_id, language, beats)
        logger.info(
            "CHILD_SHORT_VISUALS_DONE content_id=%s language=%s beats=%d",
            content_id, language, len(beats),
        )
        beats_by_lang[language] = beats

    status = "CHILD_SHORT_VISUALS_DONE" if beats_by_lang else "VISUALS_FAILED"
    return {"status": status, "beats_by_lang": beats_by_lang}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _save_video_sections(
    content_id: uuid.UUID,
    language: str,
    sections: list[dict],
    db: Session,
) -> None:
    """Persist beat dicts to video_sections (delete-then-insert).

    Beat fields not in the schema proper (visual_intent, visual_type, environment,
    motif, transition_to_next, media_url, media_type, source_script_sha256) are
    JSON-serialized into ``generation_prompt`` for re-entrant loading — see
    ``_beat_extras()`` for the authoritative field list (slimmed by roadmap 6.5).
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


def _beat_extras(s: dict) -> dict:
    """Collect the fields stored in generation_prompt JSON for re-entrant loading.

    Roadmap 6.5 / audit AR-3: this used to also carry ~19 review-only fields
    (first15 diagnostics, cinematic continuity tags, prompt quality warnings)
    that nothing in the live pipeline ever reads back — only the local HTML
    review page did. Those now go to a run-folder JSON file instead
    (`visual_review.save_beat_review_metadata()`), keeping this DB-persisted
    blob to the fields something downstream actually consumes.
    """
    return {
        "visual_intent":      s.get("visual_intent", ""),
        "visual_type":        s.get("visual_type", "b-roll"),
        "visual_category":    s.get("visual_category", "place"),
        "environment":        s.get("environment", "other"),
        "motif":              s.get("motif", "other"),
        "transition_to_next": s.get("transition_to_next", "cut"),
        # overlay_text/overlay_position are NOT persisted (subtitles-only
        # rendering) — the shared loader forces ""/"none" on every load, so
        # writing them here would be two dead keys per row per language.
        # Local Flux image path — the canonical media_url for re-entrant runs
        "media_url":          s.get("media_url", ""),
        "media_type":         s.get("media_type", "image"),
        "media_strategy":     s.get("media_strategy", "flux_generated"),
        "text_card_style":    s.get("text_card_style", "default"),
        # Stale-visuals guard (audit V-6b) — see _check_shared_beats_staleness().
        "source_script_sha256": s.get("source_script_sha256", ""),
    }
