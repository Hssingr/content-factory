import hashlib
import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelVoice, Content, ContentValidation, Script
from app.services.script_estimator import estimate_duration_sec
from app.agents.agent2_discovery.system_prompt import (
    assess_script_quality,
    auto_correct_script,
    generate_native_script,
    rewrite_script_for_quality,
    _extract_hook_context,
    generate_story_blueprint,
    generate_section,
    validate_script_globally,
    generate_shorts_plan,
    generate_short_episode_script,
)
from app.services.script_checks import (
    check_hook_quality,
    check_tts_compliance,
    check_completeness,
    check_minimum_length,
    check_section_transition,
    check_sentence_rhythm_variance,
    split_long_sentences,
    normalize_tts_chars,
    detect_generic_documentary_phrases,
)

logger = logging.getLogger(__name__)

_MAX_QUALITY_REWRITES = 2
_MIN_BODY_SECTIONS    = 2
_MAX_BODY_SECTIONS    = 7   # V2 hard cap — absolute maximum regardless of covered turns
_MAX_SECTION_RETRIES  = 2


def _script_trace(label: str, voice_script: str) -> None:
    """Log word count, section count, and SHA-256 fingerprint for script version tracing.

    Temporary diagnostic — call at every major stage where the script is passed between
    functions to verify that the latest version (not a stale copy) is in use.
    """
    wc  = len(voice_script.split())
    sec = len(re.findall(
        r"^\s*\[\s*(?:INTRO|OUTRO|SECTION[^\]]*)\]",
        voice_script,
        re.MULTILINE | re.IGNORECASE,
    ))
    h = hashlib.sha256(voice_script.encode("utf-8", errors="replace")).hexdigest()[:8]
    logger.debug("SCRIPT_TRACE [%s] words=%d sections=%d sha256=%s", label, wc, sec, h)


