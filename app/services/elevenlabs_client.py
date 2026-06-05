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
        _client = ElevenLabs(api_key=settings.elevenlabs_api_key)
        logger.info("ElevenLabs client initialised")
    return _client
