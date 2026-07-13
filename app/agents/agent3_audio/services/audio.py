import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    AudioFile, Channel, ChannelVoice,
    Content, Script,
)
from app.agents.agent3_audio.services.tts import generate_audio
from app.agents.agent3_audio.services.storage import audio_path, save_audio, measure_audio_duration_ms
from app.agents.agent3_audio.services.whisper import transcribe
from app.services.local_run_paths import ensure_run_dirs

logger = logging.getLogger(__name__)

_MIN_SHORT_AUDIO_DURATION_MS = 61_000
_DURATION_TRANSCRIPT_DRIFT_TOLERANCE = 0.02  # 2%


def _assert_short_audio_min_duration(
    *,
    content_id: uuid.UUID,
    language: str,
    duration_ms: int,
    is_short_episode: bool,
) -> bool:
    """Return False when a child Short audio file misses the hard 61 s floor."""
    if not is_short_episode:
        return True
    if duration_ms >= _MIN_SHORT_AUDIO_DURATION_MS:
        return True
    logger.error(
        "CHILD_SHORT_AUDIO_TOO_SHORT content_id=%s lang=%s duration_ms=%d min_duration_ms=%d",
        content_id, language, duration_ms, _MIN_SHORT_AUDIO_DURATION_MS,
    )
    return False


def _assert_short_audio_has_transcript(
    *,
    content_id: uuid.UUID,
    language: str,
    transcript: list[dict],
    is_short_episode: bool,
) -> bool:
    """Return False when a child Short's Whisper transcript is empty.

    Roadmap 3.9 / audit A-3, exec-10: ``transcribe()`` already tries OpenAI
    Whisper then falls back to local ``faster-whisper``, returning ``[]``
    only when both engines fail. That empty result was previously tolerated
    silently for every content type — persisted as-is, `AUDIO_DONE` reached
    regardless — which let a captionless vertical Short reach Agent 5 (the
    render-side blocker in ``agent5/services/video.py``'s
    ``_collect_technical_blockers`` is the second, independent half of this
    fix). Scoped to Shorts only: parent long-form content still tolerates a
    missing transcript exactly as before, since the storyboard/timestamp
    pipeline already has its own proportional-fallback timing machinery.
    """
    if not is_short_episode:
        return True
    if transcript:
        return True
    logger.error(
        "CHILD_SHORT_AUDIO_EMPTY_TRANSCRIPT content_id=%s lang=%s "
        "— both Whisper engines failed or returned no words",
        content_id, language,
    )
    return False


def _assert_duration_transcript_alignment(
    *,
    content_id: uuid.UUID,
    language: str,
    duration_ms: int,
    transcript: list[dict],
) -> bool:
    """Return False when ``duration_ms`` and the Whisper transcript's last
    word end disagree by more than ``_DURATION_TRANSCRIPT_DRIFT_TOLERANCE``.

    Roadmap 2b / audit P0-2 (`code_report/forensic_output_audit_borrasca_run.md`):
    a real production run persisted ``duration_ms=161724`` for a real
    616,835 ms audio file, with the SAME ``AudioFile`` row's
    ``whisper_transcript`` already showing a last word ending at ~616,580 ms
    — nothing anywhere compared the two, so the corrupted duration silently
    poisoned every downstream timeline (Agent 4's beat budget, timestamp
    mapping, the hold cap, Remotion's composition length, WPM calibration).
    This is the cross-timeline invariant that would have caught it at Agent
    3, before any Claude, Flux, or render spend.

    Applies to both parent and child content — the corruption this guards
    against hit parent audio, not just Shorts. Skipped only when there is no
    transcript to compare against, which is already a separately-tolerated
    case for parent content (see ``_assert_short_audio_has_transcript``,
    which hard-fails only Shorts on a missing transcript).
    """
    if not transcript:
        return True
    last_word_end_ms = float(transcript[-1].get("end") or 0.0) * 1000
    if last_word_end_ms <= 0:
        return True
    drift_ratio = abs(duration_ms - last_word_end_ms) / max(duration_ms, 1)
    if drift_ratio <= _DURATION_TRANSCRIPT_DRIFT_TOLERANCE:
        return True
    logger.error(
        "AUDIO_DURATION_TRANSCRIPT_MISMATCH content_id=%s lang=%s "
        "duration_ms=%d whisper_last_word_end_ms=%.0f drift=%.1f%% tolerance=%.0f%%",
        content_id, language, duration_ms, last_word_end_ms,
        drift_ratio * 100, _DURATION_TRANSCRIPT_DRIFT_TOLERANCE * 100,
    )
    return False


_VALID_VOICE_GENDERS = ("feminine", "masculine")
_DEFAULT_VOICE_GENDER = "feminine"