def _max_sentence_len(text: str) -> int:
    """Return the word count of the longest sentence in text."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return max((len(s.split()) for s in sentences), default=0)


def _count_sentences(text: str) -> int:
    """Return the number of sentences in text."""
    return len([s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()])


def diagnose_section_repetition(sections: list[dict]) -> list[dict]:
    """Compare each section against all prior sections for content token overlap.

    Uses content-token overlap (words > 3 chars). Severity: HIGH ≥ 0.40,
    MEDIUM ≥ 0.25, LOW < 0.25. Pure Python, no API calls, non-blocking.
    Results are logged at INFO. Callers should not block the pipeline on this.

    Args:
        sections: List of {"label": str, "script_text": str} dicts in generation order.

    Returns:
        List of {label, max_overlap, vs_label, repeated_tokens, severity} dicts.
    """
    results: list[dict] = []
    for i, section in enumerate(sections):
        label  = section.get("label", f"SECTION {i}")
        text   = section.get("script_text", "")
        tokens = _get_content_tokens(text)

        if i == 0 or not tokens:
            results.append({
                "label": label, "max_overlap": 0.0, "vs_label": "",
                "repeated_tokens": [], "severity": "LOW",
            })
            continue

        max_overlap: float     = 0.0
        vs_label: str          = ""
        repeated: list[str]    = []

        for j, prior in enumerate(sections[:i]):
            prior_tokens = _get_content_tokens(prior.get("script_text", ""))
            if not prior_tokens:
                continue
            shared  = tokens & prior_tokens
            overlap = len(shared) / len(tokens)
            if overlap > max_overlap:
                max_overlap = overlap
                vs_label    = prior.get("label", f"SECTION {j}")
                repeated    = sorted(shared)

        severity = (
            "HIGH"   if max_overlap >= 0.40 else
            "MEDIUM" if max_overlap >= 0.25 else
            "LOW"
        )
        results.append({
            "label":           label,
            "max_overlap":     round(max_overlap, 3),
            "vs_label":        vs_label,
            "repeated_tokens": repeated[:10],
            "severity":        severity,
        })
        if severity == "HIGH":
            logger.debug(
                "REPETITION[HIGH] label=%s overlap=%.3f vs=%r repeated=%s — "
                "section repeats prior material (non-blocking)",
                label, max_overlap, vs_label, repeated[:6],
            )
        else:
            logger.debug(
                "REPETITION label=%s severity=%s overlap=%.3f vs=%r repeated=%s",
                label, severity, max_overlap, vs_label, repeated[:6],
            )

    return results


def _emit_script_cost_estimate(scripts: dict, rewrite_calls: int) -> None:
    """Log a rough cost estimate for the current script generation pass."""
    section_calls = scripts.get("_section_calls", 0)
    retry_calls   = scripts.get("_retry_calls",   0)
    est_in  = section_calls * 1800 + retry_calls * 2000 + rewrite_calls * 5500
    est_out = section_calls *  600 + retry_calls *  600 + rewrite_calls * 3000
    logger.info(
        "SCRIPT_COST_ESTIMATE section_calls=%d retry_calls=%d rewrite_calls=%d "
        "estimated_input_tokens=%d estimated_output_tokens=%d",
        section_calls, retry_calls, rewrite_calls, est_in, est_out,
    )


def run_script_quality_gate(
    scripts: dict,
    channel: Channel,
    content: Content,
    db: Session,
    blueprint: dict,
    script_format: str = "youtube_long",
    language: str = "source",
    tts_model: str = "sonic-2",
    tts_provider: str = "cartesia",
) -> dict:
    """Run the Script Quality Gate — assess retention quality, rewrite if needed.

    Also runs global narrative-coherence validation once (attempt 1 only) and
    folds its issues into the same rewrite mechanism (Phase 10A-0) — see
    ``_run_global_script_validation``.
    """
    current = _apply_final_tts_backstop(scripts)
    rewrite_calls = 0
    global_issues = _run_global_script_validation(
        current.get("voice_script", ""), blueprint, content, db
    )

    for attempt in range(1, _MAX_QUALITY_REWRITES + 1):
        _script_trace(f"quality_gate_input_{attempt}", current.get("voice_script", ""))
        _log_quality_gate_input(current, attempt)

        try:
            review = assess_script_quality(current, channel, script_format=script_format)
        except Exception as exc:
            logger.error(
                "Script Quality Gate assessment failed (attempt %d): %s — keeping script as-is",
                attempt, exc,
            )
            _emit_script_cost_estimate(scripts, rewrite_calls)
            return current

        issue_group = _collect_quality_gate_issues(
            review=review,
            current=current,
            language=language,
            extra_issues=global_issues if attempt == 1 else [],
        )
        _log_quality_gate_review(issue_group, attempt)

        if (
            issue_group["status"] == "PASSED"
            and not issue_group["converted_det"]
            and not issue_group["global"]
        ):
            _script_trace(f"quality_gate_passed_attempt_{attempt}", current.get("voice_script", ""))
            _emit_script_cost_estimate(scripts, rewrite_calls)
            return current

        if _has_tts_only_high_issues(issue_group["all_issues"]):
            current = _apply_tts_only_quality_cleanup(
                current=current,
                issue_group=issue_group,
                attempt=attempt,
            )
            continue

        try:
            current = rewrite_script_for_quality(
                current, issue_group["all_issues"], channel,
                script_format=script_format,
                tts_model=tts_model,
                tts_provider=tts_provider,
            )
            rewrite_calls += 1
            logger.info("QUALITY_REWRITE_SCHEMA_OK attempt=%d", attempt)
        except Exception as exc:
            logger.error(
                "QUALITY_REWRITE_JSON_FAIL attempt=%d error=%s — keeping prior script",
                attempt, exc,
            )
            _script_trace(f"quality_gate_rewrite_failed_{attempt}", current.get("voice_script", ""))
            _emit_script_cost_estimate(scripts, rewrite_calls)
            return current

        current = _apply_post_rewrite_cleanup(current, attempt)
        _script_trace(f"quality_gate_after_rewrite_{attempt}", current.get("voice_script", ""))

    logger.warning(
        "Script Quality Gate: still NEEDS_REWRITE after %d attempt(s) — proceeding with latest version",
        _MAX_QUALITY_REWRITES,
    )
    current = _apply_final_quality_cleanup(current)
    _script_trace("quality_gate_max_retries_return", current.get("voice_script", ""))
    _emit_script_cost_estimate(scripts, rewrite_calls)
    return current


def _apply_final_tts_backstop(current: dict) -> dict:
    voice_script = current.get("voice_script", "")
    over_before = sum(
        1 for sentence in re.split(r"(?<=[.!?])\s+", voice_script)
        if len(sentence.split()) > 18
    )
    cleaned = split_long_sentences(normalize_tts_chars(voice_script))
    over_after = sum(
        1 for sentence in re.split(r"(?<=[.!?])\s+", cleaned)
        if len(sentence.split()) > 18
    )
    logger.debug(
        "FINAL_TTS_BACKSTOP sentences_over_limit_before=%d sentences_over_limit_after=%d",
        over_before, over_after,
    )
    if cleaned != voice_script:
        return {**current, "voice_script": cleaned}
    return current


def _log_quality_gate_input(current: dict, attempt: int) -> None:
    voice_script = current.get("voice_script", "")
    intro_match = re.search(
        r"\[INTRO\]\s*(.*?)(?=\n\s*\[|\Z)",
        voice_script,
        re.DOTALL | re.IGNORECASE,
    )
    if intro_match:
        intro_sents = [
            sentence for sentence in re.split(r"(?<=[.!?])\s+", intro_match.group(1).strip())
            if sentence.strip()
        ]
        logger.debug(
            "QUALITY_GATE_INPUT attempt=%d intro_first=%r",
            attempt, (intro_sents[0][:120] if intro_sents else ""),
        )
    outro_match = re.search(r"\[OUTRO\]\s*(.*?)$", voice_script, re.DOTALL | re.IGNORECASE)
    if outro_match:
        outro_sents = [
            sentence for sentence in re.split(r"(?<=[.!?])\s+", outro_match.group(1).strip())
            if sentence.strip()
        ]
        logger.debug(
            "QUALITY_GATE_INPUT attempt=%d outro_last=%r",
            attempt, (outro_sents[-1][:120] if outro_sents else ""),
        )


def _collect_quality_gate_issues(
    review: dict,
    current: dict,
    language: str,
    extra_issues: list[dict] | None = None,
) -> dict:
    status = review.get("status", "PASSED")
    claude_issues: list[dict] = review.get("issues", [])
    voice_script = current.get("voice_script", "")
    tts_det = check_tts_compliance(voice_script, language)
    hook_det = check_hook_quality(voice_script, language)
    det_majors = [issue for issue in tts_det + hook_det if issue["severity"] == "MAJOR"]
    converted_det: list[dict] = [
        {
            "severity": "HIGH",
            "category": issue["category"],
            "description": issue["description"],
            "fix": issue["suggestion"],
        }
        for issue in det_majors
    ]
    global_issues: list[dict] = list(extra_issues or [])
    all_issues = claude_issues + converted_det + global_issues
    return {
        "status": status,
        "tts_det": tts_det,
        "hook_det": hook_det,
        "converted_det": converted_det,
        "global": global_issues,
        "all_issues": all_issues,
    }


def _log_quality_gate_review(issue_group: dict, attempt: int) -> None:
    all_issues = issue_group["all_issues"]
    high = sum(1 for issue in all_issues if issue.get("severity") == "HIGH")
    logger.info(
        "Script Quality Gate: claude=%s det_major=%d issues=%d (high=%d) attempt=%d",
        issue_group["status"], len(issue_group["converted_det"]), len(all_issues), high, attempt,
    )
    tts_major_count = len([
        issue for issue in issue_group["tts_det"] if issue["severity"] == "MAJOR"
    ])
    hook_major_count = len([
        issue for issue in issue_group["hook_det"] if issue["severity"] == "MAJOR"
    ])
    logger.debug(
        "QUALITY_GATE_BREAKDOWN attempt=%d det_tts_maj=%d det_hook_maj=%d",
        attempt, tts_major_count, hook_major_count,
    )
    for issue in all_issues:
        logger.debug(
            "Script quality issue [%s/%s]: %s -> %s",
            issue.get("severity", "?"), issue.get("category", "?"),
            issue.get("description", ""), issue.get("fix", ""),
        )


def _has_tts_only_high_issues(all_issues: list[dict]) -> bool:
    high_issues = [issue for issue in all_issues if issue.get("severity") == "HIGH"]
    return bool(high_issues) and all(
        issue.get("category") == "tts_compliance" for issue in high_issues
    )


def _apply_tts_only_quality_cleanup(
    current: dict,
    issue_group: dict,
    attempt: int,
) -> dict:
    high_issues = [issue for issue in issue_group["all_issues"] if issue.get("severity") == "HIGH"]
    logger.info(
        "QUALITY_REWRITE_SKIPPED reason=TTS_ONLY high_count=%d attempt=%d",
        len(high_issues), attempt,
    )
    voice_script = current.get("voice_script", "")
    cleaned = split_long_sentences(normalize_tts_chars(voice_script))
    if cleaned != voice_script:
        current = {**current, "voice_script": cleaned}
        logger.info("QUALITY_REWRITE_SKIPPED: deterministic cleanup applied")
    _script_trace(f"quality_gate_tts_only_cleanup_{attempt}", current.get("voice_script", ""))
    return current


def _apply_post_rewrite_cleanup(current: dict, attempt: int) -> dict:
    voice_script = current.get("voice_script", "")
    cleaned = split_long_sentences(normalize_tts_chars(voice_script))
    if cleaned != voice_script:
        current = {**current, "voice_script": cleaned}
        logger.info(
            "Script Quality Gate: deterministic cleanup applied after rewrite (attempt %d)",
            attempt,
        )
    return current


def _apply_final_quality_cleanup(current: dict) -> dict:
    voice_script = current.get("voice_script", "")
    cleaned = split_long_sentences(normalize_tts_chars(voice_script))
    if cleaned != voice_script:
        current = {**current, "voice_script": cleaned}
        logger.info("Script Quality Gate: final deterministic cleanup applied before returning")
    return current


def generate_multilingual_scripts(
    content: Content,
    channel: Channel,
    db: Session,
    audio_tags_enabled: bool = False,
) -> list[Script]:
    """Generate and validate the complete required script set for a content row.

    The validated source-language script must already exist in the DB. For each
    configured channel language that differs from the source, ``generate_native_script()``
    is called when a validated script does not already exist.

    This function does not write a terminal script status. The caller owns the
    final ``SCRIPTS_VALIDATED`` transition after the complete required script set
    exists and every required script row is validated.

    Args:
        content:  Content ORM object currently in Agent 2 script generation.
        channel:  Channel ORM object (provides ``niche`` and ``tone``).
        db:       SQLAlchemy session managed by the caller.

    Returns:
        The complete required validated ``Script`` set. Returns an empty list
        and sets ``status="FAILED"`` if the source script is missing or any
        required configured language could not be generated.
    """
    if content.status != "GENERATING_SCRIPTS":
        content.status = "GENERATING_SCRIPTS"
        db.commit()

    # ── Load source script ────────────────────────────────────────────────────
    source_script: Script | None = (
        db.query(Script)
        .filter(
            Script.content_id == content.id,
            Script.language == content.source_language,
            Script.validated.is_(True),
        )
        .order_by(Script.version.desc())
        .first()
    )

    if not source_script:
        logger.error(
            "No validated source script found for content %s (language=%s) — cannot generate multilingual",
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

    # ── Build voice map: language → ChannelVoice (for tts_model + provider) ──
    voice_map: dict[str, ChannelVoice] = {
        v.language: v
        for v in db.query(ChannelVoice).filter(ChannelVoice.channel_id == channel.id).all()
    }

    # ── Extract hook context from the source script ───────────────────────────
    hook_context = _extract_hook_context(source_script.voice_script, script_format)

    required_languages = _required_script_languages(content, channel, db)
    if required_languages == [content.source_language]:
        logger.warning(
            "Channel %s has no target languages configured — source script set is complete",
            channel.id,
        )
        _mark_script_validated(source_script)
        db.commit()
        return [source_script]

    # ── Detect which languages already have scripts (safe for retries) ────────
    existing_by_lang: dict[str, Script] = {
        script.language: script
        for script in db.query(Script)
        .filter(Script.content_id == content.id)
        .all()
    }

    # ── Generate per-language scripts ─────────────────────────────────────────
    result: list[Script] = []

    for lang in required_languages:
        if lang == content.source_language:
            result.append(source_script)
            continue

        if lang in existing_by_lang:
            existing = existing_by_lang[lang]
            _mark_script_validated(existing)
            result.append(existing)
            logger.debug("Script for lang=%s already exists — skipping", lang)
            continue

        lang_voice: ChannelVoice | None = voice_map.get(lang)
        lang_model    = lang_voice.tts_model if lang_voice else "sonic-2"
        lang_provider = lang_voice.provider if lang_voice else "cartesia"
        if not lang_voice:
            logger.info(
                "No ChannelVoice for lang=%s in channel %s — using cartesia/sonic-2",
                lang, channel.id,
            )

        logger.info("Generating %s script for content %s…", lang, content.id)
        try:
            adapted = generate_native_script(
                voice_script=source_script.voice_script,
                target_language=lang,
                niche=channel.niche,
                tone=channel.tone,
                script_format=script_format,
                audio_tags_enabled=audio_tags_enabled,
                tts_model=lang_model,
                tts_provider=lang_provider,
                hook_context=hook_context,
            )
        except Exception as exc:
            logger.error(
                "Native script generation failed (lang=%s, content=%s): %s",
                lang, content.id, exc,
            )
            continue

        script = Script(
            content_id=content.id,
            language=lang,
            voice_script=adapted["voice_script"],
            version=1,
            validated=True,
            estimated_duration_sec=estimate_duration_sec(adapted["voice_script"], lang),
        )
        db.add(script)
        db.flush()
        result.append(script)
        logger.debug("Script saved: lang=%s id=%s", lang, script.id)

    scripts_by_lang = {script.language: script for script in result}
    missing = [lang for lang in required_languages if lang not in scripts_by_lang]
    if missing:
        logger.error(
            "Multilingual script set incomplete for content %s — missing languages=%s",
            content.id,
            missing,
        )
        content.status = "FAILED"
        db.commit()
        return []

    for script in result:
        _mark_script_validated(script)
    db.commit()

    languages = [s.language for s in result]
    logger.info(
        "Multilingual scripts validated for content %s — %d language(s): %s",
        content.id, len(result), languages,
    )
    return result


def _required_script_languages(
    content: Content,
    channel: Channel,
    db: Session,
) -> list[str]:
    channel_langs: list[ChannelLanguage] = (
        db.query(ChannelLanguage)
        .filter(ChannelLanguage.channel_id == channel.id)
        .all()
    )
    target_codes = [cl.language for cl in channel_langs]
    ordered = [content.source_language]
    for lang in target_codes:
        if lang not in ordered:
            ordered.append(lang)
    return ordered


def _mark_script_validated(script: Script) -> None:
    if script.estimated_duration_sec is None:
        script.estimated_duration_sec = estimate_duration_sec(script.voice_script, script.language)
    script.validated = True


# ── Blueprint-first section generation ───────────────────────────────────────

def _get_content_tokens(text: str) -> set[str]:
    """Extract lowercased content tokens (>3 chars) from text for overlap scoring."""
    return {w.lower() for w in re.findall(r"\b\w+\b", text) if len(w) > 3}


def _match_turns(
    reveals: list[str],
    major_turns: list[str],
    script_text: str = "",
    label: str = "",
) -> set[int]:
    """Return indices of major_turns covered by this section.

    Two-stage matching (both use content tokens >3 chars):

    1. Primary — ``reveals`` vs. turn: ≥60% of the turn's tokens must appear
       in any single reveal string. This is the strict check; it fails if Claude
       populates ``reveals`` with abstract/vague phrases.

    2. Fallback — ``script_text`` vs. turn: ≥40% of the turn's tokens must
       appear anywhere in the section body. Activated only when the primary check
       missed this turn. Lower threshold is intentional — the section text is large
       so a 40% match on a 4-6 word turn is meaningful.

    Args:
        reveals:     List of reveal strings from Claude's section response.
        major_turns: Blueprint ``major_turns`` list.
        script_text: Full section body text — used as fallback when reveals are
                     empty or vocabulary-mismatched.
        label:       Section label for diagnostic logging.

    Returns:
        Set of integer indices into ``major_turns`` that are covered.
    """
    covered: set[int] = set()
    text_tokens = _get_content_tokens(script_text) if script_text else set()

    for i, turn in enumerate(major_turns):
        turn_tokens = _get_content_tokens(turn)
        if not turn_tokens:
            continue

        _best_reveal_score = 0.0

        # Stage 1 — reveals (60% strict)
        for reveal in reveals:
            reveal_tokens = _get_content_tokens(reveal)
            _score = len(turn_tokens & reveal_tokens) / len(turn_tokens)
            if _score > _best_reveal_score:
                _best_reveal_score = _score
            if _score >= 0.6:
                covered.add(i)
                logger.debug(
                    "TURN_MATCH label=%s turn[%d] score=%.2f source=reveal matched=True turn=%r",
                    label, i, _score, turn[:60],
                )
                break

        # Stage 2 — script_text fallback (40% loose)
        if i not in covered and text_tokens:
            _ft_score = len(turn_tokens & text_tokens) / len(turn_tokens)
            if _ft_score >= 0.4:
                covered.add(i)
                logger.debug(
                    "TURN_MATCH label=%s turn[%d] score=%.2f source=script_text matched=True turn=%r",
                    label, i, _ft_score, turn[:60],
                )
            else:
                logger.debug(
                    "TURN_MATCH label=%s turn[%d] best_reveal=%.2f fallback=%.2f matched=False turn=%r",
                    label, i, _best_reveal_score, _ft_score, turn[:60],
                )
        elif i not in covered:
            logger.debug(
                "TURN_MATCH label=%s turn[%d] best_reveal=%.2f fallback=N/A matched=False turn=%r",
                label, i, _best_reveal_score, turn[:60],
            )

    logger.debug("TURN_MATCH label=%s covered=%s/%d", label, sorted(covered), len(major_turns))
    return covered


def _payoff_reached(section_dict: dict, blueprint: dict) -> bool:
    """True when a section's reveals + script_text cover ≥50% of final_payoff tokens.

    Args:
        section_dict: A section result dict (script_text, reveals, …).
        blueprint:    Blueprint dict with ``final_payoff`` key.

    Returns:
        True if payoff overlap threshold is met.
    """
    payoff = blueprint.get("final_payoff", "")
    if not payoff:
        return False
    payoff_tokens = _get_content_tokens(payoff)
    if not payoff_tokens:
        return False
    all_text = " ".join(section_dict.get("reveals", []) + [section_dict.get("script_text", "")])
    section_tokens = _get_content_tokens(all_text)
    return len(payoff_tokens & section_tokens) / len(payoff_tokens) >= 0.5


def _update_accumulator(
    accumulator: dict,
    section_result: dict,
    history: list[dict],
    label: str,
) -> None:
    """Update visual_intent accumulator and history with the section's visual data."""
    vi = section_result.get("visual_intent") or {}
    avoid = vi.get("avoid_repeating") or []
    accumulator.setdefault("avoid_repeating", [])
    accumulator["avoid_repeating"].extend(avoid)
    history.append({
        "label": label,
        "section_goal":        vi.get("section_goal", ""),
        "primary_visual_focus": vi.get("primary_visual_focus", ""),
        "avoid_repeating":     avoid,
    })


