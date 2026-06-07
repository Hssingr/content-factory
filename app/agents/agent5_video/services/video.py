"""Agent 5 — Video Generation orchestration service.

Orchestrates the full per-language video pipeline:
  1. Storyboard Agent        — Claude designs visual beats from the narration and
                               real Whisper timestamps (falls back to the legacy
                               Section Splitter + Section Validator if storyboard
                               generation fails or returns no usable beats)
  2. Save video_sections     — persist to DB
  3. Stock fetcher           — fetch actual media URLs per beat/section
  4. Media Validation Agent  — Claude reviews fetched media, replacement loop
                               (storyboard beats only — max 2 passes)
  4b. Repetition detection   — Python-side anti-slideshow guard: flags repeated/
                               near-repeated visuals (keyword families, duplicate
                               media, consecutive same-subject queries) and
                               re-fetches them via each beat's fallback_query
  5. Assembly Validator      — validate overall assembly quality (Claude, 1 pass)
  6. Shorts Cutter           — group sections into Short segments
  7. Subtitles generator     — standard (main) + karaoke (Shorts) from Whisper timestamps
  7b. Viewer Experience      — Claude reviews the final plan as a real viewer would
      Validator                (intro, script, visuals, captions, audio, pacing);
                               one deterministic repair pass, then skip render if
                               still NEEDS_FIXES
  8. Remotion builder        — write JSON props files
  9. Remotion renderer       — call Remotion CLI, save VideoRender records

Re-entrancy — each phase is skipped when its output already exists:
  • Main MP4 on disk + VideoRender in DB  → language fully done, skip all
  • Props JSON on disk                    → skip steps 1-8, go directly to render
  • Sections in DB                        → skip steps 1-3, go to stock fetch

Status transitions:
  AUDIO_DONE       → GENERATING_VIDEO  (set at start, guards against double-processing)
  GENERATING_VIDEO → VIDEO_DONE        (set on full success)
  GENERATING_VIDEO → FAILED            (set if all languages fail)
"""

import json
import logging
import re
import uuid
from collections import Counter
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AudioFile, Channel, ChannelConfig, Content, Script, VideoRender, VideoSection,
)
from app.agents.agent5_video.subagents.section_splitter import split_into_sections
from app.agents.agent5_video.subagents.section_validator import validate_sections
from app.agents.agent5_video.subagents.storyboard import split_into_beats
from app.agents.agent5_video.subagents.media_validator import validate_and_replace_media
from app.agents.agent5_video.subagents.assembly_validator import validate_assembly
from app.agents.agent5_video.subagents.shorts_cutter import cut_shorts
from app.agents.agent5_video.services.stock_fetcher import (
    fetch_all_sections, fetch_all_beats, fetch_for_beat, fetch_for_section,
)
from app.agents.agent5_video.services.subtitles import (
    build_standard_subtitles, build_karaoke_subtitles,
)
from app.agents.agent5_video.services.remotion_builder import build_main_props, build_short_props
from app.agents.agent5_video.services.renderer import render_main_video, render_short
from app.agents.agent5_video.system_prompt import assess_viewer_experience

logger = logging.getLogger(__name__)

# ── Visual repetition detection (anti-slideshow guard, runs before assembly) ──
# Keyword families grouped from the user-reported "repetitive dark corridor
# slideshow" symptom — overused generic b-roll defaults that read as filler.
_REPETITION_KEYWORD_FAMILIES: list[set[str]] = [
    {"corridor", "hallway"},
    {"dark room", "empty room"},
    {"forest"},
    {"office"},
    {"silhouette", "shadow"},
]
_REPETITION_WINDOW = 5      # how many nearby beats count as "nearby"
_REPETITION_THRESHOLD = 3   # 3+ nearby beats from the same family → force replacement

_SUBJECT_WORD_RE = re.compile(r"[a-zà-öø-ÿ0-9']+", re.IGNORECASE)
_SUBJECT_STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "to", "for", "from"}

