import logging
from typing import Iterator

from elevenlabs import ElevenLabs
from elevenlabs.types import VoiceSettings

from app.config import settings

logger = logging.getLogger(__name__)

_client: ElevenLabs | None = None


def get_client() -> ElevenLabs:
    """Return the shared ElevenLabs client singleton.

    Raises:
        RuntimeError: If ELEVENLABS_API_KEY is not set in the environment.
    """
    global _client
    if _client is None:
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        # timeout: the SDK's default (~60s read) is too tight for a long
        # eleven_v3 section — a real production run lost an entire language
        # ("The read operation timed out" on section 6/6 of a FR parent,
        # after 5 sections had already succeeded). 300s matches the
        # generous-ceiling convention the Anthropic client already uses;
        # a genuinely hung request still fails, just not a merely-slow one.
        _client = ElevenLabs(api_key=settings.elevenlabs_api_key, timeout=300.0)
        logger.info("ElevenLabs client initialised (timeout=300s)")
    return _client