def _log_section_retry_input(
    label: str,
    attempt: int,
    prior_sections_summary: list[dict],
    visual_intent_accumulator: dict,
    override: str,
) -> None:
    logger.debug(
        "SECTION_INPUT label=%s attempt=%d prior_count=%d avoid_count=%d override=%s",
        label, attempt, len(prior_sections_summary),
        len(visual_intent_accumulator.get("avoid_repeating", [])),
        bool(override),
    )
    for prior_section in prior_sections_summary[-3:]:
        logger.debug(
            "  SECTION_INPUT prior[%s] summary=%r reveals=%s open_q=%s",
            prior_section.get("label"),
            (prior_section.get("summary") or "")[:80],
            (prior_section.get("reveals") or [])[:3],
            (prior_section.get("open_questions") or [])[:2],
        )


def _call_section_generation(
    label: str,
    story,
    blueprint: dict,
    prior_sections_summary: list[dict],
    visual_intent_accumulator: dict,
    channel: Channel,
    script_format: str,
    tts_model: str,
    tts_provider: str,
    audio_tags_enabled: bool,
    override: str,
    primary_required_turn: str | None,
    future_uncovered_turns: list[str] | None,
    attempt: int,
) -> dict | None:
    try:
        return generate_section(
            label=label,
            story=story,
            blueprint=blueprint,
            prior_sections_summary=prior_sections_summary,
            visual_intent_accumulator=visual_intent_accumulator,
            channel=channel,
            script_format=script_format,
            tts_model=tts_model,
            tts_provider=tts_provider,
            audio_tags_enabled=audio_tags_enabled,
            override_instruction=override,
            primary_required_turn=primary_required_turn,
            future_uncovered_turns=future_uncovered_turns,
        )
    except Exception as exc:
        logger.error("Section %s generation error (attempt %d): %s", label, attempt, exc)
        return None


def _log_section_generation_output(label: str, attempt: int, result: dict) -> dict:
    script_text = result.get("script_text", "")
    metrics = {
        "word_count": len(script_text.split()),
        "sentence_count": _count_sentences(script_text),
        "max_sentence_len": _max_sentence_len(script_text),
    }
    first_sentence = (re.split(r"(?<=[.!?])\s+", script_text.strip()) or [""])[0][:120]
    logger.debug(
        "SECTION_OUTPUT label=%s attempt=%d words=%d sents=%d max_sent=%d suggests_outro=%s",
        label, attempt, metrics["word_count"], metrics["sentence_count"],
        metrics["max_sentence_len"], result.get("suggests_outro", False),
    )
    logger.debug("SECTION_OUTPUT label=%s first_sent=%r", label, first_sentence)
    logger.debug(
        "SECTION_OUTPUT label=%s summary=%r reveals=%s open_q=%s vi_goal=%r",
        label,
        (result.get("summary") or "")[:100],
        [(reveal or "")[:60] for reveal in (result.get("reveals") or [])[:3]],
        [(question or "")[:60] for question in (result.get("open_questions") or [])[:2]],
        ((result.get("visual_intent") or {}).get("section_goal") or "")[:80],
    )
    return metrics


def _clean_generated_section(result: dict) -> tuple[dict, str, bool]:
    script_text = result.get("script_text", "")
    cleaned = normalize_tts_chars(script_text)
    cleaned = split_long_sentences(cleaned)
    backstop_changed = cleaned != script_text
    if backstop_changed:
        result = {**result, "script_text": cleaned}
    return result, cleaned, backstop_changed


def _collect_section_retry_issues(
    script_text: str,
    check_hook: bool,
    prior_summary_text: str,
) -> dict:
    tts_issues = check_tts_compliance(script_text, "source")
    hook_issues = check_hook_quality(script_text, "source") if check_hook else []
    transition_issues = (
        check_section_transition(script_text, prior_summary_text)
        if prior_summary_text else []
    )
    rhythm_issues = check_sentence_rhythm_variance(script_text, "source")
    majors = [issue for issue in tts_issues + hook_issues if issue["severity"] == "MAJOR"]
    return {
        "tts_issues": tts_issues,
        "hook_issues": hook_issues,
        "transition_issues": transition_issues,
        "rhythm_issues": rhythm_issues,
        "majors": majors,
    }