_MARKER_STRIP_RE = re.compile(r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$", re.IGNORECASE | re.MULTILINE)

_MAX_VIEWER_REPAIR_PASSES = 1


def _script_hook(voice_script: str, length: int = 300) -> str:
    """Return the narration opening with timing markers stripped, for quality prompts."""
    return _MARKER_STRIP_RE.sub("", voice_script or "").strip()[:length]


def _keyword_family(query: str) -> frozenset[str] | None:
    """Return the overused-keyword family a search query matches, if any."""
    q = query.lower()
    for family in _REPETITION_KEYWORD_FAMILIES:
        if any(keyword in q for keyword in family):
            return frozenset(family)
    return None


def _subject(query: str) -> str:
    """Extract the first two significant words of a search query as its 'subject'."""
    words = [w for w in _SUBJECT_WORD_RE.findall(query.lower()) if w not in _SUBJECT_STOPWORDS]
    return " ".join(words[:2])


def _detect_and_fix_repetition(sections: list[dict]) -> int:
    """Detect repeated/near-repeated visuals and re-fetch them before final assembly.

    This is the deterministic, Python-side complement to the Media Validation
    Agent — a last guard against the "repetitive dark corridor slideshow" failure
    mode, using fixed rules so detection is repeatable:

      1. Keyword-family repetition: if ``_REPETITION_THRESHOLD`` or more beats
         within a sliding window of ``_REPETITION_WINDOW`` use a search_query from
         the same overused family (corridor/hallway, dark/empty room, forest,
         office, silhouette/shadow), the middle/later beats are flagged.
      2. Duplicate media: the same fetched ``media_url`` reused across beats.
      3. Consecutive same-subject queries: back-to-back beats whose search_query
         shares the same first two significant words.

    Flagged beats are repaired by swapping in their own Claude-authored
    ``fallback_query`` (a deliberately different visual angle) and re-fetching —
    no extra Claude calls needed.

    Args:
        sections: Fully fetched, media-validated beat/section dicts (mutated in place).

    Returns:
        Number of beats that were flagged and successfully re-fetched.
    """
    flagged: set[int] = set()
    family_hits: Counter = Counter()

    families = [_keyword_family(s.get("search_query", "")) for s in sections]
    for i in range(len(sections)):
        window = families[i:i + _REPETITION_WINDOW]
        counts = Counter(f for f in window if f is not None)
        for family, count in counts.items():
            if count >= _REPETITION_THRESHOLD:
                matches = [i + j for j, f in enumerate(window) if f == family]
                for idx in matches[1:]:
                    flagged.add(idx)
                family_hits[family] += 1

    seen_urls: dict[str, int] = {}
    for i, s in enumerate(sections):
        url = s.get("media_url", "")
        if not url:
            continue
        if url in seen_urls:
            flagged.add(i)
        else:
            seen_urls[url] = i

    subjects = [_subject(s.get("search_query", "")) for s in sections]
    for i in range(1, len(sections)):
        if subjects[i] and subjects[i] == subjects[i - 1]:
            flagged.add(i)

    if family_hits:
        top = ", ".join(f"{'/'.join(sorted(family))}" for family in family_hits)
        logger.info("Repetition detection: overused keyword families flagged — %s", top)

    if not flagged:
        logger.info("Repetition detection: no repeated/near-repeated visuals found")
        return 0

    fixed = 0
    for idx in sorted(flagged):
        s = sections[idx]
        fallback = (s.get("fallback_query") or "").strip()
        if not fallback or fallback.lower() == s.get("search_query", "").lower():
            continue
        old_url = s.get("media_url", "")
        s["search_query"] = fallback
        if "visual_intent" in s:
            fetch_for_beat(s)
        else:
            fetch_for_section(s)
        fixed += 1
        logger.info(
            "Repetition fix: beat %s — query=%r old_media=%s new_media=%s",
            s.get("beat_order", s.get("section_order", idx)), fallback,
            old_url[:60], s.get("media_url", "")[:60],
        )

    logger.info(
        "Repetition detection complete: %d beat(s) flagged, %d re-fetched",
        len(flagged), fixed,
    )
    return fixed


def _check_viewer_experience(
    sections: list[dict],
    shorts: list[dict],
    standard_subs: list[dict],
    audio: AudioFile,
    channel: Channel,
    channel_style: str,
    script: Script,
    language: str,
) -> bool:
    """Run the final Viewer Experience Validator with one deterministic repair pass.

    The last quality gate before Remotion rendering — reviews the fully assembled
    plan (script hook, visual sequence, Shorts, captions) the way a real viewer
    would experience it, distinct from the Assembly/Media validators which check
    technical relevance per-beat.

    If the verdict is NEEDS_FIXES, runs ``_detect_and_fix_repetition`` — the only
    deterministic Python-side repair available at this stage — which mutates
    ``sections`` in place; ``shorts[*]["sections"]`` hold references to the same
    dicts (confirmed in shorts_cutter.cut_shorts), so the fix propagates without
    rebuilding anything. Then re-checks once. If still NEEDS_FIXES, the language
    is marked as not render-ready so a bad video is never published.

    Returns:
        True if the plan is approved, or if the check itself could not run
        (fail-open — a Claude outage must never block rendering). False if the
        plan still needs fixes after the repair pass — skip rendering this language.
    """
    total_words = sum(len(c.get("text", "").split()) for c in standard_subs)
    avg_words   = total_words / len(standard_subs) if standard_subs else 0.0
    hook        = _script_hook(script.voice_script)

    for attempt in range(1, _MAX_VIEWER_REPAIR_PASSES + 2):
        try:
            review = assess_viewer_experience(
                sections=sections,
                shorts_count=len(shorts),
                caption_count=len(standard_subs),
                avg_caption_words=avg_words,
                total_duration_ms=audio.duration_ms,
                channel_niche=channel.niche or "",
                channel_tone=channel.tone or "",
                channel_style=channel_style,
                script_hook=hook,
            )
        except Exception as exc:
            logger.error(
                "Viewer Experience Validator failed (attempt %d, language=%s): %s — proceeding without check",
                attempt, language, exc,
            )
            return True

        status = review.get("status", "APPROVED")
        issues = review.get("blocking_issues", [])
        logger.info(
            "Viewer Experience Validator: status=%s issues=%d attempt=%d language=%s — %s",
            status, len(issues), attempt, language, review.get("overall_comment", ""),
        )
        for issue in issues:
            logger.warning(
                "Viewer experience issue [%s]: %s -> %s",
                issue.get("category", "?"), issue.get("issue", ""), issue.get("fix", ""),
            )

        if status == "APPROVED" or not issues:
            return True

        if attempt > _MAX_VIEWER_REPAIR_PASSES:
            break

        logger.info(
            "Viewer Experience Validator: running repair pass (repetition fix) for language=%s",
            language,
        )
        _detect_and_fix_repetition(sections)

    logger.error(
        "Viewer Experience Validator: still NEEDS_FIXES after repair pass for language=%s — skipping render",
        language,
    )
    return False


def run_video_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 5 video pipeline for one piece of content.

    Processes each language independently. A single-language failure is logged
    and skipped — the pipeline continues for remaining languages.
    Re-entrant: already-completed phases are detected and skipped automatically.

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
    runway_enabled      = config.runway_enabled               if config else False
    channel_style       = config.video_style_type             if config else "documentary"
    channel_color_grade = config.video_color_grade            if config else "desaturated"
    karaoke_color       = config.subtitle_karaoke_active_color if config else "#FFD700"
    shorts_label_style  = config.shorts_part_label_style      if config else "default"
    script_format       = config.script_format                if config else "youtube_long"

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
            logger.warning(
                "No audio file for language=%s, content=%s — skipping", language, content_id
            )
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
                script_format=script_format,
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
        logger.info(
            "Video generation complete for content %s (%d language(s))", content_id, successful
        )
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
    script_format: str,
    db: Session,
) -> bool:
    """Run the video pipeline for one language, skipping already-completed phases.

    Returns:
        True on success, False on any critical failure.
    """
    cid_str    = str(content_id)
    media_root = Path(settings.media_path).resolve()
    props_dir  = media_root / "remotion_props"

    logger.info("Agent 5: language=%s for content %s", language, content_id)

    # ── Phase check 1: already fully rendered? ─────────────────────────────────
    if _is_rendered(content_id, language, cid_str, media_root, db):
        logger.info("language=%s already rendered — skipping", language)
        return True

    # ── Phase check 2: props on disk → skip steps 1-8 ─────────────────────────
    main_props_file = props_dir / f"{cid_str}_{language}_main.json"
    if main_props_file.exists():
        if _props_contain_uhd_url(main_props_file):
            logger.warning(
                "Props file for language=%s contains UHD URL — deleting and regenerating",
                language,
            )
            for stale in props_dir.glob(f"{cid_str}_{language}_*.json"):
                stale.unlink(missing_ok=True)
        else:
            logger.info("Props found on disk for language=%s — skipping to render", language)
            return _render_from_existing_props(
                content_id, language, audio, cid_str, props_dir, db
            )

    # ── Phase check 3: sections in DB → skip steps 1-2 ────────────────────────
    db_sections = _load_sections_from_db(content_id, language, db)
    if db_sections:
        logger.info(
            "Sections already in DB for language=%s (%d) — skipping to stock fetch",
            language, len(db_sections),
        )
        sections = db_sections
        using_storyboard = any(s.get("visual_intent") for s in sections)
    else:
        # ── 1. Storyboard Agent (preferred) ───────────────────────────────────
        # Claude designs visual beats from the narration; Python deterministically
        # maps them onto real Whisper timestamps (storyboard.split_into_beats).
        beats = split_into_beats(
            voice_script=script.voice_script,
            duration_ms=audio.duration_ms,
            channel=channel,
            script_format=script_format,
            whisper_transcript=audio.whisper_transcript or [],
        )
        using_storyboard = beats is not None

        if using_storyboard:
            sections = beats
            overlay_count = sum(1 for b in sections if b.get("visual_type") == "text_overlay")
            logger.info(
                "Storyboard flow: %d beat(s), %d text_overlay beat(s) for language=%s",
                len(sections), overlay_count, language,
            )
        else:
            # ── Fallback: legacy Section Splitter → Section Validator ─────────
            logger.warning(
                "Storyboard unavailable for language=%s — falling back to legacy section splitter",
                language,
            )
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

            sections = validate_sections(
                sections=sections,
                channel_niche=channel.niche or "",
                channel_tone=channel.tone or "",
                runway_enabled=runway_enabled,
            )

        # ── 2. Save video_sections to DB ──────────────────────────────────────
        _save_video_sections(content_id, language, sections, db)
        db.commit()   # commit now — render failures must not roll sections back

    # ── 3. Stock fetcher ──────────────────────────────────────────────────────
    sections = fetch_all_beats(sections) if using_storyboard else fetch_all_sections(sections)

    # ── 4. Media Validation Agent + replacement loop (storyboard beats only) ──
    if using_storyboard:
        sections = validate_and_replace_media(
            beats=sections,
            channel_niche=channel.niche or "",
            channel_tone=channel.tone or "",
            script_format=script_format,
        )

    # ── 4b. Visual repetition detection (anti-slideshow guard) ────────────────
    _detect_and_fix_repetition(sections)

    # ── 5. Final Assembly Validation ──────────────────────────────────────────
    sections = validate_assembly(
        sections=sections,
        total_duration_ms=audio.duration_ms,
        channel_niche=channel.niche or "",
        channel_tone=channel.tone or "",
        channel_style=channel_style,
    )

    # ── 6. Shorts Cutter ──────────────────────────────────────────────────────
    shorts = cut_shorts(
        sections=sections,
        shorts_breakpoints=audio.shorts_breakpoints or [],
        language=language,
        label_style=shorts_label_style,
    )

    # ── 7. Subtitles ──────────────────────────────────────────────────────────
    whisper       = audio.whisper_transcript or []
    standard_subs = build_standard_subtitles(whisper)
    karaoke_subs  = build_karaoke_subtitles(whisper, active_color=karaoke_color)

    # ── 7b. Viewer Experience Validator (final pre-render gate) ───────────────
    if not _check_viewer_experience(
        sections=sections,
        shorts=shorts,
        standard_subs=standard_subs,
        audio=audio,
        channel=channel,
        channel_style=channel_style,
        script=script,
        language=language,
    ):
        return False

    # ── 8. Remotion builder ───────────────────────────────────────────────────
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

    short_props_pairs: list[tuple[dict, str]] = []
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
        short_props_pairs.append((short, path))

    # ── 9. Remotion renderer ──────────────────────────────────────────────────
    _run_renders(
        content_id=content_id,
        language=language,
        cid_str=cid_str,
        audio=audio,
        main_props_path=main_props_path,
        short_props_pairs=short_props_pairs,
        db=db,
    )
    return True


