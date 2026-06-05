import logging

from openai import OpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """Return the shared OpenAI client singleton.

    Raises:
        RuntimeError: If OPENAI_API_KEY is not set in the environment.
    """
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _client = OpenAI(api_key=settings.openai_api_key)
        logger.info("OpenAI client initialised")
    return _client