def _log_section_cleanup(
    label: str,
    attempt: int,
    backstop_changed: bool,
    raw_metrics: dict,
    script_text: str,
    issue_group: dict,
) -> None:
    tts_major_count = len([
        issue for issue in issue_group["tts_issues"] if issue["severity"] == "MAJOR"
    ])
    logger.debug(
        "SECTION_CLEANUP label=%s attempt=%d backstop=%s words=%d→%d "
        "max_sent=%d→%d tts_maj=%d total_maj=%d",
        label, attempt, backstop_changed,
        raw_metrics["word_count"], len(script_text.split()),
        raw_metrics["max_sentence_len"], _max_sentence_len(script_text),
        tts_major_count, len(issue_group["majors"]),
    )


def _log_section_transition_issues(label: str, transition_issues: list[dict]) -> None:
    for transition_issue in transition_issues:
        logger.info(
            "Section %s transition check [MINOR]: %s",
            label,
            transition_issue["description"],
        )


def _log_section_rhythm_issues(label: str, rhythm_issues: list[dict]) -> None:
    for rhythm_issue in rhythm_issues:
        logger.info(
            "Section %s rhythm check [MINOR]: %s",
            label,
            rhythm_issue["description"],
        )


def _finalize_section_after_retry_limit(
    label: str,
    result: dict,
    script_text: str,
    check_hook: bool,
) -> dict:
    cleaned = normalize_tts_chars(script_text)
    cleaned = split_long_sentences(cleaned)
    if cleaned != script_text:
        script_text = cleaned
        result = {**result, "script_text": script_text}

    final_majors = [
        issue for issue in
        check_tts_compliance(script_text, "source")
        + (check_hook_quality(script_text, "source") if check_hook else [])
        if issue["severity"] == "MAJOR"
    ]
    if final_majors:
        logger.warning(
            "Section %s: proceeding with %d known TTS MAJOR issue(s) after "
            "final deterministic cleanup — %s",
            label, len(final_majors),
            [f"{issue['category']}: {(issue.get('offending_text') or '')[:50]}"
             for issue in final_majors],
        )
    else:
        logger.info(
            "Section %s: final deterministic cleanup resolved all MAJOR issues",
            label,
        )
    return result


def _build_section_retry_instruction(
    majors: list[dict],
    transition_issues: list[dict],
    rhythm_issues: list[dict],
) -> str:
    feedback_parts = [issue["description"] for issue in majors[:3]]
    if transition_issues:
        feedback_parts.append(transition_issues[0]["description"])
    if rhythm_issues:
        feedback_parts.append(rhythm_issues[0]["description"])
    return f"Fix these issues from the previous attempt: {'; '.join(feedback_parts)}"


def _generate_section_with_retry(
    label: str,
    story,
    blueprint: dict,
    prior_sections_summary: list[dict],
    visual_intent_accumulator: dict,
    channel: Channel,
    script_format: str,
    tts_model: str,
    tts_provider: str,
    audio_tags_enabled: bool,
    check_hook: bool,
    prior_summary_text: str = "",
    primary_required_turn: str | None = None,
    future_uncovered_turns: list[str] | None = None,
) -> dict | None:
    """Generate a single section, retrying up to _MAX_SECTION_RETRIES on MAJOR violations."""
    override = ""
    for attempt in range(1, _MAX_SECTION_RETRIES + 2):
        _log_section_retry_input(
            label, attempt, prior_sections_summary, visual_intent_accumulator, override
        )
        result = _call_section_generation(
            label=label,
            story=story,
            blueprint=blueprint,
            prior_sections_summary=prior_sections_summary,
            visual_intent_accumulator=visual_intent_accumulator,
            channel=channel,
            script_format=script_format,
            tts_model=tts_model,
            tts_provider=tts_provider,
            audio_tags_enabled=audio_tags_enabled,
            override=override,
            primary_required_turn=primary_required_turn,
            future_uncovered_turns=future_uncovered_turns,
            attempt=attempt,
        )
        if result is None:
            if attempt > _MAX_SECTION_RETRIES:
                return None
            continue

        raw_metrics = _log_section_generation_output(label, attempt, result)
        result, script_text, backstop_changed = _clean_generated_section(result)
        issue_group = _collect_section_retry_issues(
            script_text=script_text,
            check_hook=check_hook,
            prior_summary_text=prior_summary_text,
        )
        _log_section_cleanup(
            label, attempt, backstop_changed, raw_metrics, script_text, issue_group
        )
        _log_section_transition_issues(label, issue_group["transition_issues"])
        _log_section_rhythm_issues(label, issue_group["rhythm_issues"])

        if not issue_group["majors"]:
            return result

        if attempt > _MAX_SECTION_RETRIES:
            return _finalize_section_after_retry_limit(
                label, result, script_text, check_hook
            )

        override = _build_section_retry_instruction(
            issue_group["majors"], issue_group["transition_issues"], issue_group["rhythm_issues"]
        )
        logger.info("Section %s retry %d — issues: %s", label, attempt, override)

    return None


def assemble_script(sections: list[dict]) -> str:
    """Assemble section dicts into a marked voice_script.

    Args:
        sections: List of dicts with keys ``label`` and ``script_text``, in order.

    Returns:
        The assembled voice_script text, with [LABEL] markers on their own line.
    """
    parts: list[str] = []
    for s in sections:
        parts.append(f"[{s['label']}]")
        parts.append(s["script_text"])
    return "\n\n".join(parts)


def check_narrative_completeness(
    voice_script: str,
    blueprint: dict,
    already_covered: "set[int] | None" = None,
) -> list[str]:
    """Pure Python completeness check against the story blueprint.

    Four checks (no API call):
    1. INTRO hook ≤15 words and no forbidden opener (via check_hook_quality).
    2. All major_turns covered across the full script (60% token overlap).
       Turns whose index is in ``already_covered`` are skipped — section
       progression already credited them via the two-stage reveal matching,
       which is more reliable than reassessing from the assembled text.
    3. final_payoff referenced in OUTRO (50% token overlap).
    4. comment_trigger present as last OUTRO sentence (50% token overlap).

    Args:
        voice_script:    Fully assembled voice script with [INTRO]/[SECTION N]/[OUTRO] markers.
        blueprint:       Blueprint dict from generate_story_blueprint().
        already_covered: Set of major_turn indices already credited by section progression.
                         Turns in this set are not re-checked here, eliminating false
                         "uncovered" signals caused by the two systems using different
                         token-matching methods.

    Returns:
        List of human-readable issue strings. Empty list = PASS.
    """
    issues: list[str] = []

    # ── 1. Hook quality ───────────────────────────────────────────────────────
    hook_issues = [i for i in check_hook_quality(voice_script, "source") if i["severity"] == "MAJOR"]
    if hook_issues:
        issues.append(f"Hook: {hook_issues[0]['description']}")

    # ── 2. Major turns covered ────────────────────────────────────────────────
    major_turns = blueprint.get("major_turns") or []
    body_tokens = _get_content_tokens(voice_script)
    uncovered: list[str] = []
    for i, turn in enumerate(major_turns):
        # Skip turns already credited by section progression (reveals-based matching).
        # Their text may not reach the 60% threshold in the assembled prose even though
        # they were genuinely covered — the two methods use different matching logic.
        if already_covered is not None and i in already_covered:
            continue
        turn_tokens = _get_content_tokens(turn)
        if not turn_tokens:
            continue
        overlap = len(turn_tokens & body_tokens) / len(turn_tokens)
        if overlap < 0.6:
            uncovered.append(f"turn[{i}]")
    if uncovered:
        issues.append(f"Major turns not sufficiently covered: {', '.join(uncovered)}")

    # ── 3. Final payoff in OUTRO ──────────────────────────────────────────────
    outro_match = re.search(r"\[OUTRO\](.*?)$", voice_script, re.DOTALL | re.IGNORECASE)
    outro_text  = outro_match.group(1).strip() if outro_match else ""

    payoff = blueprint.get("final_payoff", "")
    if payoff and outro_text:
        payoff_tokens = _get_content_tokens(payoff)
        outro_tokens  = _get_content_tokens(outro_text)
        if payoff_tokens:
            overlap = len(payoff_tokens & outro_tokens) / len(payoff_tokens)
            if overlap < 0.5:
                issues.append("final_payoff not adequately referenced in OUTRO")
    elif payoff and not outro_text:
        issues.append("OUTRO section missing from assembled script")

    # ── 4. Comment trigger as last OUTRO sentence ─────────────────────────────
    comment_trigger = blueprint.get("comment_trigger", "")
    if comment_trigger and outro_text:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", outro_text) if s.strip()]
        if sentences:
            last_sent     = sentences[-1]
            trigger_tokens = _get_content_tokens(comment_trigger)
            last_tokens    = _get_content_tokens(last_sent)
            if trigger_tokens:
                overlap = len(trigger_tokens & last_tokens) / len(trigger_tokens)
                if overlap < 0.5:
                    issues.append("comment_trigger not found as last sentence of OUTRO")

    return issues


