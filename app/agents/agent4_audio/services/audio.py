import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.models import (
    AudioFile, Channel, ChannelConfig, ChannelVoice,
    Content, Script,
)
from app.agents.agent4_audio.services.tts import generate_audio
from app.agents.agent4_audio.services.storage import audio_path, save_audio
from app.agents.agent4_audio.services.whisper import transcribe
from app.agents.agent4_audio.services.breakpoints import recalculate_breakpoints
from app.services.claude_client import call_claude, parse_claude_json

logger = logging.getLogger(__name__)

# ── Short bookend constants ───────────────────────────────────────────────────

# Claude prompt for per-cut rehook + bridge CTA generation.
# Agent 4 calls Claude only for this specific step — context-aware text that
# a hardcoded template cannot produce.
# max_tokens raised 150 → 300 so complex languages (DE/IT) are not truncated.
_BOOKEND_SYSTEM_PROMPT = """\
You write ultra-short hook and bridge texts for individual Shorts in a multi-part series.

You receive the narrative context immediately before and after a specific cut point,
the language code to write in, and which part number the cut creates.

Your task: produce one "rehook" and one "bridge_cta" for this exact cut.

rehook (≤10 words, in the specified language):
- Grabs a viewer who starts watching from this part with no prior context
- References something SPECIFIC from the narrative BEFORE the cut — not generic
- Must feel urgent and personal to THIS story
- Forbidden first words: "In", "Today", "Welcome", "Imagine", "Have you", "Did you know"

bridge_cta (≤12 words, in the specified language):
- Tells the viewer to follow for the next part
- Teases something SPECIFIC that will be revealed from the narrative AFTER the cut
- Not generic ("follow for more") — name the specific next reveal

Respond with compact JSON only, no whitespace formatting. No markdown. No code fence. No extra keys.
{"rehook":"...","bridge_cta":"..."}\
"""

_BOOKEND_MAX_TOKENS = 300   # raised from 150 — complex-language output was being truncated

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# ── Bookend entry shims ───────────────────────────────────────────────────────
# Bookend JSONB entries changed from str → {"path": str, "duration_ms": int}.
# These shims handle both shapes so existing DB rows stay readable.

def _bookend_path(entry) -> str | None:
    """Extract the file path from a bookend JSONB entry (str or dict)."""
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get("path")
    return entry   # legacy: bare string path


def _bookend_duration(entry) -> int:
    """Extract the duration_ms from a bookend JSONB entry (0 for legacy str entries)."""
    if entry is None or isinstance(entry, str):
        return 0
    return int(entry.get("duration_ms", 0))


def _is_valid_mp3(path: str) -> bool:
    """Return True if path exists and is a readable mp3 file with nonzero length."""
    try:
        from mutagen.mp3 import MP3
        return MP3(path).info.length > 0
    except Exception:
        return False


def _extract_whisper_context(
    transcript: list[dict],
    breakpoint_ms: int,
) -> tuple[str, str]:
    """Extract narrative context around a Shorts cut point from Whisper word timestamps.

    Args:
        transcript:    Whisper word list: [{"word": str, "start": float, "end": float}, …].
        breakpoint_ms: Cut point in milliseconds.

    Returns:
        ``(before_context, after_context)`` — last 3 sentences before and first 3 after.
    """
    if not transcript:
        return "", ""

    cut_sec = breakpoint_ms / 1000.0

    before_words = [w.get("word", "") for w in transcript if w.get("end", 0) <= cut_sec]
    after_words  = [w.get("word", "") for w in transcript if w.get("start", 0) > cut_sec]

    before_sents = _SENT_SPLIT_RE.split(" ".join(before_words).strip())
    after_sents  = _SENT_SPLIT_RE.split(" ".join(after_words).strip())

    before_ctx = " ".join(s for s in before_sents[-3:] if s).strip()
    after_ctx  = " ".join(s for s in after_sents[:3]  if s).strip()

    return before_ctx, after_ctx