# ── Phase-skip helpers ─────────────────────────────────────────────────────────

def _is_rendered(
    content_id: uuid.UUID,
    language: str,
    cid_str: str,
    media_root: Path,
    db: Session,
) -> bool:
    """Return True if the main MP4 exists on disk AND a VideoRender row is in DB."""
    row = (
        db.query(VideoRender)
        .filter(
            VideoRender.content_id == content_id,
            VideoRender.language == language,
            VideoRender.format == "main",
        )
        .first()
    )
    if not row:
        return False
    mp4 = media_root / "video" / cid_str / f"{language}_main.mp4"
    return mp4.exists()


def _load_sections_from_db(
    content_id: uuid.UUID, language: str, db: Session
) -> list[dict]:
    """Load VideoSection rows as dicts compatible with the stock fetcher.

    Storyboard beats persist their extra fields (visual_intent, visual_type,
    visual_category, avoid_reason, fallback_query, transition_to_next, overlay_text,
    overlay_position, priority, section_marker) as JSON in the otherwise-unused
    ``generation_prompt`` column.
    They are deserialized back here so a re-entrant run keeps using the storyboard
    flow (beat-aware fetch, media validation loop, ...) instead of falling back.
    """
    rows = (
        db.query(VideoSection)
        .filter(
            VideoSection.content_id == content_id,
            VideoSection.language == language,
        )
        .order_by(VideoSection.section_order)
        .all()
    )
    result = []
    for s in rows:
        section = {
            "section_order":   s.section_order,
            "beat_order":      s.section_order,
            "script_text":     s.script_text,
            "audio_start_ms":  s.audio_start_ms,
            "audio_end_ms":    s.audio_end_ms,
            "duration_sec":    (s.audio_end_ms - s.audio_start_ms) / 1000,
            "visual_source":   s.visual_source,
            "search_query":    s.search_query or "",
            "suggested_visual": "b-roll",
            "effect":          s.effect or "slow_zoom",
            "color_grade":     s.color_grade or "desaturated",
            "validation_status": "PASS",
            "subagent_rounds": s.subagent_rounds,
            "best_attempt_used": s.best_attempt_used,
        }

        if s.generation_prompt:
            try:
                extras = json.loads(s.generation_prompt)
            except (json.JSONDecodeError, TypeError):
                extras = None
            if isinstance(extras, dict) and "visual_intent" in extras:
                section.update(extras)

        result.append(section)
    return result