def _build_section_generation_context(
    channel_voice: ChannelVoice | None,
    blueprint: dict,
) -> dict:
    major_turns = blueprint.get("major_turns") or []
    max_body = max(
        _MIN_BODY_SECTIONS,
        min(_MAX_BODY_SECTIONS, blueprint.get("suggested_section_count", 3)),
    )
    min_body_for_blueprint = (
        max(_MIN_BODY_SECTIONS, min(4, len(major_turns)))
        if len(major_turns) >= 4
        else _MIN_BODY_SECTIONS
    )
    return {
        "tts_model": channel_voice.tts_model if channel_voice else "sonic-2",
        "tts_provider": channel_voice.provider if channel_voice else "cartesia",
        "major_turns": major_turns,
        "max_body": max_body,
        "min_body_for_blueprint": min_body_for_blueprint,
    }


def _create_section_loop_state() -> dict:
    return {
        "visual_intent_accumulator": {"avoid_repeating": []},
        "prior_sections_summary": [],
        "sections": [],
        "visual_intent_history": [],
        "covered_turns": set(),
        "section_calls": 0,
        "narrative_retry_calls": 0,
    }


def _log_blueprint_summary(blueprint: dict, major_turns: list[str], max_body: int) -> None:
    logger.info(
        "BLUEPRINT hook=%r payoff=%r trigger=%r section_count=%d turns=%d max_body=%d",
        (blueprint.get("hook") or "")[:80],
        (blueprint.get("final_payoff") or "")[:60],
        (blueprint.get("comment_trigger") or "")[:60],
        blueprint.get("suggested_section_count", 0),
        len(major_turns),
        max_body,
    )
    for _ti, _turn in enumerate(major_turns):
        logger.info("BLUEPRINT turn[%d]=%r", _ti, _turn[:80])


def _append_generated_section(
    state: dict,
    label: str,
    section: dict,
    major_turns: list[str],
    add_prior_summary: bool = True,
    track_turns: bool = True,
) -> set[int]:
    state["section_calls"] += 1
    state["sections"].append({"label": label, "script_text": section["script_text"]})
    _update_accumulator(
        state["visual_intent_accumulator"],
        section,
        state["visual_intent_history"],
        label,
    )
    if add_prior_summary:
        state["prior_sections_summary"].append({
            "label": label,
            "summary": section.get("summary", ""),
            "reveals": section.get("reveals", []),
            "open_questions": section.get("open_questions", []),
        })
    if not track_turns:
        return set()
    return _match_turns(
        section.get("reveals", []),
        major_turns,
        section.get("script_text", ""),
        label=label,
    )


def _select_required_turns(
    major_turns: list[str],
    covered_turns: set[int],
) -> tuple[list[int], int | None, str | None, list[str]]:
    uncovered = [i for i in range(len(major_turns)) if i not in covered_turns]
    primary_idx = uncovered[0] if uncovered else None
    primary_turn = major_turns[primary_idx] if primary_idx is not None else None
    future_turns = [major_turns[i] for i in uncovered[1:]]
    return uncovered, primary_idx, primary_turn, future_turns


def _generate_intro_section(
    story,
    blueprint: dict,
    channel: Channel,
    script_format: str,
    audio_tags_enabled: bool,
    context: dict,
    state: dict,
) -> None:
    major_turns = context["major_turns"]
    _uncov = list(range(len(major_turns)))
    logger.debug("SECTION_INPUT label=INTRO sections_so_far=0 covered=[] uncovered=%s", _uncov)
    intro = _generate_section_with_retry(
        label="INTRO",
        story=story,
        blueprint=blueprint,
        prior_sections_summary=[],
        visual_intent_accumulator=state["visual_intent_accumulator"],
        channel=channel,
        script_format=script_format,
        tts_model=context["tts_model"],
        tts_provider=context["tts_provider"],
        audio_tags_enabled=audio_tags_enabled,
        check_hook=True,
        prior_summary_text="",
        primary_required_turn=None,
        future_uncovered_turns=None,
    )
    if intro is None:
        raise RuntimeError("generate_script_sections: INTRO generation failed after retries")
    state["covered_turns"] |= _append_generated_section(state, "INTRO", intro, major_turns)


def _credit_body_turn_coverage(
    covered_turns: set[int],
    matched_turns: set[int],
    label: str,
    primary_idx: int | None,
) -> None:
    if len(matched_turns) >= 3:
        logger.debug(
            "generate_script_sections: %s over-compressed major turns — "
            "matched %d turns %s, crediting only primary turn [%s]",
            label, len(matched_turns), sorted(matched_turns), primary_idx,
        )
        if primary_idx is not None:
            covered_turns.add(primary_idx)
        else:
            covered_turns |= matched_turns
    else:
        covered_turns |= matched_turns
        if primary_idx is not None:
            covered_turns.add(primary_idx)


def _should_stop_body_loop(
    body_index: int,
    section: dict,
    blueprint: dict,
    major_turns: list[str],
    covered_turns: set[int],
    max_body: int,
    min_body_for_blueprint: int,
) -> bool:
    all_turns_covered = len(covered_turns) >= len(major_turns)
    at_min = body_index > min_body_for_blueprint
    claude_suggests_outro = bool(section.get("suggests_outro", False))
    at_soft_max = body_index > max_body
    at_hard_max = body_index > _MAX_BODY_SECTIONS
    payoff_done = _payoff_reached(section, blueprint)

    logger.debug(
        "LOOP_DECISION body_index=%d covered=%d/%d all_covered=%s payoff=%s "
        "suggests_outro=%s at_min=%s at_soft=%s at_hard=%s min_body=%d",
        body_index, len(covered_turns), len(major_turns),
        all_turns_covered, payoff_done, claude_suggests_outro,
        at_min, at_soft_max, at_hard_max, min_body_for_blueprint,
    )

    if at_hard_max:
        if not all_turns_covered:
            logger.warning(
                "generate_script_sections: hard cap (%d body sections) reached with "
                "%d/%d major turns still uncovered — proceeding to OUTRO",
                _MAX_BODY_SECTIONS,
                len(major_turns) - len(covered_turns), len(major_turns),
            )
            logger.debug("LOOP_DECISION: break_hard_cap reason=uncovered_turns_remain")
        else:
            logger.warning(
                "generate_script_sections: ending body loop after %d section(s) "
                "(hard cap reached, all turns covered)",
                body_index - 1,
            )
            logger.debug("LOOP_DECISION: break_hard_cap reason=all_covered")
        return True

    if at_soft_max and not all_turns_covered:
        logger.warning(
            "generate_script_sections: soft max (%d) reached with %d/%d major turns "
            "uncovered — extending to hard cap (%d)",
            max_body,
            len(major_turns) - len(covered_turns), len(major_turns),
            _MAX_BODY_SECTIONS,
        )
        logger.debug("LOOP_DECISION: continue reason=soft_max_but_uncovered_turns")
        return False

    if all_turns_covered and at_min and (at_soft_max or claude_suggests_outro):
        logger.info(
            "generate_script_sections: ending body loop after %d section(s) "
            "(covered_turns=%d/%d, suggests_outro=%s, at_soft_max=%s)",
            body_index - 1, len(covered_turns), len(major_turns),
            claude_suggests_outro, at_soft_max,
        )
        logger.debug(
            "LOOP_DECISION: break_normal reason=all_covered+past_min+(%s)",
            "soft_max" if at_soft_max else "claude_suggests",
        )
        return True

    logger.debug(
        "LOOP_DECISION: continue reason=not_all_conditions_met "
        "(all_covered=%s at_min=%s at_soft=%s claude=%s)",
        all_turns_covered, at_min, at_soft_max, claude_suggests_outro,
    )
    return False


def _run_body_section_loop(
    story,
    blueprint: dict,
    channel: Channel,
    script_format: str,
    audio_tags_enabled: bool,
    context: dict,
    state: dict,
) -> None:
    body_index = 1
    major_turns = context["major_turns"]
    while True:
        label = f"SECTION {body_index}"
        uncovered, primary_idx, primary_turn, future_turns = _select_required_turns(
            major_turns, state["covered_turns"]
        )
        logger.debug(
            "SECTION_INPUT label=%s sections_so_far=%d covered=[%s] "
            "primary_turn_idx=%s uncovered=%s",
            label, len(state["prior_sections_summary"]),
            ",".join(str(i) for i in sorted(state["covered_turns"])),
            primary_idx, uncovered,
        )
        section = _generate_section_with_retry(
            label=label,
            story=story,
            blueprint=blueprint,
            prior_sections_summary=state["prior_sections_summary"],
            visual_intent_accumulator=state["visual_intent_accumulator"],
            channel=channel,
            script_format=script_format,
            tts_model=context["tts_model"],
            tts_provider=context["tts_provider"],
            audio_tags_enabled=audio_tags_enabled,
            check_hook=False,
            prior_summary_text=state["prior_sections_summary"][-1]["summary"] if state["prior_sections_summary"] else "",
            primary_required_turn=primary_turn,
            future_uncovered_turns=future_turns if future_turns else None,
        )
        if section is None:
            logger.warning(
                "generate_script_sections: %s failed after retries — stopping body loop", label
            )
            break

        matched_turns = _append_generated_section(state, label, section, major_turns)
        _credit_body_turn_coverage(state["covered_turns"], matched_turns, label, primary_idx)
        body_index += 1

        if _should_stop_body_loop(
            body_index=body_index,
            section=section,
            blueprint=blueprint,
            major_turns=major_turns,
            covered_turns=state["covered_turns"],
            max_body=context["max_body"],
            min_body_for_blueprint=context["min_body_for_blueprint"],
        ):
            break


