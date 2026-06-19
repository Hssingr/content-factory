import hashlib
import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelVoice, Content, Script
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
    logger.info("SCRIPT_TRACE [%s] words=%d sections=%d sha256=%s", label, wc, sec, h)


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
            logger.warning(
                "REPETITION[HIGH] label=%s overlap=%.3f vs=%r repeated=%s — "
                "section repeats prior material (non-blocking)",
                label, max_overlap, vs_label, repeated[:6],
            )
        else:
            logger.info(
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
    script_format: str = "youtube_long",
    language: str = "source",
    tts_model: str = "sonic-2",
    tts_provider: str = "cartesia",
) -> dict:
    """Run the Script Quality Gate — assess retention quality, rewrite if needed.

    Distinct from Agent 3's technical validator: this checks whether a normal
    YouTube viewer would actually keep watching (hook, clarity, pacing, generic
    AI phrasing, TTS readability) using fixed editorial criteria. Runs BEFORE
    persistence/Telegram so the user only ever sees retention-worthy scripts.

    Augments Claude's assessment with deterministic TTS and hook-quality checks
    (``check_tts_compliance`` and ``check_hook_quality``). Any MAJOR findings are
    folded into the rewrite pass so a single Sonnet call fixes everything at once.

    Loops at most ``_MAX_QUALITY_REWRITES`` times: assess → if NEEDS_REWRITE or
    det MAJOR, rewrite the FULL script preserving facts/markers, then re-assess.
    If still failing after the limit, the latest version is kept and a warning is
    logged — the pipeline never blocks on this check.

    Args:
        scripts:       Dict with ``title``, ``video_script``, ``voice_script``
                       (source-language, output of ``generate_scripts()``).
        channel:       Channel ORM object (provides niche and tone as context).
        script_format: Format key from ``channel_config.script_format``.
        language:      BCP-47 code for the source language (used by det checkers
                       to tag their issues). Defaults to "source" when unknown.
        tts_model:     TTS model ID for the source-language voice.
        tts_provider:  TTS provider ("cartesia" | "elevenlabs").

    Returns:
        The final scripts dict — rewritten if the gate required it, otherwise
        the original. Always has ``title``, ``video_script``, ``voice_script``.
    """
    current        = scripts
    _rewrite_calls = 0   # telemetry counter

    # ── FINAL TTS BACKSTOP — deterministic pre-gate cleanup ───────────────────
    # Strips forbidden chars and splits over-limit sentences before ANY Claude call
    # so the assessor evaluates an already-clean script and the rewriter doesn't
    # inherit deterministic MAJOR issues as its baseline.
    _bs_vs = current.get("voice_script", "")
    _bs_over_before = sum(
        1 for s in re.split(r"(?<=[.!?])\s+", _bs_vs) if len(s.split()) > 18
    )
    _bs_clean = split_long_sentences(normalize_tts_chars(_bs_vs))
    _bs_over_after = sum(
        1 for s in re.split(r"(?<=[.!?])\s+", _bs_clean) if len(s.split()) > 18
    )
    logger.info(
        "FINAL_TTS_BACKSTOP sentences_over_limit_before=%d sentences_over_limit_after=%d",
        _bs_over_before, _bs_over_after,
    )
    if _bs_clean != _bs_vs:
        current = {**current, "voice_script": _bs_clean, "video_script": _bs_clean}

    for attempt in range(1, _MAX_QUALITY_REWRITES + 1):
        _script_trace(f"quality_gate_input_{attempt}", current.get("voice_script", ""))
        # ── First / last sentence trace (quality gate input) ─────────────────
        _qg_vs = current.get("voice_script", "")
        _intro_m = re.search(r"\[INTRO\]\s*(.*?)(?=\n\s*\[|\Z)", _qg_vs, re.DOTALL | re.IGNORECASE)
        if _intro_m:
            _intro_sents = [s for s in re.split(r"(?<=[.!?])\s+", _intro_m.group(1).strip()) if s.strip()]
            logger.info(
                "QUALITY_GATE_INPUT attempt=%d intro_first=%r",
                attempt, (_intro_sents[0][:120] if _intro_sents else ""),
            )
        _outro_m = re.search(r"\[OUTRO\]\s*(.*?)$", _qg_vs, re.DOTALL | re.IGNORECASE)
        if _outro_m:
            _outro_sents = [s for s in re.split(r"(?<=[.!?])\s+", _outro_m.group(1).strip()) if s.strip()]
            logger.info(
                "QUALITY_GATE_INPUT attempt=%d outro_last=%r",
                attempt, (_outro_sents[-1][:120] if _outro_sents else ""),
            )
        # ── Claude assessment ─────────────────────────────────────────────────
        try:
            review = assess_script_quality(current, channel, script_format=script_format)
        except Exception as exc:
            logger.error(
                "Script Quality Gate assessment failed (attempt %d): %s — keeping script as-is",
                attempt, exc,
            )
            _emit_script_cost_estimate(scripts, _rewrite_calls)
            return current

        status = review.get("status", "PASSED")
        claude_issues: list[dict] = review.get("issues", [])

        # ── Deterministic TTS + hook checks ───────────────────────────────────
        voice_script = current.get("voice_script", "")
        tts_det = check_tts_compliance(voice_script, language)
        hook_det = check_hook_quality(voice_script, language)
        det_majors = [i for i in tts_det + hook_det if i["severity"] == "MAJOR"]

        # Convert det issues to quality-gate format (HIGH severity, "fix" key)
        converted_det: list[dict] = [
            {
                "severity": "HIGH",
                "category": i["category"],
                "description": i["description"],
                "fix": i["suggestion"],
            }
            for i in det_majors
        ]

        all_issues = claude_issues + converted_det
        high = sum(1 for i in all_issues if i.get("severity") == "HIGH")

        logger.info(
            "Script Quality Gate: claude=%s det_major=%d issues=%d (high=%d) attempt=%d",
            status, len(converted_det), len(all_issues), high, attempt,
        )
        _tts_maj_cnt  = len([i for i in tts_det  if i["severity"] == "MAJOR"])
        _hook_maj_cnt = len([i for i in hook_det if i["severity"] == "MAJOR"])
        logger.info(
            "QUALITY_GATE_BREAKDOWN attempt=%d det_tts_maj=%d det_hook_maj=%d",
            attempt, _tts_maj_cnt, _hook_maj_cnt,
        )
        for issue in all_issues:
            logger.info(
                "Script quality issue [%s/%s]: %s -> %s",
                issue.get("severity", "?"), issue.get("category", "?"),
                issue.get("description", ""), issue.get("fix", ""),
            )

        # ── Decision: pass or rewrite ─────────────────────────────────────────
        if status == "PASSED" and not converted_det:
            _script_trace(f"quality_gate_passed_attempt_{attempt}", current.get("voice_script", ""))
            _emit_script_cost_estimate(scripts, _rewrite_calls)
            return current

        # ── Skip rewrite if every remaining HIGH issue is TTS-only ───────────
        # sentence-length violations are cheaper to fix deterministically than to
        # spend 5k+ output tokens on a full Sonnet quality rewrite.
        _high_issues = [i for i in all_issues if i.get("severity") == "HIGH"]
        _tts_only    = bool(_high_issues) and all(
            i.get("category") == "tts_compliance" for i in _high_issues
        )
        if _tts_only:
            logger.info(
                "QUALITY_REWRITE_SKIPPED reason=TTS_ONLY high_count=%d attempt=%d",
                len(_high_issues), attempt,
            )
            _vs = current.get("voice_script", "")
            _cleaned = split_long_sentences(normalize_tts_chars(_vs))
            if _cleaned != _vs:
                current = {**current, "voice_script": _cleaned, "video_script": _cleaned}
                logger.info("QUALITY_REWRITE_SKIPPED: deterministic cleanup applied")
            _script_trace(f"quality_gate_tts_only_cleanup_{attempt}", current.get("voice_script", ""))
            continue  # re-assess next iteration — no Claude call

        try:
            current = rewrite_script_for_quality(
                current, all_issues, channel,
                script_format=script_format,
                tts_model=tts_model,
                tts_provider=tts_provider,
            )
            _rewrite_calls += 1
            logger.info("QUALITY_REWRITE_SCHEMA_OK attempt=%d", attempt)
        except Exception as exc:
            logger.error(
                "QUALITY_REWRITE_JSON_FAIL attempt=%d error=%s — keeping prior script",
                attempt, exc,
            )
            _script_trace(f"quality_gate_rewrite_failed_{attempt}", current.get("voice_script", ""))
            _emit_script_cost_estimate(scripts, _rewrite_calls)
            return current

        # Deterministic cleanup after rewrite — rewrite output may introduce long sentences
        # or forbidden TTS chars (/ % & ()). Without this the next loop iteration finds the
        # same det MAJORs again, which loops Claude into the same rewrite. Both must be applied
        # so the re-assessment loop has clean input.
        _rw_vs = current.get("voice_script", "")
        _rw_clean = split_long_sentences(normalize_tts_chars(_rw_vs))
        if _rw_clean != _rw_vs:
            current = {**current, "voice_script": _rw_clean, "video_script": _rw_clean}
            logger.info(
                "Script Quality Gate: deterministic cleanup applied after rewrite (attempt %d)",
                attempt,
            )
        _script_trace(f"quality_gate_after_rewrite_{attempt}", current.get("voice_script", ""))

    logger.warning(
        "Script Quality Gate: still NEEDS_REWRITE after %d attempt(s) — proceeding with latest version",
        _MAX_QUALITY_REWRITES,
    )
    # Final cleanup — ensures the script entering multilingual generation satisfies as
    # many deterministic TTS rules as possible even when Claude couldn't fully comply.
    _final_vs = current.get("voice_script", "")
    _final_clean = split_long_sentences(normalize_tts_chars(_final_vs))
    if _final_clean != _final_vs:
        current = {**current, "voice_script": _final_clean, "video_script": _final_clean}
        logger.info("Script Quality Gate: final deterministic cleanup applied before returning")
    _script_trace("quality_gate_max_retries_return", current.get("voice_script", ""))
    _emit_script_cost_estimate(scripts, _rewrite_calls)
    return current


