"""ElevenLabs Voice Library browsing for Agent 1 setup (paginated, filtered).

Read-only listing of ElevenLabs' public Voice Library (`voices.get_shared()`
in the installed SDK, backed by `GET /v1/shared-voices`) so the operator can
search and pick a real Voice ID from the provider's own catalog instead of
pasting one blindly. Setup-time browsing only — Agent 3's TTS generation
never imports this module, and Voice IDs remain operator-provided hard
inputs (CLAUDE.md §8): this only helps the operator find one.

Reuses the shared `elevenlabs_client.get_client()` singleton (CLAUDE.md §7)
rather than constructing a second client — no new provider integration
point, just a new read-only call through the existing one.
"""

from __future__ import annotations

import logging

from app.services import elevenlabs_client

logger = logging.getLogger(__name__)

_MAX_PAGE_SIZE = 100


def list_shared_voices(
    *,
    search: str | None = None,
    gender: str | None = None,
    age: str | None = None,
    accent: str | None = None,
    language: str | None = None,
    use_case: str | None = None,
    page_size: int = 20,
    cursor: str | None = None,
) -> dict:
    """List ElevenLabs Voice Library voices, one page at a time.

    Args:
        search, gender, age, accent, language, use_case: ElevenLabs Voice
            Library filters (`voices.get_shared()`'s own params) — all
            free-text except `use_case`, which the SDK also accepts as
            free-text (no fixed enum documented).
        page_size: Voices per page (ElevenLabs allows up to 100, default 30).
        cursor: A previous response's `next_cursor` (a stringified page
            number — ElevenLabs' Voice Library pagination is offset-based,
            not cursor-based), or `None` for the first page.

    Returns:
        {"voices": [...], "has_more": bool, "next_cursor": str | None}.
        Empty/`has_more=False` on missing API key or any request failure —
        fails open, same convention as every other Agent 1 suggestion call.
    """
    try:
        client = elevenlabs_client.get_client()
    except RuntimeError as exc:
        logger.warning("ElevenLabs voice browsing unavailable: %s", exc)
        return {"voices": [], "has_more": False, "next_cursor": None}

    try:
        page = int(cursor) if cursor else 0
    except ValueError:
        page = 0

    try:
        response = client.voices.get_shared(
            page_size=max(1, min(page_size, _MAX_PAGE_SIZE)),
            page=page,
            search=search or None,
            gender=gender or None,
            age=age or None,
            accent=accent or None,
            language=language or None,
            use_cases=[use_case] if use_case else None,
        )
    except Exception as exc:
        logger.error("Failed to list ElevenLabs shared voices: %s", exc)
        return {"voices": [], "has_more": False, "next_cursor": None}

    voices = [
        {
            "voice_id": v.voice_id,
            "name": v.name,
            "description": v.description or v.descriptive or "",
            "gender": v.gender,
            "language": v.language,
            "accent": v.accent,
            "age": v.age,
            "preview_url": v.preview_url,
            "provider": "elevenlabs",
        }
        for v in response.voices
    ]
    has_more = bool(response.has_more)
    logger.info(
        "ELEVENLABS_VOICE_BROWSE fetched=%d page=%d search=%r gender=%r language=%r",
        len(voices), page, search, gender, language,
    )
    return {
        "voices": voices,
        "has_more": has_more,
        "next_cursor": str(page + 1) if has_more else None,
    }
