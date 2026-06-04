import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Language → ElevenLabs shared-voices query params
_LANG_PARAMS: dict[str, dict] = {
    'fr': {'required_languages': 'fr', 'accent': 'standard'},
    'en': {'required_languages': 'en', 'accent': 'american'},
    'de': {'required_languages': 'de', 'accent': 'standard'},
    'es': {'required_languages': 'es', 'accent': 'latin american'},
    'it': {'required_languages': 'it', 'accent': 'standard'},
    'pt': {'required_languages': 'pt', 'accent': 'european'},
}

# Cache keyed by (language, use_case) — different combos are independent
_cache: dict[tuple, list[dict]] = {}


def get_shared_voices(language: str, use_case: str) -> list[dict]:
    """Fetch up to 10 trending shared voices for a language+use_case combination.

    Returns:
        Simplified voice list: [{voice_id, name, gender, age, descriptive, description, preview_url}].
        Empty list if language is unsupported, API key is missing, or request fails.
    """
    lang_params = _LANG_PARAMS.get(language)
    if lang_params is None:
        logger.warning("Unsupported language for voice fetch: %s", language)
        return []

    if not settings.elevenlabs_api_key:
        logger.warning("ELEVENLABS_API_KEY not set — returning empty voice list")
        return []

    cache_key = (language, use_case)
    if cache_key in _cache:
        return _cache[cache_key]

    params = {
        **lang_params,
        'use_cases': use_case,
        'sort':      'trending',
        'page_size': 10,
        'page':      0,
    }

    try:
        resp = httpx.get(
            'https://api.elevenlabs.io/v1/shared-voices',
            headers={'xi-api-key': settings.elevenlabs_api_key},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        voices = resp.json().get('voices', [])
        result = [
            {
                'voice_id':    v['voice_id'],
                'name':        v['name'],
                'gender':      v.get('gender'),
                'age':         v.get('age'),
                'descriptive': v.get('descriptive'),
                'description': v.get('description'),
                'preview_url': v.get('preview_url'),
            }
            for v in voices
        ]
        _cache[cache_key] = result
        logger.info("Fetched %d shared voices for lang=%s use_case=%s", len(result), language, use_case)
        return result
    except Exception as exc:
        logger.error("Failed to fetch shared voices for lang=%s use_case=%s: %s", language, use_case, exc)
        return []
