import logging
import uuid

from sqlalchemy.orm import Session

from app.models import (
    AudioFile, Channel, ChannelConfig, ChannelVoice,
    Content, Script,
)
from app.agents.agent4_audio.services.tts import generate_audio
from app.agents.agent4_audio.services.storage import save_audio
from app.agents.agent4_audio.services.whisper import transcribe
from app.agents.agent4_audio.services.breakpoints import recalculate_breakpoints

logger = logging.getLogger(__name__)


def run_audio_generation(content_id: uuid.UUID, db: Session) -> bool:
    """Run the full Agent 4 audio pipeline for one piece of content.

    For every validated script language:
      1. Look up the ElevenLabs voice_id + emotion from channel_voices
      2. Generate TTS audio via ElevenLabs (eleven_multilingual_v2)
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

        try:
            # ── Step 1: TTS ──────────────────────────────────────────────────
            audio_bytes = generate_audio(script.voice_script, voice.voice_id, voice.emotion)

            # ── Step 2: Save + measure duration ─────────────────────────────
            file_path, duration_ms = save_audio(content_id, lang, audio_bytes)

            # ── Step 3: Whisper transcription ────────────────────────────────
            transcript = transcribe(file_path, language=lang)

            # ── Step 4: Recalculate Shorts breakpoints ───────────────────────
            bp = recalculate_breakpoints(transcript, duration_ms, shorts_rule)

            # ── Step 5: Persist AudioFile ────────────────────────────────────
            _upsert_audio_file(db, content_id, lang, file_path, duration_ms, bp, transcript)

            # ── Step 6: Update Script with real values ───────────────────────
            script.estimated_duration_sec = round(duration_ms / 1000, 1)
            script.shorts_breakpoints = bp

            db.commit()
            success_count += 1
            logger.info(
                "Audio done lang=%s: %.1fs | %d breakpoint(s) | %d words",
                lang, duration_ms / 1000, len(bp), len(transcript),
            )

        except Exception as exc:
            logger.error("Audio generation failed lang=%s: %s", lang, exc)
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