def _generate_outro_section(
    story,
    blueprint: dict,
    channel: Channel,
    script_format: str,
    audio_tags_enabled: bool,
    context: dict,
    state: dict,
) -> None:
    major_turns = context["major_turns"]
    _uncov_outro = [i for i in range(len(major_turns)) if i not in state["covered_turns"]]
    logger.debug(
        "SECTION_INPUT label=OUTRO sections_so_far=%d covered=[%s] uncovered=%s",
        len(state["prior_sections_summary"]),
        ",".join(str(i) for i in sorted(state["covered_turns"])),
        _uncov_outro,
    )
    outro = _generate_section_with_retry(
        label="OUTRO",
        story=story,
        blueprint=blueprint,
        prior_sections_summary=state["prior_sections_summary"],
        visual_intent_accumulator=state["visual_intent_accumulator"],
        channel=channel,
        script_format=script_format,
        tts_model=context["tts_model"],
        tts_provider=context["tts_provider"],
        audio_tags_enabled=audio_tags_enabled,
        check_hook=False,
        prior_summary_text=state["prior_sections_summary"][-1]["summary"] if state["prior_sections_summary"] else "",
        primary_required_turn=None,
        future_uncovered_turns=None,
    )
    if outro is None:
        raise RuntimeError("generate_script_sections: OUTRO generation failed after retries")
    _append_generated_section(
        state, "OUTRO", outro, major_turns, add_prior_summary=False, track_turns=False
    )
    _log_outro_overlap(state["sections"], outro)


def _log_outro_overlap(sections: list[dict], outro: dict) -> None:
    _outro_text = outro["script_text"]
    _prev_body = [s for s in sections[:-1] if s["label"] not in ("INTRO", "OUTRO")]
    if _prev_body:
        _outro_tokens = _get_content_tokens(_outro_text)
        _prev_tokens = _get_content_tokens(_prev_body[-1]["script_text"])
        if _outro_tokens:
            _outro_ov = len(_outro_tokens & _prev_tokens) / len(_outro_tokens)
            _repeated = sorted(_outro_tokens & _prev_tokens)[:8]
            if _outro_ov > 0.5:
                logger.debug(
                    "OUTRO_OVERLAP previous_section_overlap=%.3f repeated_terms=%s "
                    "— OUTRO heavily repeats prior section (non-blocking)",
                    _outro_ov, _repeated,
                )
            else:
                logger.debug(
                    "OUTRO_OVERLAP previous_section_overlap=%.3f repeated_terms=%s",
                    _outro_ov, _repeated,
                )


def _assemble_sections_with_diagnostics(
    state: dict,
    story,
    blueprint: dict,
    channel: Channel,
    script_format: str,
    context: dict,
) -> str:
    sections = state["sections"]
    diagnose_section_repetition(sections)
    voice_script = assemble_script(sections)
    _script_trace("after_section_assembly", voice_script)

    _phrase_hits = detect_generic_documentary_phrases(voice_script)
    for _hit in _phrase_hits:
        logger.debug(
            "GENERIC_PHRASE detected=%r in sentence=%r — rewrite recommended (non-blocking)",
            _hit["phrase"], _hit["sentence"],
        )

    completeness_issues = check_completeness(voice_script, "source")
    length_issues = check_minimum_length(voice_script, "source", script_format)

    if completeness_issues:
        logger.warning(
            "generate_script_sections: post-assembly completeness issue(s) (telemetry): %s",
            [i.get("description") for i in completeness_issues],
        )

    length_majors = [i for i in length_issues if i.get("severity") == "MAJOR"]
    if length_majors:
        voice_script = _apply_length_correction(
            voice_script=voice_script,
            length_majors=length_majors,
            story=story,
            channel=channel,
            script_format=script_format,
            context=context,
        )

    return voice_script


def _apply_length_correction(
    voice_script: str,
    length_majors: list[dict],
    story,
    channel: Channel,
    script_format: str,
    context: dict,
) -> str:
    wc_before = len(voice_script.split())
    logger.warning(
        "generate_script_sections: voice_script under minimum length (%d words) — "
        "calling auto_correct_script once with source_excerpt",
        wc_before,
    )
    try:
        corrected = auto_correct_script(
            current_scripts={"voice_script": voice_script},
            issues=length_majors,
            language=story.language,
            channel=channel,
            script_format=script_format,
            source_excerpt=(story.body or "")[:8000],
            tts_model=context["tts_model"],
            tts_provider=context["tts_provider"],
        )
        voice_script = corrected.get("voice_script", voice_script)
        wc_after = len(voice_script.split())
        logger.info(
            "generate_script_sections: length correction applied — %d → %d words",
            wc_before, wc_after,
        )
    except Exception as exc:
        logger.debug(
            "generate_script_sections: length correction failed (non-blocking): %s", exc
        )
    return voice_script


def _run_global_script_validation(
    voice_script: str,
    blueprint: dict,
    content: Content,
    db: Session,
) -> list[dict]:
    """Run global narrative-coherence validation once and persist the result.

    Called once per quality-gate pass (attempt 1 only — see ``run_script_quality_gate``).
    Persists status + raw issues to ``ContentValidation`` (Phase 10A-0) so the
    finding survives past a log line, and returns the issues converted into the
    rewrite-issue shape (``severity``/``category``/``description``/``fix``) so they
    can be merged into the same ``rewrite_script_for_quality()`` call the quality
    gate already runs for Claude-judged and deterministic issues — no second,
    parallel rewrite mechanism.

    Non-blocking: a failed Claude call persists ``NEEDS_REVIEW`` and returns no
    issues rather than raising, matching this validator's pre-existing behavior.
    """
    validation = (
        db.query(ContentValidation)
        .filter(ContentValidation.content_id == content.id)
        .first()
    )
    try:
        gv = validate_script_globally(voice_script, blueprint)
    except Exception as exc:
        logger.debug("Global validation failed (non-blocking): %s", exc)
        if validation:
            validation.script_validation_status = "NEEDS_REVIEW"
            db.commit()
        return []

    raw_issues: list[dict] = gv.get("issues", [])
    if gv.get("status") == "NEEDS_FIX":
        for issue in raw_issues:
            logger.info(
                "Global validation [%s]: %s — %s",
                issue.get("section"), issue.get("description"), issue.get("suggestion"),
            )
        status = "AUTO_CORRECTED"
    else:
        status = "PASSED"

    if validation:
        validation.script_validation_status = status
        validation.script_issues_log = raw_issues
        db.commit()
    else:
        logger.warning(
            "_run_global_script_validation: no ContentValidation row for content %s — "
            "result not persisted",
            content.id,
        )

    return [
        {
            "severity": "HIGH",
            "category": "global_narrative",
            "description": issue.get("description", ""),
            "fix": issue.get("suggestion", ""),
        }
        for issue in raw_issues
    ]


def _log_turn_coverage_alignment(
    voice_script: str,
    major_turns: list[str],
    covered_turns: set[int],
) -> None:
    _vs_body_tokens = _get_content_tokens(voice_script)
    _nc_would_flag: list[int] = []
    for _i, _turn in enumerate(major_turns):
        _tt = _get_content_tokens(_turn)
        _ov = len(_tt & _vs_body_tokens) / len(_tt) if _tt else 0.0
        if _ov < 0.6:
            _nc_would_flag.append(_i)
    logger.debug(
        "TURN_COVERAGE_SOURCE section_progression=%s narrative_check=%s",
        sorted(covered_turns), _nc_would_flag,
    )
    _disagreement = covered_turns & set(_nc_would_flag)
    if _disagreement:
        logger.debug(
            "TURN_COVERAGE_DISAGREEMENT: section_progression credits turns %s but "
            "60%%-overlap check would flag them — section_progression is authoritative, "
            "these turns will be excluded from narrative retry",
            sorted(_disagreement),
        )

    logger.debug(
        "TURN_COVERAGE_FINAL authoritative=%s total=%d/%d",
        sorted(covered_turns), len(covered_turns), len(major_turns),
    )


def _group_narrative_retry_instructions(
    nc_issues: list[str],
    sections: list[dict],
) -> dict[str, list[str]]:
    issue_to_section: list[tuple[str, str | None]] = [
        ("Hook:", "INTRO"),
        ("Major turns", None),
        ("final_payoff", "OUTRO"),
        ("comment_trigger", "OUTRO"),
    ]
    body_labels = [s["label"] for s in sections if s["label"] not in ("INTRO", "OUTRO")]
    section_instructions: dict[str, list[str]] = {}
    for issue in nc_issues:
        target_label: str | None = None
        for prefix, lbl in issue_to_section:
            if issue.startswith(prefix):
                target_label = lbl
                break
        if target_label is None:
            target_label = body_labels[-1] if body_labels else "OUTRO"
        section_instructions.setdefault(target_label, []).append(issue)
    return section_instructions