def _resolve_target_gender(content: Content, db: Session) -> str:
    """Return the target narration-voice gender for this content (roadmap
    Phase D1 — gender-aware voice selection).

    Reads ``protagonist_gender`` from the story blueprint: ``"feminine"``,
    ``"masculine"``, or ``"unspecified"`` (or missing entirely, for a
    blueprint generated before this phase shipped). Anything other than a
    real gender value normalizes to ``"feminine"`` here — a story with no
    single clear protagonist gets the channel's default voice, never a
    crash or a skipped language over an ambiguous blueprint value.

    Child Shorts never get their own blueprint — only parent script
    generation runs ``generate_story_blueprint()`` — so a child reads its
    parent's blueprint instead, the same convention already used by Agent 5's
    ``_resolve_proper_nouns_for_content()`` (roadmap Phase B2, CLAUDE.md
    §11A.4) for the same parent/child blueprint-sharing need.
    """
    blueprint = content.story_blueprint
    is_short_episode = bool(getattr(content, "is_short_episode", False))
    parent_content_id = getattr(content, "parent_content_id", None)
    if is_short_episode and not blueprint and parent_content_id:
        parent_content = db.get(Content, parent_content_id)
        blueprint = parent_content.story_blueprint if parent_content else None

    gender = (blueprint or {}).get("protagonist_gender")
    return gender if gender in _VALID_VOICE_GENDERS else _DEFAULT_VOICE_GENDER


def _select_channel_voice(
    voices_by_lang: dict[str, dict[str, "ChannelVoice"]], language: str, target_gender: str,
) -> "ChannelVoice | None":
    """Return the best-matching ``ChannelVoice`` for (language, target_gender)
    (roadmap Phase D1).

    Only one gender is required per language, not both — the operator may
    configure just feminine, just masculine, or both. An exact match wins;
    otherwise falls back to whichever gender IS configured for that language
    (there is at most one alternative, since gender is binary today) and
    logs a warning — content must never fail or skip a language solely
    because its preferred gender isn't configured. Returns ``None`` only
    when the language has no voice configured at all (unchanged from the
    pre-existing "no voice configured for lang" behavior).
    """
    by_gender = voices_by_lang.get(language) or {}
    exact = by_gender.get(target_gender)
    if exact is not None:
        return exact
    for fallback_gender, voice in by_gender.items():
        logger.warning(
            "VOICE_GENDER_FALLBACK language=%s wanted=%s used=%s",
            language, target_gender, fallback_gender,
        )
        return voice
    return None