def _render_from_existing_props(
    content_id: uuid.UUID,
    language: str,
    audio: AudioFile,
    cid_str: str,
    props_dir: Path,
    db: Session,
) -> bool:
    """Render main + all shorts from props files that are already on disk.

    Skips any individual render whose VideoRender row already exists in DB.
    """
    main_props_path = str(props_dir / f"{cid_str}_{language}_main.json")

    # Main render
    if not _render_exists(content_id, language, "main", None, db):
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
        db.commit()
    else:
        logger.info("Main render already done for language=%s — skipping", language)

    # Shorts: discover from existing props files
    short_prop_files = sorted(
        props_dir.glob(f"{cid_str}_{language}_short_*.json"),
        key=lambda p: int(p.stem.rsplit("_", 1)[1]),
    )
    for sp_path in short_prop_files:
        short_index = int(sp_path.stem.rsplit("_", 1)[1])

        if _render_exists(content_id, language, "short", short_index, db):
            logger.info(
                "Short %d render already done for language=%s — skipping",
                short_index, language,
            )
            continue

        sp = json.loads(sp_path.read_text())
        duration_ms = sp.get("duration_ms", 0)

        short_result = render_short(
            content_id=cid_str,
            language=language,
            short_index=short_index,
            props_path=str(sp_path),
            duration_ms=duration_ms,
            hook_modified=True,
        )
        db.add(VideoRender(
            content_id=content_id,
            language=language,
            format="short",
            short_order=short_index,
            duration_seconds=short_result["duration_seconds"],
            hook_modified=True,
            render_time_seconds=short_result["render_time_seconds"],
        ))
        db.commit()

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
    short_props_pairs: list[tuple[dict, str]],
    db: Session,
) -> None:
    """Render main video + all shorts, committing each VideoRender row individually."""
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
    db.commit()

    for short, props_path in short_props_pairs:
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
        "language=%s done: 1 main + %d short(s) for content %s",
        language, len(short_props_pairs), content_id,
    )


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _props_contain_uhd_url(props_file: Path) -> bool:
    """Return True if any URL in the props JSON exceeds FHD resolution.

    Checks for known UHD/4K filename patterns that crash Remotion's
    OffthreadVideo proxy.  Fast string scan — no full JSON parse needed.
    """
    try:
        raw = props_file.read_text()
        uhd_markers = ("_4096_", "_2160_", "_3840_", "_uhd_", "_4k_", "2160p", "4096p")
        return any(m in raw for m in uhd_markers)
    except Exception:
        return False


