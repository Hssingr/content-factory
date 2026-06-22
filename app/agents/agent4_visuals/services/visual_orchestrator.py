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

import json
import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.models import AudioFile, Channel, ChannelConfig, Content, Script, VideoSection
from app.agents.agent4_visuals.subagents.section_splitter import split_into_sections
from app.agents.agent4_visuals.subagents.storyboard import (
    split_into_beats, remap_beats_for_short, generate_pending_beat_images,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    validate_storyboard, validate_media_assets,
)
from app.agents.agent4_visuals.services.flux_generator import generate_all_beat_images
from app.agents.agent4_visuals.system_prompt import (
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION,
    enrich_sections_with_visuals,
)

logger = logging.getLogger(__name__)

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

    result = run_visual_generation(
        content=content,
        channel=channel,
        scripts_by_lang=scripts_by_lang,
        audio_by_lang=audio_by_lang,
        script_format=script_format,
        allow_legacy_fallback=allow_legacy_fallback,
        db=db,
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

    # status is "PARENT_VISUALS_DONE" or "CHILD_SHORT_VISUALS_DONE" — Agent 4
    # is the sole writer of these statuses; pickup_visual_ready reads them.
    content.status = status
    db.commit()
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
) -> dict:
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
        _check_media_assets(content_id, language, beats_for_lang, db)
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
        "Agent4 [VISUAL_PASS] content=%s source_lang=%s "
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
                    "Agent4 [FAIL] content=%s status=STORYBOARD_FAILED "
                    "reason=legacy_splitter_empty",
                    content_id,
                )
                return None, 0
        else:
            logger.error(
                "Agent4 [FAIL] content=%s status=STORYBOARD_FAILED "
                "reason=storyboard_generation_failed (allow_legacy_fallback=False)",
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
    )
    if beats is None:
        logger.error(
            "Agent4 [FAIL] content=%s status=STORYBOARD_VALIDATION_FAILED "
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
        "Agent4 [FLUX_DONE] content=%s beats=%d flux_ok=%d text_card=%d",
        content_id, len(beats), succeeded, text_card_count,
    )

    # ── 4. Update saved beats with Flux media_url ─────────────────────────────
    _save_shared_beats(content_id, beats, db)
    db.commit()
    logger.info("PARENT_VISUALS_DONE content_id=%s beats=%d", content_id, len(beats))

    return beats, source_duration_ms


def _check_storyboard_issues(beats: list[dict]) -> list[dict]:
    """Run validate_storyboard() and log MINOR findings; return the MAJOR ones.

    This is the single call site for ``validate_storyboard()`` shared by both
    the parent storyboard path and the child remap path — neither caller
    forks or re-implements the validator itself, only what happens after a
    MAJOR finding differs (parent can retry via a full storyboard re-run;
    child remap has no equivalent regeneration primitive and logs/proceeds
    immediately, the same terminal behavior the parent falls back to when its
    own retry still leaves MAJOR issues).

    Returns:
        The MAJOR issues found (empty list if the storyboard is clean).
    """
    issues = validate_storyboard(beats)
    minor_issues = [i for i in issues if i["severity"] == "MINOR"]
    major_issues = [i for i in issues if i["severity"] == "MAJOR"]

    for issue in minor_issues:
        logger.warning(
            "Storyboard MINOR: beat=%d check=%s — %s",
            issue["beat_order"], issue["check"], issue["description"][:200],
        )

    return major_issues


