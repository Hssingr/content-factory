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
import uuid

from sqlalchemy.orm import Session

from app.models import AudioFile, Channel, ChannelConfig, Content, Script, VideoSection
from app.agents.agent4_visuals.subagents.storyboard import (
    split_into_beats, remap_beats_for_short, generate_pending_beat_images,
    build_continuity_line_from_blueprint,
)
from app.agents.agent4_visuals.subagents.storyboard_validator import (
    validate_storyboard, repair_duplicate_flux_prompts,
)
from app.agents.agent4_visuals.services.flux_generator import generate_all_beat_images
from app.agents.agent4_visuals.services.media_validation import validate_visual_media_assets
from app.agents.agent4_visuals.services.visual_review import (
    generate_visual_review_html, save_beat_review_metadata,
)
from app.services.local_run_paths import ensure_run_dirs
from app.services.video_sections import load_video_sections
from app.agents.agent4_visuals.system_prompt import (
    STORYBOARD_SCHEMA_VERSION as _STORYBOARD_SCHEMA_VERSION,
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
        return _run_child_short_visuals(
            content, channel, scripts_by_lang, audio_by_lang, db,
            visual_style=visual_style, image_style=image_style,
        )

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
    current_audio_duration_ms = _source_audio_duration_ms(content, audio_by_lang)

    # Stale-visuals guard (audit V-6b + roadmap 2d / audit P0-3): a script
    # regenerated after the visual pass already ran (operator
    # --force-scripts, or any retry that changes the source narration) must
    # never let the OLD beats be silently reused — and neither must a
    # duration correction on an unchanged script (the audited incident's
    # actual repair scenario: fixing a corrupted duration_ms in place).
    # Either fingerprint mismatching is enough to force a regeneration.
    script_staleness = (
        _check_shared_beats_staleness(content_id, shared_beats, current_script_hash)
        if shared_beats else "fresh"
    )
    audio_staleness = (
        _check_audio_duration_staleness(content_id, shared_beats, current_audio_duration_ms)
        if shared_beats else "fresh"
    )
    if "stale" in (script_staleness, audio_staleness):
        shared_beats = []
    else:
        needs_backfill_save = False
        if script_staleness == "backfill":
            _tag_beats_with_script_hash(shared_beats, current_script_hash)
            needs_backfill_save = True
        if audio_staleness == "backfill":
            _tag_beats_with_audio_duration(shared_beats, current_audio_duration_ms)
            needs_backfill_save = True
        if needs_backfill_save:
            logger.info(
                "STALE_VISUALS_CHECK_BACKFILL content_id=%s beats=%d — no baseline "
                "staleness fingerprint(s) stored on these beats yet; stamping current "
                "value(s) without forcing a regeneration",
                content_id, len(shared_beats),
            )
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
            blueprint=content.story_blueprint,
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

    src_audio = audio_by_lang.get(content.source_language)
    if source_duration_ms == 0:
        # Fallback: use source language audio duration
        source_duration_ms = src_audio.duration_ms if src_audio else 0
    source_sections = src_audio.section_boundaries if src_audio else None

    beats_by_lang: dict[str, list[dict]] = {}
    for language, _script in scripts_by_lang.items():
        audio = audio_by_lang.get(language)
        if not audio:
            continue
        beats_for_lang = _remap_beats_timing(
            shared_beats, audio.duration_ms, source_duration_ms,
            source_sections=source_sections,
            target_sections=audio.section_boundaries,
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
    blueprint: dict | None = None,
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
        blueprint: ``Content.story_blueprint``, used to build a deterministic
            continuity line (see ``build_continuity_line_from_blueprint()``)
            — no AI call, replaces the deleted visual-bible layer (Elimination
            Mandate, code_report/forensic_output_audit_borrasca_run.md, D2.1).

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

    continuity_line = build_continuity_line_from_blueprint(blueprint)

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
        continuity_line=continuity_line,
        content_id=str(content_id),
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

    # The second, flat-2000ms micro-beat cleanup that used to run here is
    # deleted (fresh full-system audit §3.2): it systematically merged away
    # every 1.0-2.0s high-intensity reveal beat the storyboard prompt
    # explicitly designs (high tier = 1.0-2.5s), and — because it ran AFTER
    # split_into_beats() applied the parent hold cap — merging could silently
    # re-create holds above the 9s ceiling. The mapping-level intensity floors
    # (INTENSITY_FLOOR_MS), micro-merge discard guard, and density invariant
    # inside split_into_beats() already guarantee no zero-width or absurd
    # beats, with the intensity awareness the flat floor lacked.
    logger.info(
        "Agent4 [STORYBOARD] content=%s beats=%d source_lang=%s",
        content_id, len(beats), source_lang,
    )

    # ── 1b. Storyboard validation gate ────────────────────────────────────────
    # Runs after storyboard is complete and before any fal.ai calls. The sole
    # remaining deterministic validator (Elimination Mandate D2.4). Telemetry
    # only (D1.5) — MAJOR findings are logged, never trigger a retry. The
    # issues list is passed straight into the duplicate-prompt repair so the
    # full validator (and its always-on diagnostics) runs exactly once per
    # path (fresh full-system audit §3.3 — it used to run twice, double-
    # logging every diagnostic).
    issues = _collect_storyboard_issues(beats)
    _log_storyboard_major_issues(issues)

    # ── 1c. Duplicate-prompt repair (audit G-5.3) — last mutation of
    # flux_prompt before Flux. ──
    beats = _repair_duplicate_prompts(
        beats, content_id=content_id, language=source_lang, issues=issues,
    )

    # Stale-visuals guard (audit V-6b / roadmap 2d, P0-3): stamp both
    # staleness fingerprints onto every beat before the FIRST save, so they
    # survive (in-place mutation) through the post-Flux save below too.
    if script_hash:
        _tag_beats_with_script_hash(beats, script_hash)
    _tag_beats_with_audio_duration(beats, source_duration_ms)

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


def _repair_duplicate_prompts(
    beats: list[dict],
    *,
    content_id: uuid.UUID,
    language: str = "",
    include_resolved: bool = False,
    issues: list[dict] | None = None,
) -> list[dict]:
    """Deterministically vary surviving duplicate-prompt beats (audit G-5.3).

    The single call site for `repair_duplicate_flux_prompts()` — shared by
    the parent path (both fresh generation and flux-incomplete recovery) and
    the child remap path. Must run BEFORE any Flux call, so the repaired text
    is what actually reaches fal.ai — this is the last mutation of
    `flux_prompt` before generation.

    ``issues``: the already-computed ``validate_storyboard()`` findings from
    this path's validation gate — passing them avoids a second full validator
    run (audit §3.3). The flux-incomplete recovery path omits it (no prior
    validation ran there), so the repair validates once itself.
    """
    repaired, repairs = repair_duplicate_flux_prompts(
        beats, include_resolved=include_resolved, issues=issues,
    )
    if not repairs:
        logger.info(
            "DUPLICATE_PROMPT_REPAIR_OK content=%s language=%s beats=%d include_resolved=%s",
            content_id, language or "__visual__", len(beats), include_resolved,
        )
        return repaired

    for repair in repairs:
        logger.warning(
            "DUPLICATE_PROMPT_REPAIRED content=%s language=%s beat_order=%s "
            "check=%s include_resolved=%s variation=%r",
            content_id, language or "__visual__", repair["beat_order"],
            repair["check"], include_resolved, repair["variation"],
        )
    logger.info(
        "DUPLICATE_PROMPT_REPAIR_DONE content=%s language=%s beats=%d repaired=%d include_resolved=%s",
        content_id, language or "__visual__", len(beats), len(repairs), include_resolved,
    )
    return repaired


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
    the parent storyboard path and the child remap path. Neither caller forks
    or re-implements the validator itself. MAJOR findings never trigger a
    regeneration on either path (Elimination Mandate D1.5,
    code_report/forensic_output_audit_borrasca_run.md) — they are telemetry
    only; both paths log and proceed.

    Returns:
        The MAJOR issues found (empty list if the storyboard is clean).
    """
    return [i for i in _collect_storyboard_issues(beats) if i["severity"] == "MAJOR"]


def _log_storyboard_major_issues(issues: list[dict]) -> None:
    """Log MAJOR validator findings — telemetry only, never a retry (D1.5)."""
    major_issues = [i for i in issues if i["severity"] == "MAJOR"]
    if major_issues:
        logger.warning(
            "Storyboard MAJOR issue(s) found — telemetry only, no retry. "
            "MAJOR_count=%d checks=%s",
            len(major_issues), [i["check"] for i in major_issues],
        )


def _run_storyboard_validation(beats: list[dict]) -> list[dict]:
    """Run the storyboard validation gate as telemetry only.

    Elimination Mandate D1.5: MAJOR findings are logged and the storyboard is
    returned unchanged; MINOR issues are logged at WARNING only. Kept as a
    self-contained unit entrypoint (tests exercise it directly); the
    production path in ``_run_visual_pass()`` calls
    ``_collect_storyboard_issues()`` + ``_log_storyboard_major_issues()``
    itself so the same issues list can feed the duplicate-prompt repair
    without a second full validation run (fresh full-system audit §3.3).
    """
    _log_storyboard_major_issues(_collect_storyboard_issues(beats))
    return beats


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


def _source_script_hash(
    content: Content,
    scripts_by_lang: dict[str, Script],
) -> str | None:
    """Source-language script fingerprint for parent shared visuals."""
    return _script_hash(
        scripts_by_lang.get(content.source_language)
    )

def _script_hash(script: Script | None) -> str | None:
    """SHA-256 fingerprint of one persisted script's narration."""
    if not script or not script.voice_script:
        return None

    return hashlib.sha256(
        script.voice_script.encode("utf-8")
    ).hexdigest()

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


def _source_audio_duration_ms(
    content: Content, audio_by_lang: dict[str, AudioFile],
) -> int | None:
    """Source-language `AudioFile.duration_ms` — the audio-side staleness
    fingerprint for the shared `__visual__` beats (roadmap 2d / audit P0-3,
    `code_report/forensic_output_audit_borrasca_run.md`). The script-hash
    fingerprint alone does not catch a duration correction on an unchanged
    script — exactly the audited incident's repair scenario: a corrupted
    `duration_ms` fixed in place with the narration text untouched, which
    the script hash could never detect. Returns ``None`` when the
    source-language audio is unavailable (fail open, never force an
    unwarranted regeneration).
    """
    source_audio = audio_by_lang.get(content.source_language)
    if not source_audio or not source_audio.duration_ms:
        return None
    return source_audio.duration_ms


def _tag_beats_with_audio_duration(beats: list[dict], duration_ms: int) -> None:
    """Stamp `duration_ms` onto every beat in place — same convention as
    `_tag_beats_with_script_hash()`."""
    for beat in beats:
        beat["source_audio_duration_ms"] = duration_ms


def _check_audio_duration_staleness(
    content_id: uuid.UUID,
    shared_beats: list[dict],
    current_duration_ms: int | None,
) -> str:
    """Classify loaded `__visual__` beats against the current source audio
    duration — the audio-side half of the stale-visuals guard (roadmap 2d /
    audit P0-3). Same `"fresh"`/`"stale"`/`"backfill"` contract as
    `_check_shared_beats_staleness()`.
    """
    if current_duration_ms is None:
        logger.debug(
            "STALE_VISUALS_CHECK_SKIPPED content_id=%s reason=source_audio_unavailable",
            content_id,
        )
        return "fresh"

    stored_duration_ms = shared_beats[0].get("source_audio_duration_ms") or 0
    if not stored_duration_ms:
        return "backfill"

    if stored_duration_ms == current_duration_ms:
        return "fresh"

    logger.warning(
        "PARENT_VISUALS_STALE_AUDIO_DURATION content_id=%s stored_ms=%d current_ms=%d "
        "beats=%d — source audio duration changed since these beats were generated "
        "(e.g. a corrupted duration_ms was corrected) — discarding and regenerating "
        "the visual pass",
        content_id, stored_duration_ms, current_duration_ms, len(shared_beats),
    )
    return "stale"


def _remap_beats_timing(
    beats: list[dict],
    target_duration_ms: int,
    source_duration_ms: int,
    source_sections: list[dict] | None = None,
    target_sections: list[dict] | None = None,
) -> list[dict]:
    """Return a copy of beats with timestamps scaled to target_duration_ms.

    When all languages have identical audio duration (common for single-language
    channels), this is a no-op.

    Per-section anchoring (roadmap Phase B1 — the parent multi-language
    "décalage" fix): a real production run showed the whole-video
    proportional stretch below drifting up to ±20s out of sync with real
    non-source-language narration pacing, since narration speed is not
    uniform across a video's length. When both ``source_sections`` and
    ``target_sections`` are usable (see ``_build_section_anchor_map()``),
    each beat is remapped using the local stretch ratio of the
    ``[INTRO]``/``[SECTION N]``/``[OUTRO]`` section it falls within, instead
    of one ratio for the whole video — bounding drift to within one section
    (~2-3s) rather than the whole video's length. Falls back to the
    original whole-video stretch, logged, whenever section data is missing,
    mismatched, or structurally inconsistent between languages — never
    guesses a correspondence.

    Args:
        beats:               Source beats from the visual pass.
        target_duration_ms:  This language's audio duration.
        source_duration_ms:  Duration of the source audio used for storyboard generation.
        source_sections:     Source language's ``AudioFile.section_boundaries``, if any.
        target_sections:     This language's ``AudioFile.section_boundaries``, if any.

    Returns:
        New list of beat dicts with re-scaled audio_start_ms / audio_end_ms.
    """
    if source_duration_ms == 0 or source_duration_ms == target_duration_ms:
        return list(beats)

    section_pairs, reason = _build_section_anchor_map(source_sections, target_sections)
    if section_pairs is not None:
        result = _remap_beats_timing_per_section(beats, section_pairs)
        logger.info(
            "VISUAL_TIMING_SECTION_ANCHORED sections=%d source_duration_ms=%d target_duration_ms=%d",
            len(section_pairs), source_duration_ms, target_duration_ms,
        )
    else:
        logger.info(
            "VISUAL_TIMING_WHOLE_VIDEO_STRETCH_FALLBACK reason=%s source_duration_ms=%d target_duration_ms=%d",
            reason, source_duration_ms, target_duration_ms,
        )
        result = _remap_beats_timing_whole_video(beats, target_duration_ms, source_duration_ms)

    # Clamp last beat to exactly target_duration_ms (shared final step,
    # regardless of which remap path produced `result`).
    if result:
        last = result[-1]
        last["audio_end_ms"] = target_duration_ms
        last["duration_sec"] = (target_duration_ms - last["audio_start_ms"]) / 1000

    return result


def _remap_beats_timing_whole_video(
    beats: list[dict],
    target_duration_ms: int,
    source_duration_ms: int,
) -> list[dict]:
    """The original single-ratio proportional stretch — used as the fallback
    when per-section anchor data isn't usable (see _remap_beats_timing)."""
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
    return result


def _build_section_anchor_map(
    source_sections: list[dict] | None,
    target_sections: list[dict] | None,
) -> tuple[list[tuple[dict, dict]] | None, str]:
    """Pair up source/target sections 1:1, in order, by (section_type,
    section_index) — never by label text, which is itself translated per
    language. Returns (None, reason) when the caller must fall back to the
    whole-video stretch instead: either side missing/empty, section counts
    differing (a translated script legitimately can drop/gain a section —
    CLAUDE.md §9.3's section_loss check is telemetry-only, not blocking),
    a structural (type/index) mismatch at the same position, or a
    degenerate (non-positive) span on either side.
    """
    if not source_sections or not target_sections:
        return None, "missing_section_boundaries"
    if len(source_sections) != len(target_sections):
        return None, f"section_count_mismatch source={len(source_sections)} target={len(target_sections)}"

    src_sorted = sorted(source_sections, key=lambda s: s.get("start_ms", 0))
    tgt_sorted = sorted(target_sections, key=lambda s: s.get("start_ms", 0))

    pairs: list[tuple[dict, dict]] = []
    for src, tgt in zip(src_sorted, tgt_sorted):
        if src.get("section_type") != tgt.get("section_type"):
            return None, "section_structure_mismatch"
        if src.get("section_index") != tgt.get("section_index"):
            return None, "section_structure_mismatch"
        if src.get("end_ms", 0) <= src.get("start_ms", 0):
            return None, "degenerate_section_span"
        if tgt.get("end_ms", 0) <= tgt.get("start_ms", 0):
            return None, "degenerate_section_span"
        pairs.append((src, tgt))

    return pairs, "ok"


def _find_section_pair(
    section_pairs: list[tuple[dict, dict]], start_ms: int,
) -> tuple[dict, dict]:
    """Return the (source, target) section pair containing ``start_ms``.

    Section pairs are contiguous and cover [0, duration_ms) by construction
    (tts._compute_section_boundaries()), so a beat starting inside any
    section always matches exactly one pair. A beat starting at/after the
    last section's end, or before the first section's start (both only
    possible from integer-rounding at the very edges), clamps to the
    nearest section rather than raising — a boundary rounding case, not a
    data error.
    """
    for src, tgt in section_pairs:
        if src["start_ms"] <= start_ms < src["end_ms"]:
            return src, tgt
    if start_ms < section_pairs[0][0]["start_ms"]:
        return section_pairs[0]
    return section_pairs[-1]


def _remap_beats_timing_per_section(
    beats: list[dict], section_pairs: list[tuple[dict, dict]],
) -> list[dict]:
    """Remap each beat using the local stretch ratio of the section it
    starts within, instead of one whole-video ratio (see _remap_beats_timing).

    A beat that straddles a section boundary is still remapped entirely
    using its start section's local ratio — a deliberate, bounded
    simplification: the resulting drift for that one beat is at most the
    difference between two adjacent sections' ratios, applied over the
    beat's own (seconds-scale) span, nowhere near the whole-video drift
    this mechanism replaces.
    """
    result: list[dict] = []
    for b in beats:
        start = b.get("audio_start_ms", 0)
        end   = b.get("audio_end_ms", 0)
        src, tgt = _find_section_pair(section_pairs, start)
        src_span = src["end_ms"] - src["start_ms"]
        local_ratio = (tgt["end_ms"] - tgt["start_ms"]) / src_span

        new_beat = dict(b)
        new_beat["audio_start_ms"] = int(round(tgt["start_ms"] + (start - src["start_ms"]) * local_ratio))
        new_beat["audio_end_ms"]   = int(round(tgt["start_ms"] + (end   - src["start_ms"]) * local_ratio))
        new_beat["duration_sec"]   = (
            new_beat["audio_end_ms"] - new_beat["audio_start_ms"]
        ) / 1000
        result.append(new_beat)

    return result


# The orchestrator-level flat-2000ms micro-beat cleanup that lived here was
# deleted (fresh full-system audit §3.2) — see the note at its former call
# site in _run_visual_pass(). storyboard.py's intensity-aware
# _cleanup_micro_beats (INTENSITY_FLOOR_MS) is the single remaining
# micro-beat mechanism.


# ── Child short visual readiness ───────────────────────────────────────────────

def _run_child_short_visuals(
    content: Content,
    channel: Channel,
    scripts_by_lang: dict[str, Script],
    audio_by_lang: dict[str, AudioFile],
    db: Session,
    visual_style: str = "",
    image_style: str = "",
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

        # Idempotency guard (fresh full-system audit §3.6): a re-dispatched
        # child (defer loop, crash retry) used to re-run the remap Claude call
        # and regenerate every portrait image even when this language's rows
        # were already complete — remap output is nondeterministic, so each
        # re-run produced different prompts and a full fresh generation. If
        # complete persisted rows exist (every beat has a local image), reuse
        # them — mirroring the parent path's persisted-beat reuse. (Child
        # scripts are never regenerated in place — run_shorts_planner skips
        # existing children — so no script-hash staleness check is needed here.)
        current_script_hash = _script_hash(script)
        current_audio_duration_ms = (
            int(audio.duration_ms)
            if audio.duration_ms
            else None
        )

        existing_sections = load_video_sections(
            content_id,
            language,
            db,
        )

        existing_complete = bool(existing_sections) and all(
            (beat.get("media_url") or "").startswith("cache/")
            for beat in existing_sections
        )

        if existing_complete:
            stored_script_hash = (
                existing_sections[0].get("source_script_sha256")
                or ""
            )
            stored_audio_duration_ms = (
                existing_sections[0].get("source_audio_duration_ms")
                or 0
            )

            script_stale = (
                current_script_hash is not None
                and bool(stored_script_hash)
                and stored_script_hash != current_script_hash
            )
            audio_stale = (
                current_audio_duration_ms is not None
                and bool(stored_audio_duration_ms)
                and stored_audio_duration_ms
                != current_audio_duration_ms
            )

            if script_stale or audio_stale:
                logger.warning(
                    "CHILD_SHORT_VISUALS_STALE "
                    "content_id=%s language=%s "
                    "script_stale=%s audio_stale=%s "
                    "stored_script=%s current_script=%s "
                    "stored_audio_ms=%s current_audio_ms=%s "
                    "— discarding persisted child visuals and regenerating",
                    content_id,
                    language,
                    script_stale,
                    audio_stale,
                    stored_script_hash[:12],
                    (
                        current_script_hash[:12]
                        if current_script_hash
                        else ""
                    ),
                    stored_audio_duration_ms,
                    current_audio_duration_ms,
                )
            else:
                needs_backfill = False

                if (
                    current_script_hash is not None
                    and not stored_script_hash
                ):
                    _tag_beats_with_script_hash(
                        existing_sections,
                        current_script_hash,
                    )
                    needs_backfill = True

                if (
                    current_audio_duration_ms is not None
                    and not stored_audio_duration_ms
                ):
                    _tag_beats_with_audio_duration(
                        existing_sections,
                        current_audio_duration_ms,
                    )
                    needs_backfill = True

                if needs_backfill:
                    _save_video_sections(
                        content_id,
                        language,
                        existing_sections,
                        db,
                    )
                    db.commit()

                    logger.info(
                        "CHILD_SHORT_VISUALS_FINGERPRINT_BACKFILLED "
                        "content_id=%s language=%s beats=%d",
                        content_id,
                        language,
                        len(existing_sections),
                    )

                logger.info(
                    "CHILD_SHORT_VISUALS_REUSED "
                    "content_id=%s language=%s beats=%d "
                    "— persisted rows complete and fingerprints fresh",
                    content_id,
                    language,
                    len(existing_sections),
                )

                beats_by_lang[language] = existing_sections
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
            visual_style=visual_style,
            image_style=image_style,
            channel_niche=channel.niche or "",
            channel_tone=channel.tone or "",
        )
        if not beats:
            logger.error(
                "Agent4 [FAIL] lang=%s content=%s status=SHORT_REMAP_EMPTY "
                "reason=remap_beats_for_short returned no beats",
                language, content_id,
            )
            continue

        # Same storyboard validation gate the parent path runs (§ "Parent visual
        # readiness" above), applied to the remapped child beats. Telemetry only
        # (Elimination Mandate D1.5): MAJOR findings are logged and the child
        # proceeds unchanged. The issues list feeds the duplicate-prompt repair
        # directly so the validator runs exactly once (audit §3.3).
        issues = _collect_storyboard_issues(beats)
        major_issues = [i for i in issues if i["severity"] == "MAJOR"]
        if major_issues:
            logger.error(
                "Storyboard MAJOR issue(s) found in child short remap — telemetry only, "
                "no remediation attempted, proceeding (pipeline not blocked per spec). "
                "content=%s language=%s MAJOR_count=%d checks=%s",
                content_id, language, len(major_issues),
                [i["check"] for i in major_issues],
            )

        # Duplicate-prompt repair — last mutation of flux_prompt before Flux.
        # Every child beat leaves remap_beats_for_short() pending
        # (media_url="", audit §3.1), so the default pending-only repair scope
        # covers the whole list.
        beats = _repair_duplicate_prompts(
            beats, content_id=content_id, language=language, issues=issues,
        )

        # Generation happens AFTER validation, not before (Phase 4E-E ordering
        # alignment) — the remap step above deliberately left any
        # below-threshold beat's media_url empty so the validation gate above
        # ran before any fal.ai call, mirroring the parent path's
        # validate-then-generate order. This call now regenerates EVERY beat
        # at portrait size (1080x1920, roadmap 3.1) — including beats that
        # were "reused" from the parent's landscape cache — since a Short
        # must never render a landscape image; see
        # generate_pending_beat_images()'s docstring.
        beats = generate_pending_beat_images(
            beats,
            str(content_id),
        )

        if current_script_hash is not None:
            _tag_beats_with_script_hash(
                beats,
                current_script_hash,
            )

        if current_audio_duration_ms is not None:
            _tag_beats_with_audio_duration(
                beats,
                current_audio_duration_ms,
            )

        _save_video_sections(
            content_id,
            language,
            beats,
            db,
        )
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
        "script_text_source": s.get("script_text_source", "provided"),
        "script_text_missing": bool(s.get("script_text_missing", False)),
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
        # Stale-visuals guard, audio side (roadmap 2d / audit P0-3) — see
        # _check_audio_duration_staleness().
        "source_audio_duration_ms": s.get("source_audio_duration_ms", 0),
    }