def generate_short_bookends(
    content_id: uuid.UUID,
    language: str,
    audio_file: AudioFile,
    channel_voice: ChannelVoice,
    db: Session,
) -> None:
    """Generate per-Short rehook + bridge CTA audio via Claude + ElevenLabs.

    For each breakpoint in ``audio_file.shorts_breakpoints`` (index i):
      1. Extract Whisper context: last 3 sentences before + first 3 after the cut.
      2. Call Claude (max_tokens=150) → unique ``rehook`` for Short i+1 and
         unique ``bridge_cta`` for Short i, both grounded in the cut's actual content.
      3. Synthesise both via ElevenLabs using the channel voice.
      4. Save mp3 files; build indexed path lists (None where a clip is not needed).
      5. Persist as JSONB lists on ``audio_file.short_rehook_paths`` /
         ``audio_file.short_bridge_paths`` (caller commits).

    Index convention (n_shorts = len(breakpoints) + 1):
      - ``short_rehook_paths[0]`` = None  (Part 1 needs no rehook)
      - ``short_rehook_paths[i+1]`` = path for Short starting at breakpoints[i]
      - ``short_bridge_paths[-1]`` = None (last Short needs no bridge)
      - ``short_bridge_paths[i]``   = path for Short ending at breakpoints[i]

    Skipped silently when ``audio_file.shorts_breakpoints`` is empty or None.
    Per-cut failures are warned and skipped — other cuts and the main record are
    unaffected (the main AudioFile is already committed before this runs).

    Args:
        content_id:    UUID of the content item.
        language:      BCP-47 language code (e.g. "fr", "en").
        audio_file:    AudioFile ORM object — provides ``shorts_breakpoints`` and
                       ``whisper_transcript``; updated in place with bookend paths.
        channel_voice: ChannelVoice providing voice_id and model settings.
        db:            SQLAlchemy session (caller commits after this returns).
    """
    breakpoints: list[int] = audio_file.shorts_breakpoints or []
    if not breakpoints:
        logger.info(
            "No Shorts breakpoints for content=%s lang=%s — skipping bookends",
            content_id, language,
        )
        return

    transcript: list[dict] = audio_file.whisper_transcript or []
    n_shorts    = len(breakpoints) + 1
    bookend_dir = audio_path(content_id, language).parent
    bookend_dir.mkdir(parents=True, exist_ok=True)

    # One slot per Short; None where a clip is not applicable.
    rehook_paths: list[str | None] = [None] * n_shorts   # Short 0 has no rehook
    bridge_paths: list[str | None] = [None] * n_shorts   # Last Short has no bridge

    for i, bp_ms in enumerate(breakpoints):
        rehook_file = bookend_dir / f"{language}_rehook_{i + 1}.mp3"
        bridge_file = bookend_dir / f"{language}_bridge_{i}.mp3"

        # Re-entrancy guard — skip if valid files already exist (e.g. task retry)
        rehook_exists = _is_valid_mp3(str(rehook_file))
        bridge_exists = _is_valid_mp3(str(bridge_file))
        if rehook_exists and bridge_exists:
            logger.info(
                "Bookend cut=%d lang=%s: both files exist — skipping (re-entrant skip)", i, language
            )
            # Populate paths with existing dict entries so the persist step is correct
            from mutagen.mp3 import MP3 as _MP3
            rehook_paths[i + 1] = {
                "path": str(rehook_file),
                "duration_ms": int(_MP3(str(rehook_file)).info.length * 1000),
            }
            bridge_paths[i] = {
                "path": str(bridge_file),
                "duration_ms": int(_MP3(str(bridge_file)).info.length * 1000),
            }
            continue

        before_ctx, after_ctx = _extract_whisper_context(transcript, bp_ms)

        user_msg = (
            f"Language: {language}\n"
            f"This cut creates: Short {i + 1} of {n_shorts} "
            f"(Short {i} ends here, Short {i + 1} starts here)\n\n"
            f"Last 3 sentences BEFORE the cut:\n{before_ctx or '(no context available)'}\n\n"
            f"First 3 sentences AFTER the cut:\n{after_ctx or '(no context available)'}"
        )

        try:
            raw    = call_claude(_BOOKEND_SYSTEM_PROMPT, user_msg, max_tokens=_BOOKEND_MAX_TOKENS, task="bookends")
            result = parse_claude_json(
                raw,
                required_keys=["rehook", "bridge_cta"],
                type_checks={"rehook": str, "bridge_cta": str},
            )
            rehook_text = result["rehook"].strip()
            bridge_text = result["bridge_cta"].strip()
        except Exception as exc:
            logger.warning(
                "Claude bookend generation failed cut=%d lang=%s: %s — skipping cut",
                bp_ms, language, exc,
            )
            continue

        # Rehook for Short i+1 — skip synthesis if file already valid
        if not rehook_exists:
            try:
                from mutagen.mp3 import MP3 as _MP3
                rehook_bytes = generate_audio(rehook_text, channel_voice)
                rehook_file.write_bytes(rehook_bytes)
                rehook_duration_ms = int(_MP3(str(rehook_file)).info.length * 1000)
                rehook_paths[i + 1] = {"path": str(rehook_file), "duration_ms": rehook_duration_ms}
                logger.info(
                    "Rehook %d saved: %s (%d bytes, %d ms)",
                    i + 1, rehook_file, len(rehook_bytes), rehook_duration_ms,
                )
            except Exception as exc:
                logger.warning("Rehook synthesis failed cut=%d lang=%s: %s", bp_ms, language, exc)

        # Bridge CTA for Short i — skip synthesis if file already valid
        if not bridge_exists:
            try:
                from mutagen.mp3 import MP3 as _MP3
                bridge_bytes = generate_audio(bridge_text, channel_voice)
                bridge_file.write_bytes(bridge_bytes)
                bridge_duration_ms = int(_MP3(str(bridge_file)).info.length * 1000)
                bridge_paths[i] = {"path": str(bridge_file), "duration_ms": bridge_duration_ms}
                logger.info(
                    "Bridge %d saved: %s (%d bytes, %d ms)",
                    i, bridge_file, len(bridge_bytes), bridge_duration_ms,
                )
            except Exception as exc:
                logger.warning("Bridge synthesis failed cut=%d lang=%s: %s", bp_ms, language, exc)

    audio_file.short_rehook_paths = rehook_paths
    audio_file.short_bridge_paths = bridge_paths
    db.flush()