def _check_media_assets(
    content_id: uuid.UUID,
    language: str,
    beats: list[dict],
    db: Session,
) -> list[dict]:
    """Run media existence/integrity/reuse checks, plus a persistence
    round-trip comparison, AFTER `_save_video_sections()` has already
    committed for this language. The single call site for
    `validate_media_assets()`, shared by both the parent and child paths —
    neither caller forks or re-implements it.

    Unlike `_check_storyboard_issues()`, this runs post-persistence, not
    pre-generation: a file's existence cannot be checked before it exists,
    and a persistence round-trip cannot be checked before the row is saved.

    All findings are MAJOR and all are logged at ERROR; none block the
    pipeline — there is no retry/regeneration mechanism for a missing or
    corrupt media file (Phase 4E-E's remediation classification covers the
    future-work options, none implemented). This call is observability only.

    Returns:
        The MAJOR issues found (empty list if every beat's media reference
        is present, well-formed, exists on disk, and survived persistence
        unchanged).
    """
    issues = list(validate_media_assets(beats, str(content_id)))

    # Persistence round-trip: reload from the DB and compare media_url/
    # media_type against what was just saved. This is the regression guard
    # for a future _build_beat_section()-style bug (Phase 4E-E/4D-E0) —
    # it runs every time, in production, not just in a smoke test.
    reloaded_by_order = {
        s["section_order"]: s for s in _load_sections_from_db(content_id, language, db)
    }
    for beat in beats:
        order = beat.get("section_order", beat.get("beat_order", 0))
        reloaded = reloaded_by_order.get(order)
        if reloaded is None:
            issues.append({
                "severity": "MAJOR",
                "beat_order": order,
                "check": "persistence_row_missing",
                "description": (
                    f"beat_order={order} was saved but no matching VideoSection "
                    f"row was found on reload (content={content_id} language={language})."
                ),
            })
            continue
        expected_url = beat.get("media_url", "")
        actual_url = reloaded.get("media_url", "")
        if expected_url != actual_url:
            issues.append({
                "severity": "MAJOR",
                "beat_order": order,
                "check": "persistence_media_url_mismatch",
                "description": (
                    f"beat_order={order} media_url changed across persistence: "
                    f"saved={expected_url!r} but reloaded={actual_url!r} "
                    f"(content={content_id} language={language})."
                ),
            })
        expected_type = beat.get("media_type", "image")
        actual_type = reloaded.get("media_type", "image")
        if expected_type != actual_type:
            issues.append({
                "severity": "MAJOR",
                "beat_order": order,
                "check": "persistence_media_type_mismatch",
                "description": (
                    f"beat_order={order} media_type changed across persistence: "
                    f"saved={expected_type!r} but reloaded={actual_type!r} "
                    f"(content={content_id} language={language})."
                ),
            })

    if issues:
        logger.error(
            "MediaAsset MAJOR issue(s) found — observability only, not blocking. "
            "content=%s language=%s MAJOR_count=%d checks=%s",
            content_id, language, len(issues), [i["check"] for i in issues],
        )

    return issues


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
    major_issues = _check_storyboard_issues(beats)

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
        # no regeneration primitive to retry against (unlike split_into_beats()),
        # so a MAJOR finding here is logged and the pipeline proceeds — the same
        # terminal behavior the parent falls back to when its own retry still
        # leaves MAJOR issues. Coverage only; no new rule, threshold, or status.
        major_issues = _check_storyboard_issues(beats)
        if major_issues:
            logger.error(
                "Storyboard MAJOR issue(s) found in child short remap — no retry "
                "primitive for remap, proceeding (pipeline not blocked per spec). "
                "content=%s language=%s MAJOR_count=%d checks=%s",
                content_id, language, len(major_issues),
                [i["check"] for i in major_issues],
            )

        # Generation happens AFTER validation, not before (Phase 4E-E ordering
        # alignment) — the remap step above deliberately left any
        # below-threshold beat's media_url empty so the validation gate above
        # ran before any fal.ai call, mirroring the parent path's
        # validate-then-generate order. This call fills in those pending
        # images; it is not a retry of the remap itself.
        beats = generate_pending_beat_images(beats, str(content_id))

        _save_video_sections(content_id, language, beats, db)
        db.commit()
        _check_media_assets(content_id, language, beats, db)
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
    """Load VideoSection rows as dicts compatible with the visual/render pipeline."""
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
