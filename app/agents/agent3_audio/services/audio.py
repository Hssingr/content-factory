import logging
import uuid

from sqlalchemy.orm import Session

from app.models import (
    AudioFile, Channel, ChannelVoice,
    Content, Script,
)
from app.agents.agent3_audio.services.tts import generate_audio
from app.agents.agent3_audio.services.storage import audio_path, save_audio
from app.agents.agent3_audio.services.whisper import transcribe

logger = logging.getLogger(__name__)


def run_audio_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 3 audio pipeline for one piece of content.

    For every validated script language:
      1. Look up the voice_id + emotion from channel_voices
      2. Generate TTS audio via the configured provider (channel_voice.provider;
         model from channel_voice.tts_model; chunked at [SECTION N] boundaries)
      3. Save the mp3 to disk and measure exact duration with mutagen
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
            "PARENT_AUDIO_STANDALONE_SHORTS_ONLY content_id=%s "
            "standalone_child_shorts_only=True",
            content_id,
        )

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
                audio_bytes             = generate_audio(script.voice_script, voice, is_short_episode=is_short_episode)
                file_path, duration_ms  = save_audio(content_id, lang, audio_bytes)

        except Exception as exc:
            logger.error("TTS/storage failed lang=%s: %s", lang, exc)
            db.rollback()
            continue

        # ── Step 3: Whisper transcription (soft — missing transcript is tolerated) ──
        transcript: list[dict] = []
        try:
            transcript = transcribe(file_path, language=lang)
        except Exception as exc:
            logger.warning(
                "Whisper failed lang=%s (%s) — continuing without word timestamps",
                lang, exc,
            )

        try:
            # ── Step 5: Persist AudioFile ────────────────────────────────────
            _upsert_audio_file(db, content_id, lang, file_path, duration_ms, transcript)

            # ── Step 6: Update Script with real duration ─────────────────────
            script.estimated_duration_sec = round(duration_ms / 1000, 1)

            db.commit()
            success_count += 1
            logger.info(
                "Audio done lang=%s: %.1fs | standalone_short_architecture | %d whisper words",
                lang, duration_ms / 1000, len(transcript),
            )
            if is_short_episode:
                logger.info(
                    "CHILD_SHORT_AUDIO_DONE child_content_id=%s duration_ms=%d lang=%s",
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
        db.flush()
        return existing

    audio_file = AudioFile(
        content_id=content_id,
        language=language,
        file_path=file_path,
        duration_ms=duration_ms,
        whisper_transcript=whisper_transcript,
    )
    db.add(audio_file)
    db.flush()
    return audio_file