def _run_single_narrative_retry(
    target_label: str,
    instructions: list[str],
    state: dict,
    story,
    blueprint: dict,
    channel: Channel,
    script_format: str,
    audio_tags_enabled: bool,
    context: dict,
) -> None:
    sections = state["sections"]
    major_turns = context["major_turns"]
    covered_turns = state["covered_turns"]
    label_to_idx: dict[str, int] = {s["label"]: i for i, s in enumerate(sections)}
    idx = label_to_idx.get(target_label)
    if idx is None:
        logger.warning(
            "generate_script_sections: narrative retry — section %r not found, skipping",
            target_label,
        )
        return

    combined = "; ".join(instructions)
    override = (
        f"The assembled script has these narrative completeness issues: {combined}. "
        f"Fix all of them in this section."
    )
    logger.info(
        "generate_script_sections: narrative retry for section %r — %s",
        target_label, combined,
    )
    logger.info(
        "NARRATIVE_COMPLETENESS target=%r issues=%s covered_before=%d/%d",
        target_label, instructions, len(covered_turns), len(major_turns),
    )
    state["narrative_retry_calls"] += 1
    _old_sha = hashlib.sha256(
        sections[idx]["script_text"].encode("utf-8", errors="replace")
    ).hexdigest()[:8]
    _covered_before_retry = len(covered_turns)
    try:
        prior_for_retry = [
            {"label": s["label"], "summary": "", "reveals": [], "open_questions": []}
            for s in sections[:idx]
        ]
        result = generate_section(
            label=target_label,
            story=story,
            blueprint=blueprint,
            prior_sections_summary=prior_for_retry,
            visual_intent_accumulator=state["visual_intent_accumulator"],
            channel=channel,
            script_format=script_format,
            tts_model=context["tts_model"],
            tts_provider=context["tts_provider"],
            audio_tags_enabled=audio_tags_enabled,
            override_instruction=override,
        )
        retry_text = _clean_narrative_retry_text(result.get("script_text", ""), target_label)
        sections[idx] = {"label": target_label, "script_text": retry_text}
        _new_sha = hashlib.sha256(
            retry_text.encode("utf-8", errors="replace")
        ).hexdigest()[:8]
        _new_first = (re.split(r"(?<=[.!?])\s+", retry_text.strip()) or [""])[0][:80]
        _retry_coverage = _match_turns(
            result.get("reveals", []), major_turns, retry_text,
            label=f"{target_label}_retry_check",
        )
        _covered_after_retry = len(covered_turns | _retry_coverage)
        logger.debug(
            "NARRATIVE_RETRY target=%r sha=%s→%s first_sent=%r",
            target_label, _old_sha, _new_sha, _new_first,
        )
        logger.debug(
            "NARRATIVE_RETRY target=%r new_reveals=%s turns_covered=%d→%d/%d",
            target_label,
            [(r or "")[:60] for r in (result.get("reveals") or [])[:3]],
            _covered_before_retry, _covered_after_retry, len(major_turns),
        )
        logger.info(
            "generate_script_sections: narrative retry replaced section %r", target_label
        )
    except Exception as exc:
        logger.warning(
            "generate_script_sections: narrative retry call failed for %r: %s — "
            "proceeding with original section",
            target_label, exc,
        )


def _clean_narrative_retry_text(retry_text: str, target_label: str) -> str:
    cleaned = split_long_sentences(normalize_tts_chars(retry_text))
    if cleaned != retry_text:
        retry_text = cleaned
        logger.info(
            "generate_script_sections: narrative retry backstop modified %r",
            target_label,
        )

    if target_label == "INTRO":
        _hook_after = [
            i for i in check_hook_quality(retry_text, "source")
            if i["severity"] == "MAJOR"
        ]
        if _hook_after:
            logger.warning(
                "generate_script_sections: INTRO narrative retry still has "
                "MAJOR hook issue(s) after backstop — %s",
                [i["description"] for i in _hook_after],
            )
    return retry_text


def _log_post_retry_narrative_result(
    voice_script: str,
    blueprint: dict,
    major_turns: list[str],
    covered_turns: set[int],
) -> None:
    nc_issues_after = check_narrative_completeness(
        voice_script, blueprint, already_covered=covered_turns
    )
    if nc_issues_after:
        logger.warning(
            "generate_script_sections: narrative completeness still failing after retry: %s",
            nc_issues_after,
        )
        _post_nc_body = _get_content_tokens(voice_script)
        for _i_post, _t_post in enumerate(major_turns):
            if _i_post in covered_turns:
                _tp = _get_content_tokens(_t_post)
                _ov_post = len(_tp & _post_nc_body) / len(_tp) if _tp else 0.0
                if _ov_post < 0.6:
                    logger.warning(
                        "TURN_COVERAGE_DISAGREEMENT_POST_RETRY turn[%d] overlap=%.2f "
                        "— section_progression is authoritative, ignoring",
                        _i_post, _ov_post,
                    )
    else:
        logger.info(
            "generate_script_sections: narrative completeness PASSED after retry"
        )


def _run_narrative_completeness_retry(
    voice_script: str,
    state: dict,
    story,
    blueprint: dict,
    channel: Channel,
    script_format: str,
    audio_tags_enabled: bool,
    context: dict,
) -> str:
    major_turns = context["major_turns"]
    covered_turns = state["covered_turns"]
    _log_turn_coverage_alignment(voice_script, major_turns, covered_turns)

    nc_issues = check_narrative_completeness(
        voice_script, blueprint, already_covered=covered_turns
    )
    if not nc_issues:
        return voice_script

    logger.info(
        "generate_script_sections: narrative completeness issues before retry: %s", nc_issues
    )
    section_instructions = _group_narrative_retry_instructions(nc_issues, state["sections"])
    for target_label, instructions in section_instructions.items():
        _run_single_narrative_retry(
            target_label=target_label,
            instructions=instructions,
            state=state,
            story=story,
            blueprint=blueprint,
            channel=channel,
            script_format=script_format,
            audio_tags_enabled=audio_tags_enabled,
            context=context,
        )

    voice_script = assemble_script(state["sections"])
    _script_trace("after_narrative_retry", voice_script)
    _log_post_retry_narrative_result(voice_script, blueprint, major_turns, covered_turns)
    return voice_script


def generate_script_sections(
    story,
    blueprint: dict,
    channel: Channel,
    channel_voice: ChannelVoice | None,
    script_format: str = "youtube_long",
    audio_tags_enabled: bool = False,
) -> dict:
    """Generate INTRO → body sections → OUTRO guided by the story blueprint.

    Each section is generated individually with per-section TTS and hook checks.
    Python controls the section count via _MIN_BODY_SECTIONS, _MAX_BODY_SECTIONS,
    and coverage of blueprint.major_turns. Claude's ``suggests_outro`` is advisory.

    Post-assembly:
    - check_completeness + check_minimum_length are run; issues logged as WARNING
      (non-blocking — per-section TTS enforcement makes assembly-level issues rare).
    - check_narrative_completeness (pure Python) is blocking: failing sections are
      regenerated once with targeted override instructions before proceeding.

    Global narrative-coherence validation (Haiku, ``validate_script_globally``) is
    no longer run here — it now runs once inside ``run_script_quality_gate()``,
    where its result is persisted to ``ContentValidation`` and its issues feed the
    same rewrite mechanism the quality gate already uses (Phase 10A-0). Running it
    here was redundant: this function returns before the quality gate ever sees the
    script, so the result had nowhere to go but a log line.
    """
    context = _build_section_generation_context(channel_voice, blueprint)
    state = _create_section_loop_state()
    _log_blueprint_summary(blueprint, context["major_turns"], context["max_body"])

    _generate_intro_section(
        story, blueprint, channel, script_format, audio_tags_enabled, context, state
    )
    _run_body_section_loop(
        story, blueprint, channel, script_format, audio_tags_enabled, context, state
    )
    _generate_outro_section(
        story, blueprint, channel, script_format, audio_tags_enabled, context, state
    )
    voice_script = _assemble_sections_with_diagnostics(
        state, story, blueprint, channel, script_format, context
    )
    voice_script = _run_narrative_completeness_retry(
        voice_script=voice_script,
        state=state,
        story=story,
        blueprint=blueprint,
        channel=channel,
        script_format=script_format,
        audio_tags_enabled=audio_tags_enabled,
        context=context,
    )

    _script_trace("generate_script_sections_returning", voice_script)
    return {
        "title": blueprint.get("suggested_title", story.title),
        "voice_script": voice_script,
        "visual_intent_history": state["visual_intent_history"],
        "_section_calls": state["section_calls"],
        "_retry_calls": state["narrative_retry_calls"],
    }


# ── Standalone short planning: Shorts Planner ──────────────────────────────────────────────────

_MAX_SHORT_CORRECTION_ROUNDS = 2
_MAX_SHORT_WORDS = 250  # 83 s at Cartesia sonic-2 ~3 words/s


