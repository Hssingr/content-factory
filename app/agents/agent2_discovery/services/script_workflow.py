"""Agent 2 script workflow orchestration."""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.agent2_discovery.services.scripts import (
    _collect_short_script_major_issues,
    _resolve_short_word_bounds,
    _script_trace,
    generate_multilingual_scripts,
    generate_script_sections,
    run_script_quality_gate,
    run_shorts_planner,
)
from app.agents.agent2_discovery.services.story import MAX_SOURCE_EXCERPT_CHARS, Story
from app.agents.agent2_discovery.services.story_generator import expand_story_premise
from app.agents.agent2_discovery.services.narration_pov import normalize_narration_pov
from app.agents.agent2_discovery.system_prompt import generate_story_blueprint, generate_solo_short_script
from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script
from app.services.local_run_paths import ensure_run_dirs
from app.services.script_checks import check_source_material_floor, SOLO_SHORT_SCRIPT_FORMAT
from app.services.script_estimator import estimate_duration_sec
from app.services.script_source import normalize_script_source

logger = logging.getLogger(__name__)

# B2 ceiling-regen (Task 2d, code_report/TODO, 2026-08-05): the retry target
# is this many words BELOW the real cap (static or per-channel calibrated),
# not the cap itself — a retry that lands exactly at the ceiling has no
# margin left for ordinary TTS/pacing variance and risks tipping back over
# it. At the static default (_MAX_SHORT_WORDS=270) this is a 255-word
# target, scaling proportionally for a calibrated cap.
_CEILING_REGEN_TARGET_MARGIN = 15


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

    AI-generated story expansion (``ChannelConfig.script_source="ai_generated"``,
    see ``code_report/ai_generated_story_discovery_design.md``): runs before
    the source-material floor check above — ``content.source_excerpt`` holds
    only the short discovery-time premise until this point, and
    ``_ensure_ai_story_expanded()`` expands it into a full story body in
    place. The floor check then judges the expanded body exactly like it
    already judges a Reddit-discovered body — one shared pass/fail
    implementation for both paths. Any ``story`` argument the caller passed
    in is discarded and rebuilt from the just-expanded ``content`` row for
    this path — a pre-expansion premise ``Story`` would otherwise silently
    ground the blueprint/sections on the short premise instead of the real
    expanded story.

    Args:
        content: Approved parent `Content` row.
        db:      SQLAlchemy session managed by the caller.
        story:   Optional real discovery `Story`; when omitted (or when
                 ``script_source="ai_generated"``, regardless of what was
                 passed), rebuilt from the content row (`source_excerpt` as
                 body).
        context: Optional preloaded `ScriptWorkflowContext` (saves the caller
                 a duplicate load); loaded from the DB when omitted.

    Returns:
        The persisted source voice_script text, or ``None`` when the channel
        context could not be loaded, AI-generated expansion failed outright,
        or the source-material floor failed.
    """
    context = context or _load_script_workflow_context(content, db)
    if context is None:
        return None

    script_source = normalize_script_source(
        context.config.script_source if context.config else "reddit"
    )
    if script_source == "ai_generated":
        if not _ensure_ai_story_expanded(content, context, db):
            return None
        # _ensure_ai_story_expanded() just rewrote content.source_excerpt in
        # place (premise -> full expanded body). Any Story the caller passed
        # in was necessarily built before expansion (test_full_pipeline.py's
        # fresh-discovery path captures Story from run_discovery(), which for
        # this script_source only ever holds the short discovery-time
        # premise) and would silently ground the blueprint/sections on that
        # stale premise instead of the real story. Always rebuild from the
        # just-expanded content row for this path — never trust a pre-
        # expansion story argument here.
        story = _build_story(content)

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


def _ensure_ai_story_expanded(
    content: Content,
    context: ScriptWorkflowContext,
    db: Session,
) -> bool:
    """Expand an ``ai_generated`` content's premise into a full story body.

    Idempotent: a no-op when ``content.source_excerpt`` already clears the
    source-material floor (e.g. a stuck-state-recovery re-entry that already
    expanded it on a prior attempt) — checked with the same
    ``check_source_material_floor()`` the caller runs immediately after this
    function returns, so both checks agree on "clears the floor" using one
    shared implementation rather than two that could drift.

    Otherwise calls ``expand_story_premise()`` and persists the result in
    place to ``content.source_excerpt`` (committing immediately, matching
    this module's per-step commit convention). Returns ``False`` (and sets
    ``content.status = "FAILED"``) only on an outright expansion failure —
    per the Elimination Mandate, a still-too-short-but-real expansion is
    deliberately left for the ``_passes_source_material_floor()`` call right
    after this one to catch, exactly like it already does for a too-thin
    Reddit candidate, rather than being re-rolled here.
    """
    already_sufficient = not check_source_material_floor(
        content.source_excerpt or "", content.source_language, context.script_format,
    )
    if already_sufficient:
        logger.info(
            "AI_STORY_EXPANSION_SKIPPED content=%s reason=already_clears_floor",
            content.id,
        )
        return True

    premise = content.source_excerpt or ""
    expanded = expand_story_premise(
        premise=premise,
        channel=context.channel,
        script_format=context.script_format,
        language=content.source_language,
    )
    if expanded is None:
        logger.error(
            "AI_STORY_EXPANSION_FAILED content=%s — expand_story_premise returned no result",
            content.id,
        )
        content.status = "FAILED"
        db.commit()
        return False

    content.source_excerpt = expanded[:MAX_SOURCE_EXCERPT_CHARS]
    db.commit()
    logger.info(
        "AI_STORY_EXPANSION_DONE content=%s expanded_words=%d",
        content.id, len(expanded.split()),
    )
    return True


def run_script_workflow(content: Content, db: Session) -> None:
    """Generate, validate, persist, and plan scripts for approved content.

    Branches on ``Content.is_short_episode`` at the top: a Solo Short (a
    short episode with no parent — ``output_mode="shorts_only"``, see
    code_report/output_mode_shorts_only_and_youtube_long_only_roadmap.md) is
    itself the primary content unit and follows an entirely different
    script-generation sequence (``_run_solo_short_script_workflow()`` —
    written directly from source material, no blueprint->sections->
    quality-gate long-form pipeline). Everything below this branch is the
    unchanged long-form parent path, including the ``run_shorts_planner()``
    call at the end — a Solo Short never reaches it, since this branch
    returns before that point.
    """
    ensure_run_dirs(content.id)

    if content.is_short_episode:
        return _run_solo_short_script_workflow(content, db)

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


def _run_solo_short_script_workflow(content: Content, db: Session) -> None:
    """Generate, validate, and persist scripts for a Solo Short — a short
    episode with no parent (``is_short_episode=True``, ``parent_content_id``
    ``None``; ``output_mode="shorts_only"``, see code_report/output_mode_
    shorts_only_and_youtube_long_only_roadmap.md).

    Modeled on ``generate_parent_source_script()`` (blueprint generation,
    the source-material floor, ``ai_generated`` expansion — all reused
    verbatim) but with the long-form section-generation/quality-gate middle
    swapped for a single ``generate_solo_short_script()`` call: there is no
    parent script to derive Shorts from, so this content's own script IS the
    final output, written directly from the discovered/generated source.

    Never calls ``run_shorts_planner()`` — a Solo Short must never spawn its
    own child Shorts (Finding B; also independently guarded inside
    ``run_shorts_planner()`` itself).
    """
    context = _load_script_workflow_context(content, db)
    if context is None:
        return

    # Every source-material-floor/expansion helper below reads
    # context.script_format — substituting it once here (a Solo Short needs
    # the ~420-word floor, not the channel's long-form ~900-word one) means
    # every reused helper gets the right value with no parameter threading.
    context = replace(context, script_format=SOLO_SHORT_SCRIPT_FORMAT)

    script_source = normalize_script_source(
        context.config.script_source if context.config else "reddit"
    )
    if script_source == "ai_generated":
        if not _ensure_ai_story_expanded(content, context, db):
            return

    if not _passes_source_material_floor(content, context, db):
        return

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
        "SOLO_SHORT_SCRIPT_START content=%s — blueprint generated (%d major_turns)",
        content.id, len(blueprint.get("major_turns", [])),
    )
    content.story_blueprint = blueprint
    db.commit()

    draft = generate_solo_short_script(
        story, blueprint, context.channel, context.source_voice,
        visual_style=context.visual_style,
        image_style=context.image_style,
        narration_pov=context.narration_pov,
    )

    # Calibrated per (channel, language) from real completed Shorts once
    # enough exist (script_estimator.compute_measured_wpm(),
    # _MIN_SAMPLES_FOR_CALIBRATION=3); falls back to the static
    # (_MIN_SHORT_WORDS, _MAX_SHORT_WORDS) below that. See
    # _resolve_short_word_bounds() docstring — a real production run showed
    # the static 175 wpm assumption doesn't hold for every configured voice,
    # which let a 270-word-capped Short still land at ~114s instead of ~92s.
    min_words, max_words = _resolve_short_word_bounds(
        db, content.source_language, content.channel_id,
    )

    # Word-floor regeneration (operator-approved Elimination Mandate
    # exception, 2026-07-16) — mirrors _generate_short_script()'s single-
    # retry pattern in scripts.py verbatim: a deterministic count-triggered
    # regeneration, never an AI quality judgment, and never more than once.
    word_count = len((draft.get("voice_script") or "").split())
    if word_count < min_words:
        logger.warning(
            "SOLO_SHORT_WORD_FLOOR_REGEN content=%s words=%d floor=%d — regenerating once",
            content.id, word_count, min_words,
        )
        retry_draft = generate_solo_short_script(
            story, blueprint, context.channel, context.source_voice,
            visual_style=context.visual_style,
            image_style=context.image_style,
            narration_pov=context.narration_pov,
            word_floor_note=(
                f"Your previous draft was only {word_count} words — well under the "
                f"required {min_words}-word minimum. Write a longer, more "
                f"complete script this time, at least 210 words."
            ),
        )
        retry_words = len((retry_draft.get("voice_script") or "").split())
        if retry_words > word_count:
            draft, word_count = retry_draft, retry_words
        if word_count < min_words:
            logger.warning(
                "SOLO_SHORT_SCRIPT_STILL_UNDER_FLOOR content=%s words=%d floor=%d — "
                "proceeding with the longer draft; Agent 3's 61s audio gate is the "
                "final enforcement",
                content.id, word_count, min_words,
            )

    # B2: objective, single-call ceiling regeneration. This is the exact
    # inverse of the floor path above: no judgment loop, and the shorter of
    # the two drafts is retained deterministically.
    if word_count > max_words * 1.10:
        logger.warning(
            "SOLO_SHORT_WORD_CEILING_REGEN content=%s words=%d cap=%d — regenerating once",
            content.id, word_count, max_words,
        )
        ceiling_target = max(min_words, max_words - _CEILING_REGEN_TARGET_MARGIN)
        retry_draft = generate_solo_short_script(
            story, blueprint, context.channel, context.source_voice,
            visual_style=context.visual_style,
            image_style=context.image_style,
            narration_pov=context.narration_pov,
            word_ceiling_note=(
                f"Your previous draft was {word_count} words — above the hard "
                f"{max_words}-word ceiling. Rewrite it to at most {ceiling_target} "
                f"words, not just under {max_words} — a draft that lands right at "
                f"the ceiling has no margin left and risks tipping back over it "
                f"after ordinary pacing variance, pushing this Short past its "
                f"target watch time. Preserve the hook, reveal, and payoff."
            ),
        )
        retry_words = len((retry_draft.get("voice_script") or "").split())
        if retry_words < word_count:
            draft, word_count = retry_draft, retry_words

    # Structural checks — telemetry only (Elimination Mandate); the same
    # function run_shorts_planner()'s per-part path already uses. No parent
    # script exists to run detect_parent_child_overlap() against — skipped
    # entirely, same as the existing PARENT_CHILD_OVERLAP_SKIPPED no-op path.
    issues = _collect_short_script_major_issues(
        draft.get("voice_script") or "", content.source_language,
        part_n=1, correction_round=1, caller_label="solo_short",
    )
    if issues:
        logger.warning(
            "SOLO_SHORT_SCRIPT_ISSUES content=%s issues=%s — telemetry only, no retry",
            content.id, [i["category"] for i in issues],
        )

    src_voice_script = _persist_source_script(content, draft, db)
    logger.info(
        "SOLO_SHORT_SCRIPT_DONE content=%s words=%d", content.id, word_count,
    )

    _script_trace("solo_short_entering_multilingual", src_voice_script)
    required_scripts = generate_multilingual_scripts(
        content,
        context.channel,
        db,
        audio_tags_enabled=context.audio_tags_enabled,
    )
    if not required_scripts:
        logger.error(
            "Content %s Solo Short script workflow stopped before SCRIPTS_VALIDATED — "
            "script set incomplete",
            content.id,
        )
        return
    _set_multilingual_durations(content, db)

    content.status = "SCRIPTS_VALIDATED"
    db.commit()
    logger.info("Content %s — SCRIPTS_VALIDATED (Solo Short)", content.id)


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
    narration_pov = normalize_narration_pov(
        getattr(config, "narration_pov", None), channel_id=channel.id,
    )

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
    src_dur_sec = estimate_duration_sec(
        src_voice_script, content.source_language, db=db,
        is_short_episode=bool(getattr(content, "is_short_episode", False)),
        channel_id=content.channel_id,
    )
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
        dur = estimate_duration_sec(
            script.voice_script, script.language, db=db,
            is_short_episode=bool(getattr(content, "is_short_episode", False)),
            channel_id=content.channel_id,
        )
        script.estimated_duration_sec = dur
        script.validated = True
        logger.info(
            "Duration set for lang=%s content %s: %.1fs",
            script.language,
            content.id,
            dur,
        )
    db.commit()