def run_audio_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 4 audio pipeline for one piece of content.

    For every validated script language:
      1. Look up the voice_id + emotion from channel_voices
      2. Generate TTS audio via the configured provider (channel_voice.provider;
         model from channel_voice.tts_model; chunked at [SECTION N] boundaries)
      3. Save the mp3 to disk and measure exact duration with mutagen
      4. Transcribe with OpenAI Whisper → word-level timestamps
      5. Recalculate Shorts breakpoints from real timestamps
      6. Persist AudioFile record; update Script.estimated_duration_sec
         and Script.shorts_breakpoints with real values

    Languages without a configured voice are skipped with a warning.
    Single-language failures are logged and skipped — the pipeline
    continues for the remaining languages.

    Args:
        content_id: UUID of content with status ``SCRIPTS_VALIDATED``.
        db:         SQLAlchemy session managed by the caller.

    Returns:
        ``True``  — at least one AudioFile was successfully generated.
        ``False`` — no AudioFile could be produced (all languages failed).
    """
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.error("Content %s not found", content_id)
        return False

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content_id)
        return False

    config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    shorts_rule = config.shorts_rule if config else "auto"

    # Build voice map: language → ChannelVoice
    voices: dict[str, ChannelVoice] = {
        v.language: v
        for v in db.query(ChannelVoice)
        .filter(ChannelVoice.channel_id == channel.id)
        .all()
    }

    # Load the latest validated script per language
    scripts = _load_latest_scripts(content_id, db)
    if not scripts:
        logger.warning("No validated scripts found for content %s", content_id)
        return False

    content.status = "GENERATING_AUDIO"
    db.commit()

    success_count = 0

    for lang, script in scripts.items():
        voice = voices.get(lang)
        if not voice or not voice.voice_id:
            logger.warning("No voice configured for lang=%s — skipping", lang)
            continue

        logger.info("Processing lang=%s voice_id=%s", lang, voice.voice_id)

        # ── Step 1: TTS (skip if file already on disk) ───────────────────────
        existing = audio_path(content_id, lang)
        try:
            if existing.exists():
                logger.info("Audio already on disk — skipping TTS for lang=%s", lang)
                file_path   = str(existing)
                from mutagen.mp3 import MP3
                duration_ms = int(MP3(file_path).info.length * 1000)
            else:
                audio_bytes             = generate_audio(script.voice_script, voice)
                file_path, duration_ms  = save_audio(content_id, lang, audio_bytes)

        except Exception as exc:
            logger.error("TTS/storage failed lang=%s: %s", lang, exc)
            db.rollback()
            continue

        # ── Step 3: Whisper transcription (soft — failure uses equal splits) ──
        transcript: list[dict] = []
        try:
            transcript = transcribe(file_path, language=lang)
        except Exception as exc:
            logger.warning(
                "Whisper failed lang=%s (%s) — continuing with equal-interval breakpoints",
                lang, exc,
            )

        try:
            # ── Step 4: Recalculate Shorts breakpoints ───────────────────────
            bp = recalculate_breakpoints(
                transcript, duration_ms, shorts_rule,
                voice_script=script.voice_script or "",
                language=lang,
            )

            # ── Step 5: Persist AudioFile ────────────────────────────────────
            audio_record = _upsert_audio_file(db, content_id, lang, file_path, duration_ms, bp, transcript)

            # ── Step 6: Update Script with real values ───────────────────────
            script.estimated_duration_sec = round(duration_ms / 1000, 1)
            script.shorts_breakpoints = bp

            db.commit()
            success_count += 1
            logger.info(
                "Audio done lang=%s: %.1fs | %d breakpoint(s) | %d whisper words",
                lang, duration_ms / 1000, len(bp), len(transcript),
            )

        except Exception as exc:
            logger.error("Breakpoints/persist failed lang=%s: %s", lang, exc)
            db.rollback()
            continue

        # ── Step 7: Short bookend clips (soft — failure does not fail the language) ──
        try:
            generate_short_bookends(
                content_id=content_id,
                language=lang,
                audio_file=audio_record,
                channel_voice=voice,
                db=db,
            )
            db.commit()
        except Exception as exc:
            logger.warning("Bookend generation failed lang=%s: %s — skipping", lang, exc)
            db.rollback()

    if success_count > 0:
        content.status = "AUDIO_DONE"
    else:
        content.status = "FAILED"
        logger.error("All languages failed for content %s", content_id)

    db.commit()
    logger.info(
        "run_audio_generation done for content %s: %d/%d language(s) succeeded → %s",
        content_id, success_count, len(scripts), content.status,
    )
    return success_count > 0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_latest_scripts(content_id: uuid.UUID, db: Session) -> dict[str, Script]:
    """Return the highest-version validated Script per language."""
    rows: list[Script] = (
        db.query(Script)
        .filter(Script.content_id == content_id, Script.validated.is_(True))
        .order_by(Script.language, Script.version.desc())
        .all()
    )
    latest: dict[str, Script] = {}
    for s in rows:
        if s.language not in latest:
            latest[s.language] = s
    return latest


def _upsert_audio_file(
    db: Session,
    content_id: uuid.UUID,
    language: str,
    file_path: str,
    duration_ms: int,
    shorts_breakpoints: list[int],
    whisper_transcript: list[dict],
) -> AudioFile:
    """Insert or update the AudioFile record for a content+language pair."""
    existing: AudioFile | None = (
        db.query(AudioFile)
        .filter(AudioFile.content_id == content_id, AudioFile.language == language)
        .first()
    )
    if existing:
        existing.file_path           = file_path
        existing.duration_ms         = duration_ms
        existing.shorts_breakpoints  = shorts_breakpoints
        existing.whisper_transcript  = whisper_transcript
        db.flush()
        return existing

    audio_file = AudioFile(
        content_id=content_id,
        language=language,
        file_path=file_path,
        duration_ms=duration_ms,
        shorts_breakpoints=shorts_breakpoints,
        whisper_transcript=whisper_transcript,
    )
    db.add(audio_file)
    db.flush()
    return audio_file