def generate_multilingual_scripts(
    content: Content,
    channel: Channel,
    db: Session,
    audio_tags_enabled: bool = False,
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

    # ── Build voice map: language → ChannelVoice (for tts_model + provider) ──
    voice_map: dict[str, ChannelVoice] = {
        v.language: v
        for v in db.query(ChannelVoice).filter(ChannelVoice.channel_id == channel.id).all()
    }

    # ── Extract hook context from the (potentially optimised) source script ───
    hook_context = _extract_hook_context(source_script.voice_script, script_format)

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

        # Resolve per-language voice model and provider; fallback to Cartesia defaults
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
                video_script=source_script.video_script,
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
            continue   # partial failure — other languages still proceed

        script = Script(
            content_id=content.id,
            language=lang,
            video_script=adapted["video_script"],
            voice_script=adapted["voice_script"],
            version=1,
            validated=False,
            # estimated_duration_sec set by Agent 3
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

    logger.info("TURN_MATCH label=%s covered=%s/%d", label, sorted(covered), len(major_turns))
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
    """Generate a single section, retrying up to _MAX_SECTION_RETRIES on MAJOR violations.

    Per-section checks (in order):
    1. check_tts_compliance — always run.
    2. check_hook_quality   — INTRO only (check_hook=True).
    3. check_section_transition — body/OUTRO only; MINOR, folded into retry override
       if a MAJOR retry is already triggered. Never hard-blocks on its own.

    On MAJOR findings the specific descriptions (plus any MINOR transition note) are
    forwarded as override_instruction to the next attempt.

    Args:
        label:                   Section label ("INTRO", "SECTION 1", "OUTRO", …).
        story:                   Story object passed to generate_section().
        blueprint:               Blueprint dict.
        prior_sections_summary:  Accumulated summaries from all prior sections.
        visual_intent_accumulator: Accumulated visual avoid list.
        channel:                 Channel ORM object.
        script_format:           Format key.
        tts_model:               TTS model ID.
        tts_provider:            TTS provider.
        audio_tags_enabled:      ElevenLabs v3 audio tag opt-in.
        check_hook:              Whether to run check_hook_quality (INTRO only).
        prior_summary_text:      Summary text of the immediately preceding section —
                                 used by check_section_transition. Empty string skips
                                 the check (correct for INTRO).
        primary_required_turn:   The single earliest uncovered turn to advance. None for
                                 INTRO/OUTRO (no constraint). Forwarded to generate_section().
        future_uncovered_turns:  Remaining uncovered turns after the primary. Injected as
                                 "do NOT resolve yet". None when ≤1 uncovered turn remains.

    Returns:
        Section dict from generate_section(), or None if all retries exhausted.
    """
    override = ""
    for attempt in range(1, _MAX_SECTION_RETRIES + 2):
        # ── Section generation input log ──────────────────────────────────────
        logger.info(
            "SECTION_INPUT label=%s attempt=%d prior_count=%d avoid_count=%d override=%s",
            label, attempt, len(prior_sections_summary),
            len(visual_intent_accumulator.get("avoid_repeating", [])),
            bool(override),
        )
        for _ps in prior_sections_summary[-3:]:
            logger.debug(
                "  SECTION_INPUT prior[%s] summary=%r reveals=%s open_q=%s",
                _ps.get("label"),
                (_ps.get("summary") or "")[:80],
                (_ps.get("reveals") or [])[:3],
                (_ps.get("open_questions") or [])[:2],
            )
        try:
            result = generate_section(
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
            if attempt > _MAX_SECTION_RETRIES:
                return None
            continue

        script_text = result.get("script_text", "")

        # ── Section generation output log ─────────────────────────────────────
        _wc_raw      = len(script_text.split())
        _sc_raw      = _count_sentences(script_text)
        _max_len_raw = _max_sentence_len(script_text)
        _first_raw   = (re.split(r"(?<=[.!?])\s+", script_text.strip()) or [""])[0][:120]
        logger.info(
            "SECTION_OUTPUT label=%s attempt=%d words=%d sents=%d max_sent=%d suggests_outro=%s",
            label, attempt, _wc_raw, _sc_raw, _max_len_raw, result.get("suggests_outro", False),
        )
        logger.info("SECTION_OUTPUT label=%s first_sent=%r", label, _first_raw)
        logger.debug(
            "SECTION_OUTPUT label=%s summary=%r reveals=%s open_q=%s vi_goal=%r",
            label,
            (result.get("summary") or "")[:100],
            [(r or "")[:60] for r in (result.get("reveals") or [])[:3]],
            [(q or "")[:60] for q in (result.get("open_questions") or [])[:2]],
            ((result.get("visual_intent") or {}).get("section_goal") or "")[:80],
        )

        # ── Deterministic backstop: normalize forbidden chars, then split long sentences ─
        # Runs on EVERY attempt — normalize first so split operates on clean text.
        # A section that passes on attempt 1 or 2 has its forbidden chars (/ % & ())
        # removed here; without this normalize would only run in the final-cleanup block
        # (after max retries), letting "/" survive into the assembled script and
        # re-trigger issues at the quality gate.
        cleaned = normalize_tts_chars(script_text)
        cleaned = split_long_sentences(cleaned)
        _backstop_changed = (cleaned != script_text)
        if _backstop_changed:
            script_text = cleaned
            result = {**result, "script_text": script_text}

        # ── Deterministic checks ──────────────────────────────────────────────
        tts_issues        = check_tts_compliance(script_text, "source")
        hook_issues       = check_hook_quality(script_text, "source") if check_hook else []
        transition_issues = (
            check_section_transition(script_text, prior_summary_text)
            if prior_summary_text else []
        )
        majors = [i for i in tts_issues + hook_issues if i["severity"] == "MAJOR"]

        # ── Cleanup log ───────────────────────────────────────────────────────
        _tts_maj = len([i for i in tts_issues if i["severity"] == "MAJOR"])
        logger.info(
            "SECTION_CLEANUP label=%s attempt=%d backstop=%s words=%d→%d "
            "max_sent=%d→%d tts_maj=%d total_maj=%d",
            label, attempt, _backstop_changed,
            _wc_raw, len(script_text.split()),
            _max_len_raw, _max_sentence_len(script_text),
            _tts_maj, len(majors),
        )

        # Log MINOR transition issues regardless of retry outcome
        for ti in transition_issues:
            logger.info(
                "Section %s transition check [MINOR]: %s", label, ti["description"]
            )

        if not majors:
            return result

        if attempt > _MAX_SECTION_RETRIES:
            # Final deterministic cleanup — normalize forbidden chars then re-split
            cleaned = normalize_tts_chars(script_text)
            cleaned = split_long_sentences(cleaned)
            if cleaned != script_text:
                script_text = cleaned
                result = {**result, "script_text": script_text}

            # Re-check to get accurate final issue count for logging
            final_majors = [
                i for i in
                check_tts_compliance(script_text, "source")
                + (check_hook_quality(script_text, "source") if check_hook else [])
                if i["severity"] == "MAJOR"
            ]
            if final_majors:
                logger.warning(
                    "Section %s: proceeding with %d known TTS MAJOR issue(s) after "
                    "final deterministic cleanup — %s",
                    label, len(final_majors),
                    [f"{i['category']}: {(i.get('offending_text') or '')[:50]}"
                     for i in final_majors],
                )
            else:
                logger.info(
                    "Section %s: final deterministic cleanup resolved all MAJOR issues",
                    label,
                )
            return result

        # Build override: MAJOR descriptions + MINOR transition note (if any)
        feedback_parts = [i["description"] for i in majors[:3]]
        if transition_issues:
            feedback_parts.append(transition_issues[0]["description"])
        override = f"Fix these issues from the previous attempt: {'; '.join(feedback_parts)}"
        logger.info("Section %s retry %d — issues: %s", label, attempt, override)

    return None


def assemble_script(sections: list[dict]) -> tuple[str, str]:
    """Assemble section dicts into a marked voice_script and video_script.

    voice_script and video_script are identical: Agent 4 visuals generate their own visual
    decisions via the storyboard and does not depend on video_script content.

    Args:
        sections: List of dicts with keys ``label`` and ``script_text``, in order.

    Returns:
        Tuple of (voice_script, video_script) — both are the same assembled text.
    """
    parts: list[str] = []
    for s in sections:
        parts.append(f"[{s['label']}]")
        parts.append(s["script_text"])
    assembled = "\n\n".join(parts)
    return assembled, assembled


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
    - validate_script_globally (Haiku) checks narrative coherence; issues logged only.
    - check_narrative_completeness (pure Python) is blocking: failing sections are
      regenerated once with targeted override instructions before proceeding.

    Args:
        story:           Story object (title, url, body, language).
        blueprint:       Dict from generate_story_blueprint().
        channel:         Channel ORM object.
        channel_voice:   ChannelVoice for TTS model/provider. May be None (cartesia default).
        script_format:   Format key from channel_config.script_format.
        audio_tags_enabled: ElevenLabs v3 audio tag opt-in.

    Returns:
        Dict with: title, video_script, voice_script, visual_intent_history.
        visual_intent_history is a list of {label, section_goal, primary_visual_focus,
        avoid_repeating} per section — merged into content.story_blueprint by tasks.py.

    Raises:
        RuntimeError: If INTRO or OUTRO generation fails after all retries.
    """
    tts_model    = (channel_voice.tts_model   if channel_voice else "sonic-2")
    tts_provider = (channel_voice.provider    if channel_voice else "cartesia")
    major_turns  = blueprint.get("major_turns") or []
    max_body     = max(
        _MIN_BODY_SECTIONS,
        min(_MAX_BODY_SECTIONS, blueprint.get("suggested_section_count", 3)),
    )
    # When the blueprint has 4+ turns, require at least min(4, n_turns) body sections
    # so suggests_outro cannot collapse the loop before each turn gets its own section.
    _min_body_for_bp = (
        max(_MIN_BODY_SECTIONS, min(4, len(major_turns)))
        if len(major_turns) >= 4
        else _MIN_BODY_SECTIONS
    )

    visual_intent_accumulator: dict     = {"avoid_repeating": []}
    prior_sections_summary:    list     = []
    sections:                  list     = []
    visual_intent_history:     list     = []
    covered_turns:             set[int] = set()
    _sec_calls:                int      = 0    # section generation calls (telemetry)
    _narrative_retry_calls:    int      = 0    # narrative retry section calls (telemetry)

    # ── Blueprint log ─────────────────────────────────────────────────────────
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

    # ── INTRO ─────────────────────────────────────────────────────────────────
    _uncov = list(range(len(major_turns)))
    logger.info("SECTION_INPUT label=INTRO sections_so_far=0 covered=[] uncovered=%s", _uncov)
    intro = _generate_section_with_retry(
        label="INTRO",
        story=story,
        blueprint=blueprint,
        prior_sections_summary=[],
        visual_intent_accumulator=visual_intent_accumulator,
        channel=channel,
        script_format=script_format,
        tts_model=tts_model,
        tts_provider=tts_provider,
        audio_tags_enabled=audio_tags_enabled,
        check_hook=True,
        prior_summary_text="",   # no prior section — transition check skipped
        primary_required_turn=None,    # INTRO sets the stage; no single-turn constraint
        future_uncovered_turns=None,
    )
    if intro is None:
        raise RuntimeError("generate_script_sections: INTRO generation failed after retries")
    _sec_calls += 1
    sections.append({"label": "INTRO", "script_text": intro["script_text"]})
    _update_accumulator(visual_intent_accumulator, intro, visual_intent_history, "INTRO")
    prior_sections_summary.append({
        "label": "INTRO",
        "summary": intro.get("summary", ""),
        "reveals": intro.get("reveals", []),
        "open_questions": intro.get("open_questions", []),
    })
    covered_turns |= _match_turns(intro.get("reveals", []), major_turns, intro.get("script_text", ""), label="INTRO")

    # ── Body sections ─────────────────────────────────────────────────────────
    body_index = 1
    while True:
        label = f"SECTION {body_index}"
        _uncov_now    = [i for i in range(len(major_turns)) if i not in covered_turns]
        _primary_idx  = _uncov_now[0] if _uncov_now else None
        _primary_turn = major_turns[_primary_idx] if _primary_idx is not None else None
        _future_turns = [major_turns[i] for i in _uncov_now[1:]]
        logger.info(
            "SECTION_INPUT label=%s sections_so_far=%d covered=[%s] "
            "primary_turn_idx=%s uncovered=%s",
            label, len(prior_sections_summary),
            ",".join(str(i) for i in sorted(covered_turns)),
            _primary_idx, _uncov_now,
        )
        section = _generate_section_with_retry(
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
            check_hook=False,
            prior_summary_text=prior_sections_summary[-1]["summary"] if prior_sections_summary else "",
            primary_required_turn=_primary_turn,
            future_uncovered_turns=_future_turns if _future_turns else None,
        )
        if section is None:
            logger.warning(
                "generate_script_sections: %s failed after retries — stopping body loop", label
            )
            break
        _sec_calls += 1
        sections.append({"label": label, "script_text": section["script_text"]})
        _update_accumulator(visual_intent_accumulator, section, visual_intent_history, label)
        prior_sections_summary.append({
            "label": label,
            "summary": section.get("summary", ""),
            "reveals": section.get("reveals", []),
            "open_questions": section.get("open_questions", []),
        })
        _all_matched = _match_turns(
            section.get("reveals", []), major_turns,
            section.get("script_text", ""), label=label,
        )
        if len(_all_matched) >= 3:
            logger.warning(
                "generate_script_sections: %s over-compressed major turns — "
                "matched %d turns %s, crediting only primary turn [%s]",
                label, len(_all_matched), sorted(_all_matched), _primary_idx,
            )
            if _primary_idx is not None:
                covered_turns.add(_primary_idx)
            else:
                covered_turns |= _all_matched
        else:
            # 0, 1, or 2 matched — credit all; also always credit primary
            covered_turns |= _all_matched
            if _primary_idx is not None:
                covered_turns.add(_primary_idx)

        body_index += 1

        all_turns_covered     = len(covered_turns) >= len(major_turns)
        at_min                = body_index > _min_body_for_bp
        claude_suggests_outro = bool(section.get("suggests_outro", False))
        at_soft_max           = body_index > max_body          # blueprint-guided cap
        at_hard_max           = body_index > _MAX_BODY_SECTIONS  # absolute V2 cap
        payoff_done           = _payoff_reached(section, blueprint)

        logger.info(
            "LOOP_DECISION body_index=%d covered=%d/%d all_covered=%s payoff=%s "
            "suggests_outro=%s at_min=%s at_soft=%s at_hard=%s min_body=%d",
            body_index, len(covered_turns), len(major_turns),
            all_turns_covered, payoff_done, claude_suggests_outro,
            at_min, at_soft_max, at_hard_max, _min_body_for_bp,
        )

        # Hard cap — always exit, log a warning if turns are still uncovered
        if at_hard_max:
            if not all_turns_covered:
                logger.warning(
                    "generate_script_sections: hard cap (%d body sections) reached with "
                    "%d/%d major turns still uncovered — proceeding to OUTRO",
                    _MAX_BODY_SECTIONS,
                    len(major_turns) - len(covered_turns), len(major_turns),
                )
                logger.info("LOOP_DECISION: break_hard_cap reason=uncovered_turns_remain")
            else:
                logger.info(
                    "generate_script_sections: ending body loop after %d section(s) "
                    "(hard cap reached, all turns covered)",
                    body_index - 1,
                )
                logger.info("LOOP_DECISION: break_hard_cap reason=all_covered")
            break

        # Soft cap reached but turns are uncovered — keep going to hard cap
        if at_soft_max and not all_turns_covered:
            logger.warning(
                "generate_script_sections: soft max (%d) reached with %d/%d major turns "
                "uncovered — extending to hard cap (%d)",
                max_body,
                len(major_turns) - len(covered_turns), len(major_turns),
                _MAX_BODY_SECTIONS,
            )
            logger.info("LOOP_DECISION: continue reason=soft_max_but_uncovered_turns")
            # fall through — loop continues to next body section

        # Normal exit: turns covered + past min + (soft max OR Claude says done)
        elif all_turns_covered and at_min and (at_soft_max or claude_suggests_outro):
            logger.info(
                "generate_script_sections: ending body loop after %d section(s) "
                "(covered_turns=%d/%d, suggests_outro=%s, at_soft_max=%s)",
                body_index - 1, len(covered_turns), len(major_turns),
                claude_suggests_outro, at_soft_max,
            )
            logger.info(
                "LOOP_DECISION: break_normal reason=all_covered+past_min+(%s)",
                "soft_max" if at_soft_max else "claude_suggests",
            )
            break
        else:
            logger.info(
                "LOOP_DECISION: continue reason=not_all_conditions_met "
                "(all_covered=%s at_min=%s at_soft=%s claude=%s)",
                all_turns_covered, at_min, at_soft_max, claude_suggests_outro,
            )

    # ── OUTRO ─────────────────────────────────────────────────────────────────
    _uncov_outro = [i for i in range(len(major_turns)) if i not in covered_turns]
    logger.info(
        "SECTION_INPUT label=OUTRO sections_so_far=%d covered=[%s] uncovered=%s",
        len(prior_sections_summary),
        ",".join(str(i) for i in sorted(covered_turns)),
        _uncov_outro,
    )
    outro = _generate_section_with_retry(
        label="OUTRO",
        story=story,
        blueprint=blueprint,
        prior_sections_summary=prior_sections_summary,
        visual_intent_accumulator=visual_intent_accumulator,
        channel=channel,
        script_format=script_format,
        tts_model=tts_model,
        tts_provider=tts_provider,
        audio_tags_enabled=audio_tags_enabled,
        check_hook=False,
        prior_summary_text=prior_sections_summary[-1]["summary"] if prior_sections_summary else "",
        primary_required_turn=None,   # OUTRO resolves; no single-turn constraint
        future_uncovered_turns=None,
    )
    if outro is None:
        raise RuntimeError("generate_script_sections: OUTRO generation failed after retries")
    _sec_calls += 1
    sections.append({"label": "OUTRO", "script_text": outro["script_text"]})
    _update_accumulator(visual_intent_accumulator, outro, visual_intent_history, "OUTRO")

    # ── OUTRO overlap diagnostic ───────────────────────────────────────────────
    _outro_text       = outro["script_text"]
    _prev_body        = [s for s in sections[:-1] if s["label"] not in ("INTRO", "OUTRO")]
    if _prev_body:
        _outro_tokens = _get_content_tokens(_outro_text)
        _prev_tokens  = _get_content_tokens(_prev_body[-1]["script_text"])
        if _outro_tokens:
            _outro_ov   = len(_outro_tokens & _prev_tokens) / len(_outro_tokens)
            _repeated   = sorted(_outro_tokens & _prev_tokens)[:8]
            if _outro_ov > 0.5:
                logger.warning(
                    "OUTRO_OVERLAP previous_section_overlap=%.3f repeated_terms=%s "
                    "— OUTRO heavily repeats prior section (non-blocking)",
                    _outro_ov, _repeated,
                )
            else:
                logger.info(
                    "OUTRO_OVERLAP previous_section_overlap=%.3f repeated_terms=%s",
                    _outro_ov, _repeated,
                )

    # ── Repetition diagnostics (diagnostic only — non-blocking) ───────────────
    diagnose_section_repetition(sections)

    # ── Assemble ──────────────────────────────────────────────────────────────
    voice_script, video_script = assemble_script(sections)
    _script_trace("after_section_assembly", voice_script)

    # ── Generic-phrase scan (diagnostic only — non-blocking) ─────────────────
    _phrase_hits = detect_generic_documentary_phrases(voice_script)
    for _hit in _phrase_hits:
        logger.warning(
            "GENERIC_PHRASE detected=%r in sentence=%r — rewrite recommended (non-blocking)",
            _hit["phrase"], _hit["sentence"],
        )

    # ── Post-assembly deterministic checks ────────────────────────────────────
    # check_completeness: structural markers are guaranteed by assemble_script() —
    # these issues are telemetry only and never trigger correction.
    # check_minimum_length: per-section generation has no word-count floor, so
    # an under-length assembly is possible; correct it once with source_excerpt.
    completeness_issues = check_completeness(video_script, voice_script, "source")
    length_issues       = check_minimum_length(voice_script, "source", script_format)

    if completeness_issues:
        logger.warning(
            "generate_script_sections: post-assembly completeness issue(s) (telemetry): %s",
            [i.get("description") for i in completeness_issues],
        )

    length_majors = [i for i in length_issues if i.get("severity") == "MAJOR"]
    if length_majors:
        wc_before = len(voice_script.split())
        logger.warning(
            "generate_script_sections: voice_script under minimum length (%d words) — "
            "calling auto_correct_script once with source_excerpt",
            wc_before,
        )
        try:
            corrected = auto_correct_script(
                current_scripts={"video_script": video_script, "voice_script": voice_script},
                issues=length_majors,
                language=story.language,
                channel=channel,
                script_format=script_format,
                source_excerpt=(story.body or "")[:8000],
                tts_model=tts_model,
                tts_provider=tts_provider,
            )
            video_script = corrected.get("video_script", video_script)
            voice_script = corrected.get("voice_script", voice_script)
            wc_after = len(voice_script.split())
            logger.info(
                "generate_script_sections: length correction applied — %d → %d words",
                wc_before, wc_after,
            )
        except Exception as exc:
            logger.warning(
                "generate_script_sections: length correction failed (non-blocking): %s", exc
            )

    # ── Global validation (Haiku — narrative coherence; non-blocking) ─────────
    try:
        gv = validate_script_globally(voice_script, blueprint)
        if gv.get("status") == "NEEDS_FIX":
            for issue in gv.get("issues", []):
                logger.info(
                    "Global validation [%s]: %s — %s",
                    issue.get("section"), issue.get("description"), issue.get("suggestion"),
                )
    except Exception as exc:
        logger.warning("Global validation failed (non-blocking): %s", exc)

    # ── Narrative completeness — BLOCKING with targeted section retry ─────────
    # Issue prefix → which section label to regenerate (max 1 call per issue).
    # Multiple issues targeting the same section are merged into one call.
    _ISSUE_TO_SECTION: list[tuple[str, str | None]] = [
        ("Hook:",            "INTRO"),
        ("Major turns",      None),      # resolved at runtime: last body section
        ("final_payoff",     "OUTRO"),
        ("comment_trigger",  "OUTRO"),
    ]

    # ── Turn coverage alignment — log both views before checking ─────────────
    _vs_body_tokens = _get_content_tokens(voice_script)
    _nc_would_flag: list[int] = []
    for _i, _turn in enumerate(major_turns):
        _tt = _get_content_tokens(_turn)
        _ov = len(_tt & _vs_body_tokens) / len(_tt) if _tt else 0.0
        if _ov < 0.6:
            _nc_would_flag.append(_i)
    logger.info(
        "TURN_COVERAGE_SOURCE section_progression=%s narrative_check=%s",
        sorted(covered_turns), _nc_would_flag,
    )
    _disagreement = covered_turns & set(_nc_would_flag)
    if _disagreement:
        logger.warning(
            "TURN_COVERAGE_DISAGREEMENT: section_progression credits turns %s but "
            "60%%-overlap check would flag them — section_progression is authoritative, "
            "these turns will be excluded from narrative retry",
            sorted(_disagreement),
        )

    logger.info(
        "TURN_COVERAGE_FINAL authoritative=%s total=%d/%d",
        sorted(covered_turns), len(covered_turns), len(major_turns),
    )

    nc_issues = check_narrative_completeness(voice_script, blueprint, already_covered=covered_turns)
    if nc_issues:
        logger.info(
            "generate_script_sections: narrative completeness issues before retry: %s", nc_issues
        )

        # Build label_to_idx for in-place replacement
        label_to_idx: dict[str, int] = {s["label"]: i for i, s in enumerate(sections)}
        body_labels   = [s["label"] for s in sections if s["label"] not in ("INTRO", "OUTRO")]

        # Group issues by target section label (deduplicate targets)
        section_instructions: dict[str, list[str]] = {}
        for issue in nc_issues:
            target_label: str | None = None
            for prefix, lbl in _ISSUE_TO_SECTION:
                if issue.startswith(prefix):
                    target_label = lbl
                    break
            if target_label is None:
                # "Major turns" fallback: last body section
                target_label = body_labels[-1] if body_labels else "OUTRO"
            section_instructions.setdefault(target_label, []).append(issue)

        # One generate_section() call per affected section
        for target_label, instructions in section_instructions.items():
            idx = label_to_idx.get(target_label)
            if idx is None:
                logger.warning(
                    "generate_script_sections: narrative retry — section %r not found, skipping",
                    target_label,
                )
                continue

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
            _narrative_retry_calls += 1
            _old_sha   = hashlib.sha256(sections[idx]["script_text"].encode("utf-8", errors="replace")).hexdigest()[:8]
            _old_first = (re.split(r"(?<=[.!?])\s+", sections[idx]["script_text"].strip()) or [""])[0][:80]
            _covered_before_retry = len(covered_turns)
            try:
                # Prior summary is everything before this section's index
                prior_for_retry = [
                    {"label": s["label"], "summary": "", "reveals": [], "open_questions": []}
                    for s in sections[:idx]
                ]
                result = generate_section(
                    label=target_label,
                    story=story,
                    blueprint=blueprint,
                    prior_sections_summary=prior_for_retry,
                    visual_intent_accumulator=visual_intent_accumulator,
                    channel=channel,
                    script_format=script_format,
                    tts_model=tts_model,
                    tts_provider=tts_provider,
                    audio_tags_enabled=audio_tags_enabled,
                    override_instruction=override,
                )
                retry_text = result.get("script_text", "")

                # Backstop — generate_section() is called directly here, bypassing the
                # normalize+split loop in _generate_section_with_retry(). Apply it now
                # so that the retried section is as clean as any regularly generated one.
                _rt_cleaned = split_long_sentences(normalize_tts_chars(retry_text))
                if _rt_cleaned != retry_text:
                    retry_text = _rt_cleaned
                    logger.info(
                        "generate_script_sections: narrative retry backstop modified %r",
                        target_label,
                    )

                # INTRO: verify hook quality — a bad opener here would survive to the
                # quality gate and re-trigger the same MAJOR hook issue.
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

                sections[idx] = {"label": target_label, "script_text": retry_text}
                _new_sha   = hashlib.sha256(retry_text.encode("utf-8", errors="replace")).hexdigest()[:8]
                _new_first = (re.split(r"(?<=[.!?])\s+", retry_text.strip()) or [""])[0][:80]
                _retry_coverage = _match_turns(
                    result.get("reveals", []), major_turns, retry_text,
                    label=f"{target_label}_retry_check",
                )
                _covered_after_retry = len(covered_turns | _retry_coverage)
                logger.info(
                    "NARRATIVE_RETRY target=%r sha=%s→%s first_sent=%r",
                    target_label, _old_sha, _new_sha, _new_first,
                )
                logger.info(
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

        # Reassemble after retries
        voice_script, video_script = assemble_script(sections)
        _script_trace("after_narrative_retry", voice_script)

        # Re-check once — pass already_covered so section_progression turns are not
        # false-alarmed here (the post-retry overlap view differs from the per-section
        # reveal-matching view, causing spurious "still failing" warnings).
        nc_issues_after = check_narrative_completeness(voice_script, blueprint, already_covered=covered_turns)
        if nc_issues_after:
            logger.warning(
                "generate_script_sections: narrative completeness still failing after retry: %s",
                nc_issues_after,
            )
            # Compute any turns that overlap disagrees with section_progression and log them
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

    _script_trace("generate_script_sections_returning", voice_script)
    return {
        "title":                  blueprint.get("suggested_title", story.title),
        "video_script":           video_script,
        "voice_script":           voice_script,
        "visual_intent_history":  visual_intent_history,
        "_section_calls":         _sec_calls,
        "_retry_calls":           _narrative_retry_calls,
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
    """Generate 3–5 standalone TikTok episode scripts from a validated long-form content.

    Orchestrates the Shorts Planner pipeline:
      1. Load source Script for the long-form content.
      2. Call generate_shorts_plan() (Haiku) — returns part plan with 3–5 parts.
         Python validates 3 ≤ total_parts ≤ 5. Retries once on range violation.
      3. For each part:
         a. Create Content row (is_short_episode=True, parent_content_id, etc.).
         b. Call generate_short_episode_script() (Sonnet) to write TikTok narration.
         c. Run check_tts_compliance() only (not run_deterministic_checks — Short
            episode scripts have no [SECTION N] markers and intentionally short word
            count; check_completeness and check_minimum_length would always false-MAJOR).
         d. Auto-correct up to _MAX_SHORT_CORRECTION_ROUNDS rounds on MAJOR TTS issues.
         e. Persist Script row. Set content.status = "SCRIPTS_VALIDATED".
      4. Returns None — Shorts failures are logged but never affect the parent content.

    Args:
        long_content_id: UUID of the parent long-form Content row (status=SCRIPTS_VALIDATED).
        channel:         Channel ORM object.
        config:          ChannelConfig ORM object (may be None — defaults applied).
        db:              SQLAlchemy session managed by the caller.
    """
    script_format = config.script_format if config else "youtube_long"

    # ── Load source Script ────────────────────────────────────────────────────
    long_content: Content | None = db.get(Content, long_content_id)
    if not long_content:
        logger.error("run_shorts_planner: content %s not found", long_content_id)
        return

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
        return

    blueprint: dict = long_content.story_blueprint or {}
    voice_script = source_script.voice_script or ""

    # ── Resolve channel voice for TTS block ──────────────────────────────────
    channel_voice: ChannelVoice | None = (
        db.query(ChannelVoice)
        .filter(
            ChannelVoice.channel_id == channel.id,
            ChannelVoice.language == long_content.source_language,
        )
        .first()
    )

    # ── Step 1: Generate part plan (Haiku) ────────────────────────────────────
    plan: dict | None = None
    for attempt in (1, 2):
        try:
            plan = generate_shorts_plan(voice_script, blueprint, channel)
            break
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
            return
        except Exception as exc:
            logger.error(
                "run_shorts_planner: plan generation API error (%s) — skipping Shorts", exc
            )
            return

    if plan is None:
        return

    total_parts: int = plan["total_parts"]
    parts: list[dict] = plan["parts"]
    logger.info(
        "run_shorts_planner: plan generated for content %s — %d parts", long_content_id, total_parts
    )

    # ── Idempotency guard: skip if child Short episodes already exist ──────────
    _existing_count: int = (
        db.query(Content)
        .filter(
            Content.parent_content_id == long_content_id,
            Content.is_short_episode.is_(True),
        )
        .count()
    )
    if _existing_count > 0:
        _existing_shorts: list[Content] = (
            db.query(Content)
            .filter(
                Content.parent_content_id == long_content_id,
                Content.is_short_episode.is_(True),
            )
            .all()
        )
        _status_counts: dict[str, int] = {}
        for _s in _existing_shorts:
            _status_counts[_s.status] = _status_counts.get(_s.status, 0) + 1
        logger.info(
            "STANDALONE_SHORTS_ALREADY_EXIST parent_content_id=%s count=%d statuses=%s",
            long_content_id, _existing_count, _status_counts,
        )
        return

    # ── Step 2: Generate one Short episode per part ───────────────────────────
    for part_plan in parts:
        part_n = part_plan.get("part", 0)
        # Inject total_parts so generate_short_episode_script can reference it
        part_plan_with_total = {**part_plan, "_total_parts": total_parts}

        # Create Content row for this Short episode
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
        db.flush()  # populate short_content.id

        logger.info(
            "run_shorts_planner: created Content %s for part %d/%d",
            short_content.id, part_n, total_parts,
        )

        # Script generation with TTS + hook correction loop
        generated: dict | None = None
        _tts_majors: list[dict] = []
        for correction_round in range(1, _MAX_SHORT_CORRECTION_ROUNDS + 2):
            try:
                result = generate_short_episode_script(
                    part_plan=part_plan_with_total,
                    long_voice_script=voice_script,
                    blueprint=blueprint,
                    channel=channel,
                    channel_voice=channel_voice,
                    override_instruction="" if correction_round == 1 else (
                        f"Fix these issues from the previous attempt: "
                        f"{'; '.join(i['description'] for i in _tts_majors[:3])}"
                    ),
                )
            except Exception as exc:
                logger.error(
                    "run_shorts_planner: script error for part %d attempt %d: %s",
                    part_n, correction_round, exc,
                )
                break

            ep_voice_script = result.get("voice_script", "")

            # TTS compliance — same rules as long-form scripts
            tts_issues = check_tts_compliance(ep_voice_script, long_content.source_language)

            # First-sentence hook check: Short scripts are flat narration with no
            # [INTRO] marker, so we wrap the first sentence in a synthetic prefix to
            # let check_hook_quality() locate it without modifying the check itself.
            first_sent = (
                re.split(r"(?<=[.!?])\s+", ep_voice_script.strip())[0]
                if ep_voice_script.strip() else ""
            )
            hook_issues = check_hook_quality(
                f"[INTRO]\n{first_sent}", long_content.source_language
            )

            _tts_majors = [
                i for i in tts_issues + hook_issues
                if i["severity"] == "MAJOR"
            ]

            # Word count ceiling — enforced in code regardless of prompt compliance
            ep_wc = len(ep_voice_script.split())
            if ep_wc > _MAX_SHORT_WORDS:
                _tts_majors.append({
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
                    part_n, correction_round, ep_wc, _MAX_SHORT_WORDS,
                )

            if not _tts_majors:
                generated = result
                break

            if correction_round > _MAX_SHORT_CORRECTION_ROUNDS:
                logger.warning(
                    "run_shorts_planner: part %d still has MAJOR issues after %d round(s) — "
                    "using latest version",
                    part_n, _MAX_SHORT_CORRECTION_ROUNDS,
                )
                generated = result
                break

            logger.info(
                "run_shorts_planner: part %d retry %d — %d MAJOR issue(s): %s",
                part_n, correction_round, len(_tts_majors),
                [i["category"] for i in _tts_majors],
            )

        if generated is None:
            # Script generation completely failed — clean up the Content row
            db.delete(short_content)
            db.commit()
            logger.error(
                "run_shorts_planner: part %d script generation failed — content row removed",
                part_n,
            )
            continue

        # Persist Script
        short_script = Script(
            content_id=short_content.id,
            language=long_content.source_language,
            video_script=generated.get("voice_script", ""),  # same as voice_script for shorts
            voice_script=generated.get("voice_script", ""),
            version=1,
            validated=True,
        )
        db.add(short_script)

        # Update title and set awaiting-parent status.
        # Audio generation is gated behind the parent reaching AUDIO_DONE —
        # pickup_short_episodes_awaiting_parent() flips this to SCRIPTS_VALIDATED
        # once the parent's audio is complete, releasing the Short into Agent 3 audio.
        short_content.title = generated.get("title", short_content.title)
        short_content.status = "SCRIPTS_VALIDATED_AWAITING_PARENT"
        db.commit()

        logger.info(
            "run_shorts_planner: part %d/%d SCRIPTS_VALIDATED_AWAITING_PARENT — content=%s",
            part_n, total_parts, short_content.id,
        )
