import logging
import re

from elevenlabs.types import VoiceSettings

from app.services.elevenlabs_client import get_client

logger = logging.getLogger(__name__)

# Matches [INTRO], [OUTRO], [SECTION N], [SECTION N: Title] on their own line.
# These markers are included in voice_script for timing alignment and must be
# stripped before sending to ElevenLabs (otherwise they are read aloud).
_MARKER_RE = re.compile(
    r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# ElevenLabs multilingual model — supports all 6 channel languages (fr/en/de/es/it/pt)
_MODEL_ID      = "eleven_multilingual_v2"
_OUTPUT_FORMAT = "mp3_44100_128"   # 44 100 Hz stereo, 128 kbps

# Emotion label → ElevenLabs VoiceSettings
# stability        : 0–1  (higher = more consistent; lower = more expressive)
# similarity_boost : 0–1  (how closely to mimic the original voice clone)
# style            : 0–1  (style exaggeration; v2 models only)
# use_speaker_boost: improves clarity at slight latency cost
_EMOTION_SETTINGS: dict[str, dict] = {
    "neutral":       {"stability": 0.75, "similarity_boost": 0.75, "style": 0.00, "use_speaker_boost": True},
    "calm":          {"stability": 0.85, "similarity_boost": 0.80, "style": 0.00, "use_speaker_boost": True},
    "warm":          {"stability": 0.65, "similarity_boost": 0.80, "style": 0.15, "use_speaker_boost": True},
    "authoritative": {"stability": 0.80, "similarity_boost": 0.85, "style": 0.20, "use_speaker_boost": True},
    "enthusiastic":  {"stability": 0.45, "similarity_boost": 0.75, "style": 0.45, "use_speaker_boost": True},
    "dramatic":      {"stability": 0.30, "similarity_boost": 0.70, "style": 0.60, "use_speaker_boost": True},
}
_DEFAULT_EMOTION = "neutral"


def generate_audio(voice_script: str, voice_id: str, emotion: str | None) -> bytes:
    """Convert a voice script to mp3 audio via ElevenLabs TTS.

    Uses ``eleven_multilingual_v2`` at 44 100 Hz / 128 kbps.
    The ``emotion`` label is translated to ElevenLabs VoiceSettings.

    Args:
        voice_script: Full narrator text (no stage directions, no brackets).
        voice_id:     ElevenLabs voice ID from ``channel_voices.voice_id``.
        emotion:      Emotion label from ``channel_voices.emotion``.
                      Supported: neutral | calm | warm | authoritative |
                      enthusiastic | dramatic.  Falls back to neutral.

    Returns:
        Raw mp3 bytes ready to be written to disk.

    Raises:
        RuntimeError: If ELEVENLABS_API_KEY is not configured.
        Exception:    On any ElevenLabs API error.
    """
    voice_script = _MARKER_RE.sub("", voice_script).strip()

    resolved_emotion = emotion or _DEFAULT_EMOTION
    if resolved_emotion not in _EMOTION_SETTINGS:
        logger.warning("Unknown emotion %r — falling back to neutral", resolved_emotion)
        resolved_emotion = _DEFAULT_EMOTION

    voice_settings = VoiceSettings(**_EMOTION_SETTINGS[resolved_emotion])

    logger.info(
        "TTS start: voice_id=%s emotion=%s words=%d model=%s",
        voice_id, resolved_emotion, len(voice_script.split()), _MODEL_ID,
    )

    audio_iter = get_client().text_to_speech.convert(
        text=voice_script,
        voice_id=voice_id,
        model_id=_MODEL_ID,
        output_format=_OUTPUT_FORMAT,
        voice_settings=voice_settings,
    )

    # convert() returns a generator — collect all chunks
    audio_bytes = b"".join(audio_iter)
    logger.info("TTS complete: %d bytes (%.1f KB)", len(audio_bytes), len(audio_bytes) / 1024)
    return audio_bytes