def run_shorts_planner(
    long_content_id: "uuid.UUID",
    channel: Channel,
    config: ChannelConfig | None,
    db: Session,
) -> None:
    """Generate 3-5 standalone TikTok episode scripts from validated long content."""
    planner_source = _load_shorts_planner_source(long_content_id, db)
    if planner_source is None:
        return

    long_content, source_script = planner_source
    blueprint: dict = long_content.story_blueprint or {}
    voice_script = source_script.voice_script or ""
    channel_voice = _load_short_source_voice(long_content, channel, db)

    plan = _generate_shorts_plan_with_retry(voice_script, blueprint, channel)
    if plan is None:
        return

    total_parts: int = plan["total_parts"]
    parts: list[dict] = plan["parts"]
    logger.info(
        "run_shorts_planner: plan generated for content %s — %d parts",
        long_content_id,
        total_parts,
    )

    if _child_shorts_already_exist(long_content_id, db):
        return

    for part_plan in parts:
        part_n = part_plan.get("part", 0)
        part_plan_with_total = {**part_plan, "_total_parts": total_parts}
        short_content = _create_child_short_content(
            long_content=long_content,
            long_content_id=long_content_id,
            blueprint=blueprint,
            part_n=part_n,
            total_parts=total_parts,
            db=db,
        )

        generated = _generate_validated_short_script(
            part_plan=part_plan_with_total,
            part_n=part_n,
            voice_script=voice_script,
            blueprint=blueprint,
            channel=channel,
            channel_voice=channel_voice,
            source_language=long_content.source_language,
        )
        if generated is None:
            _remove_failed_short_content(short_content, part_n, db)
            continue

        _persist_child_short_script(
            short_content=short_content,
            generated=generated,
            source_language=long_content.source_language,
            channel=channel,
            audio_tags_enabled=config.audio_tags_enabled if config else False,
            part_n=part_n,
            total_parts=total_parts,
            db=db,
        )


def _load_shorts_planner_source(
    long_content_id: "uuid.UUID",
    db: Session,
) -> tuple[Content, Script] | None:
    long_content: Content | None = db.get(Content, long_content_id)
    if not long_content:
        logger.error("run_shorts_planner: content %s not found", long_content_id)
        return None

    source_script: Script | None = (
        db.query(Script)
        .filter(
            Script.content_id == long_content_id,
            Script.language == long_content.source_language,
            Script.validated.is_(True),
        )
        .order_by(Script.version.desc())
        .first()
    )
    if not source_script:
        logger.error(
            "run_shorts_planner: no validated source script for content %s", long_content_id
        )
        return None

    return long_content, source_script


def _load_short_source_voice(
    long_content: Content,
    channel: Channel,
    db: Session,
) -> ChannelVoice | None:
    return (
        db.query(ChannelVoice)
        .filter(
            ChannelVoice.channel_id == channel.id,
            ChannelVoice.language == long_content.source_language,
        )
        .first()
    )


def _generate_shorts_plan_with_retry(
    voice_script: str,
    blueprint: dict,
    channel: Channel,
) -> dict | None:
    for attempt in (1, 2):
        try:
            return generate_shorts_plan(voice_script, blueprint, channel)
        except ValueError as exc:
            if attempt == 1:
                logger.warning(
                    "run_shorts_planner: plan attempt %d failed (%s) — retrying", attempt, exc
                )
                continue
            logger.error(
                "run_shorts_planner: plan generation failed after 2 attempts (%s) — skipping Shorts",
                exc,
            )
            return None
        except Exception as exc:
            logger.error(
                "run_shorts_planner: plan generation API error (%s) — skipping Shorts", exc
            )
            return None
    return None


def _child_shorts_already_exist(long_content_id: "uuid.UUID", db: Session) -> bool:
    existing_count: int = (
        db.query(Content)
        .filter(
            Content.parent_content_id == long_content_id,
            Content.is_short_episode.is_(True),
        )
        .count()
    )
    if existing_count <= 0:
        return False

    existing_shorts: list[Content] = (
        db.query(Content)
        .filter(
            Content.parent_content_id == long_content_id,
            Content.is_short_episode.is_(True),
        )
        .all()
    )
    status_counts: dict[str, int] = {}
    for short_content in existing_shorts:
        status_counts[short_content.status] = status_counts.get(short_content.status, 0) + 1
    logger.info(
        "STANDALONE_SHORTS_ALREADY_EXIST parent_content_id=%s count=%d statuses=%s",
        long_content_id,
        existing_count,
        status_counts,
    )
    return True


def _create_child_short_content(
    long_content: Content,
    long_content_id: "uuid.UUID",
    blueprint: dict,
    part_n: int,
    total_parts: int,
    db: Session,
) -> Content:
    short_content = Content(
        channel_id=long_content.channel_id,
        source_url=long_content.source_url,
        source_language=long_content.source_language,
        content_hash=f"{long_content.content_hash}_short_{part_n}",
        title=f"{long_content.title} — Part {part_n}/{total_parts}",
        status="GENERATING_SCRIPTS",
        source_excerpt=long_content.source_excerpt,
        story_blueprint=blueprint,
        is_short_episode=True,
        parent_content_id=long_content_id,
        short_part_number=part_n,
        short_total_parts=total_parts,
    )
    db.add(short_content)
    db.flush()

    logger.info(
        "run_shorts_planner: created Content %s for part %d/%d",
        short_content.id,
        part_n,
        total_parts,
    )
    return short_content


def _generate_validated_short_script(
    part_plan: dict,
    part_n: int,
    voice_script: str,
    blueprint: dict,
    channel: Channel,
    channel_voice: ChannelVoice | None,
    source_language: str,
) -> dict | None:
    generated: dict | None = None
    tts_majors: list[dict] = []
    for correction_round in range(1, _MAX_SHORT_CORRECTION_ROUNDS + 2):
        try:
            result = generate_short_episode_script(
                part_plan=part_plan,
                long_voice_script=voice_script,
                blueprint=blueprint,
                channel=channel,
                channel_voice=channel_voice,
                override_instruction="" if correction_round == 1 else (
                    f"Fix these issues from the previous attempt: "
                    f"{'; '.join(i['description'] for i in tts_majors[:3])}"
                ),
            )
        except Exception as exc:
            logger.error(
                "run_shorts_planner: script error for part %d attempt %d: %s",
                part_n,
                correction_round,
                exc,
            )
            break

        ep_voice_script = result.get("voice_script", "")
        tts_majors = _collect_short_script_major_issues(
            ep_voice_script=ep_voice_script,
            source_language=source_language,
            part_n=part_n,
            correction_round=correction_round,
        )
        if not tts_majors:
            generated = result
            break

        if correction_round > _MAX_SHORT_CORRECTION_ROUNDS:
            logger.warning(
                "run_shorts_planner: part %d still has MAJOR issues after %d round(s) — "
                "using latest version",
                part_n,
                _MAX_SHORT_CORRECTION_ROUNDS,
            )
            generated = result
            break

        logger.info(
            "run_shorts_planner: part %d retry %d — %d MAJOR issue(s): %s",
            part_n,
            correction_round,
            len(tts_majors),
            [i["category"] for i in tts_majors],
        )
    return generated


def _collect_short_script_major_issues(
    ep_voice_script: str,
    source_language: str,
    part_n: int,
    correction_round: int,
) -> list[dict]:
    tts_issues = check_tts_compliance(ep_voice_script, source_language)
    first_sent = (
        re.split(r"(?<=[.!?])\s+", ep_voice_script.strip())[0]
        if ep_voice_script.strip() else ""
    )
    hook_issues = check_hook_quality(f"[INTRO]\n{first_sent}", source_language)
    tts_majors = [
        issue for issue in tts_issues + hook_issues
        if issue["severity"] == "MAJOR"
    ]

    ep_wc = len(ep_voice_script.split())
    if ep_wc > _MAX_SHORT_WORDS:
        tts_majors.append({
            "severity": "MAJOR",
            "category": "script_too_long",
            "description": (
                f"voice_script is {ep_wc} words — exceeds the {_MAX_SHORT_WORDS}-word hard cap "
                f"(≈83 s at Cartesia speed). Target 160–{_MAX_SHORT_WORDS} words. "
                f"Cut {ep_wc - _MAX_SHORT_WORDS} words by removing the least essential sentences."
            ),
        })
        logger.warning(
            "run_shorts_planner: part %d attempt %d word count %d > cap %d — will retry",
            part_n,
            correction_round,
            ep_wc,
            _MAX_SHORT_WORDS,
        )
    return tts_majors


def _remove_failed_short_content(short_content: Content, part_n: int, db: Session) -> None:
    db.delete(short_content)
    db.commit()
    logger.error(
        "run_shorts_planner: part %d script generation failed — content row removed",
        part_n,
    )


def _persist_child_short_script(
    short_content: Content,
    generated: dict,
    source_language: str,
    channel: Channel,
    audio_tags_enabled: bool,
    part_n: int,
    total_parts: int,
    db: Session,
) -> None:
    short_script = Script(
        content_id=short_content.id,
        language=source_language,
        voice_script=generated.get("voice_script", ""),
        version=1,
        validated=True,
    )
    db.add(short_script)
    db.flush()

    required_scripts = generate_multilingual_scripts(
        short_content,
        channel,
        db,
        audio_tags_enabled=audio_tags_enabled,
    )
    if not required_scripts:
        logger.error(
            "run_shorts_planner: part %d/%d script set incomplete — content=%s",
            part_n,
            total_parts,
            short_content.id,
        )
        return

    short_content.title = generated.get("title", short_content.title)
    short_content.status = "SCRIPTS_VALIDATED"
    db.commit()

    logger.info(
        "run_shorts_planner: part %d/%d SCRIPTS_VALIDATED — content=%s languages=%d",
        part_n,
        total_parts,
        short_content.id,
        len(required_scripts),
    )