def run_audio_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 3 audio pipeline for one piece of content.

    For every validated script language:
      1. Resolve the target narration-voice gender from the story blueprint's
         protagonist_gender (roadmap Phase D1 — _resolve_target_gender()) and
         select the matching channel_voices row (_select_channel_voice()) —
         falls back to whichever gender IS configured, logged, when the
         preferred one isn't; falls back to "feminine" when the blueprint
         has no protagonist_gender at all (legacy content, or a genuinely
         ambiguous story).
      2. Generate TTS audio via the configured provider (channel_voice.provider;
         model from channel_voice.tts_model; chunked at [SECTION N] boundaries)
      3. Save the mp3 to disk and measure exact duration with ffprobe
      4. Transcribe with OpenAI Whisper → word-level timestamps
      5. Persist AudioFile record and update Script.estimated_duration_sec

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

    ensure_run_dirs(content_id)

    channel: Channel | None = db.get(Channel, content.channel_id)
    if not channel:
        logger.error("Channel not found for content %s", content_id)
        return False

    is_short_episode: bool = bool(getattr(content, "is_short_episode", False))

    # Standalone short architecture content-level log — one line per content item, not per language.
    if is_short_episode:
        logger.info(
            "CHILD_SHORT_AUDIO_START content_id=%s parent_content_id=%s "
            "own_audio=True own_whisper=True",
            content_id, getattr(content, "parent_content_id", None),
        )
    else:
        logger.info(
            "PARENT_AUDIO_START content_id=%s "
            "is_short_episode=False own_audio=True own_whisper=True",
            content_id,
        )

    # Build voice map: language → gender → ChannelVoice (roadmap Phase D1 —
    # gender-aware voice selection; a language may have one or both genders
    # configured, see _select_channel_voice()).
    voices_by_lang: dict[str, dict[str, ChannelVoice]] = {}
    for v in db.query(ChannelVoice).filter(ChannelVoice.channel_id == channel.id).all():
        voices_by_lang.setdefault(v.language, {})[v.gender] = v

    target_gender = _resolve_target_gender(content, db)

    # Load the latest validated script per language
    scripts = _load_latest_scripts(content_id, db)
    if not scripts:
        logger.warning("No validated scripts found for content %s", content_id)
        return False

    content.status = "GENERATING_AUDIO"
    db.commit()

    success_count = 0

    for lang, script in scripts.items():
        voice = _select_channel_voice(voices_by_lang, lang, target_gender)
        if not voice or not voice.voice_id:
            logger.warning("No voice configured for lang=%s — skipping", lang)
            continue

        logger.debug("Processing lang=%s voice_id=%s", lang, voice.voice_id)

        # ── Step 1: TTS (skip if file already on disk) ───────────────────────
        existing = audio_path(content_id, lang)
        audio_was_generated = False
        reuse_existing_transcript = False
        transcript: list[dict] = []
        # Real per-section audio boundaries (roadmap Phase B1) — only known
        # at fresh-generation time or when reused from an already-persisted
        # row; None on any other resume path (the final concatenated file
        # on disk alone cannot reconstruct per-section timing), which Agent 4
        # already treats as "no section data available" and falls back to
        # whole-video proportional stretch for.
        section_boundaries: list[dict] | None = None
        try:
            if existing.exists():
                logger.info("Audio already on disk — skipping TTS for lang=%s", lang)
                file_path = str(existing)
                existing_audio_file: AudioFile | None = (
                    db.query(AudioFile)
                    .filter(AudioFile.content_id == content_id, AudioFile.language == lang)
                    .first()
                )
                if existing_audio_file and existing_audio_file.duration_ms and existing_audio_file.whisper_transcript:
                    # D1.6 (Elimination Mandate, code_report/forensic_output_audit_borrasca_run.md):
                    # the audio file on disk hasn't changed, so trust the already-persisted
                    # duration + transcript instead of re-measuring the header and
                    # re-transcribing it — a resumed run was re-transcribing the same
                    # audio on every retry for zero new information.
                    duration_ms = existing_audio_file.duration_ms
                    transcript  = existing_audio_file.whisper_transcript
                    section_boundaries = existing_audio_file.section_boundaries
                    reuse_existing_transcript = True
                    logger.info(
                        "RESUME_TRANSCRIPT_REUSED content_id=%s lang=%s duration_ms=%d words=%d",
                        content_id, lang, duration_ms, len(transcript),
                    )
                else:
                    duration_ms = measure_audio_duration_ms(existing)
            else:
                audio_bytes, section_boundaries = generate_audio(
                    script.voice_script, voice, is_short_episode=is_short_episode
                )
                audio_was_generated = True
                file_path, duration_ms  = save_audio(content_id, lang, audio_bytes)

            if not _assert_short_audio_min_duration(
                content_id=content_id,
                language=lang,
                duration_ms=duration_ms,
                is_short_episode=is_short_episode,
            ):
                db.rollback()
                continue

        except Exception as exc:
            logger.error("TTS/storage failed lang=%s: %s", lang, exc)
            db.rollback()
            continue

        # ── Step 3: Whisper transcription ─────────────────────────────────────
        # Soft for parent long-form content — missing transcript is tolerated,
        # falling back to proportional timing downstream. Hard for child
        # Shorts (roadmap 3.9 / audit A-3, exec-10) — see
        # _assert_short_audio_has_transcript() below. Skipped entirely when a
        # transcript was already reused above (D1.6) — the file didn't change.
        if not reuse_existing_transcript:
            try:
                transcript = transcribe(file_path, language=lang)
            except Exception as exc:
                logger.warning(
                    "Whisper failed lang=%s (%s) — continuing without word timestamps",
                    lang, exc,
                )

        if not _assert_short_audio_has_transcript(
            content_id=content_id,
            language=lang,
            transcript=transcript,
            is_short_episode=is_short_episode,
        ):
            db.rollback()
            continue

        if not _assert_duration_transcript_alignment(
            content_id=content_id,
            language=lang,
            duration_ms=duration_ms,
            transcript=transcript,
        ):
            db.rollback()
            continue

        try:
            # ── Step 5: Persist AudioFile ────────────────────────────────────
            _upsert_audio_file(
                db, content_id, lang, file_path, duration_ms, transcript, section_boundaries,
                refresh_generated_at=audio_was_generated,
            )

            # ── Step 6: Update Script with real duration ─────────────────────
            script.estimated_duration_sec = round(duration_ms / 1000, 1)

            db.commit()
            success_count += 1
            logger.debug(
                "Audio done lang=%s: %.1fs | standalone_short_architecture | %d whisper words",
                lang, duration_ms / 1000, len(transcript),
            )
            if is_short_episode:
                logger.info(
                    "CHILD_SHORT_AUDIO_DONE content_id=%s duration_ms=%d lang=%s",
                    content_id, duration_ms, lang,
                )
            else:
                logger.info(
                    "PARENT_AUDIO_DONE content_id=%s duration_ms=%d lang=%s",
                    content_id, duration_ms, lang,
                )
        except Exception as exc:
            logger.error("Persist failed lang=%s: %s", lang, exc)
            db.rollback()
            continue

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
    whisper_transcript: list[dict],
    section_boundaries: list[dict] | None = None,
    refresh_generated_at: bool = True,
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
        existing.whisper_transcript  = whisper_transcript
        existing.section_boundaries  = section_boundaries
        if refresh_generated_at:
            existing.generated_at = datetime.now(timezone.utc)
        db.flush()
        return existing

    audio_file = AudioFile(
        content_id=content_id,
        language=language,
        file_path=file_path,
        duration_ms=duration_ms,
        whisper_transcript=whisper_transcript,
        section_boundaries=section_boundaries,
    )
    db.add(audio_file)
    db.flush()
    return audio_file