def _save_video_sections(
    content_id: uuid.UUID,
    language: str,
    sections: list[dict],
    db: Session,
) -> None:
    """Persist validated sections (or storyboard beats) to video_sections (upsert by order).

    Storyboard-beat-only fields (visual_intent, visual_type, visual_category,
    avoid_reason, fallback_query, transition_to_next, overlay_text, overlay_position,
    priority, section_marker) are JSON-serialized into the otherwise-unused
    ``generation_prompt`` column so
    they survive re-entrancy without requiring a schema migration. Legacy sections
    (no ``visual_intent``) store ``generation_prompt=None`` as before.
    """
    db.query(VideoSection).filter(
        VideoSection.content_id == content_id,
        VideoSection.language   == language,
    ).delete()

    for s in sections:
        is_beat = "visual_intent" in s
        generation_prompt = json.dumps(_beat_extras(s), ensure_ascii=False) if is_beat else None

        db.add(VideoSection(
            content_id=content_id,
            language=language,
            section_order=s["section_order"],
            script_text=s.get("script_text", ""),
            audio_start_ms=s.get("audio_start_ms", 0),
            audio_end_ms=s.get("audio_end_ms", 0),
            visual_source=s.get("visual_source", "pexels"),
            search_query=s.get("search_query"),
            generation_prompt=generation_prompt,
            effect=s.get("effect"),
            color_grade=s.get("color_grade"),
            runway_used=s.get("visual_source") == "runway",
            subagent_rounds=s.get("subagent_rounds", 1),
            best_attempt_used=s.get("best_attempt_used", False),
        ))

    db.flush()   # caller commits after returning
    logger.info(
        "Saved %d video section(s) for language=%s, content=%s",
        len(sections), language, content_id,
    )


def _beat_extras(section: dict) -> dict:
    """Collect storyboard-beat-only fields for JSON storage in generation_prompt."""
    return {
        "section_marker":     section.get("section_marker", ""),
        "visual_intent":      section.get("visual_intent", ""),
        "visual_type":        section.get("visual_type", "b-roll"),
        "visual_category":    section.get("visual_category", "place"),
        "avoid_reason":       section.get("avoid_reason", ""),
        "fallback_query":     section.get("fallback_query", ""),
        "transition_to_next": section.get("transition_to_next", "cut"),
        "overlay_text":       section.get("overlay_text", ""),
        "overlay_position":   section.get("overlay_position", "none"),
        "priority":           section.get("priority", "essential"),
    }
