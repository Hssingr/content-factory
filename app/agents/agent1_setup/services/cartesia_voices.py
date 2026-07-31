"""Cartesia voice-catalog browsing for Agent 1 setup (paginated, filtered).

Read-only listing of Cartesia's voice catalog (the operator's own voices plus
Cartesia's shared/public ones) so the operator can search and pick a real
Voice ID from the provider's own catalog instead of pasting one blindly.
Setup-time browsing only — Agent 3's TTS generation never imports this
module, and Voice IDs remain operator-provided hard inputs (CLAUDE.md §8):
this only helps the operator find one.

The installed `cartesia` SDK's `Voices.list()` takes no query parameters at
all (see `cartesia/voices.py` in site-packages), so this calls Cartesia's
REST `GET /voices` endpoint directly via httpx, reusing the same
`X-API-Key`/`Cartesia-Version` header convention the SDK's own `Resource`
base class uses (`cartesia/resource.py`) — kept in sync with the installed
SDK's pinned API version via `DEFAULT_CARTESIA_VERSION` rather than a
hardcoded duplicate string.

**Two response shapes, handled defensively (live-canary fix):** Cartesia's
current public docs describe a paginated envelope
(`{"data": [...], "has_more": bool, "next_page": str}`), but the API version
this SDK is pinned to (`DEFAULT_CARTESIA_VERSION`, "2024-06-10" as of this
writing — matching the exact version Agent 3's TTS generation already uses
successfully in production) returns a **bare JSON array** instead — a real
`--confirm`-adjacent run hit `AttributeError: 'list' object has no attribute
'get'` on the very first live call, proving the envelope shape is a newer
API version's behavior, not this pinned one's. Rather than guess a different
`Cartesia-Version` value with no way to verify it live, `list_voices()`
detects which shape came back: a dict uses the documented envelope fields
directly (works if a future SDK/version bump adopts the paginated shape); a
bare list is the full unfiltered/unpaginated catalog, so `search`/`gender`/
`language` filtering and offset-based pagination are applied client-side in
Python instead, so the feature still delivers on "paginated, filtered"
regardless of which shape this account's pinned API version returns.
"""

from __future__ import annotations

import logging

import httpx
from cartesia._constants import DEFAULT_CARTESIA_VERSION

from app.config import settings

logger = logging.getLogger(__name__)

_LIST_URL = "https://api.cartesia.ai/voices"
_MAX_PAGE_SIZE = 100


def _normalize_voice(v: dict) -> dict:
    return {
        "voice_id": v["id"],
        "name": v.get("name", ""),
        "description": v.get("description") or v.get("tagline") or "",
        "gender": v.get("gender"),
        "language": v.get("language"),
        "accent": None,
        "age": None,
        "preview_url": v.get("preview_file_url"),
        "provider": "cartesia",
    }


def _matches_filters(voice: dict, *, search: str | None, gender: str | None, language: str | None) -> bool:
    if search:
        haystack = " ".join(
            str(voice.get(f, "") or "") for f in ("voice_id", "name", "description")
        ).lower()
        if search.lower() not in haystack:
            return False
    if gender and (voice.get("gender") or "").lower() != gender.lower():
        return False
    if language:
        voice_language = (voice.get("language") or "").lower()
        wanted = language.lower()
        if voice_language != wanted and not voice_language.startswith(f"{wanted}-"):
            return False
    return True


def list_voices(
    *,
    search: str | None = None,
    gender: str | None = None,
    language: str | None = None,
    page_size: int = 20,
    cursor: str | None = None,
) -> dict:
    """List Cartesia voices, one page at a time.

    Args:
        search: Free-text search across name/description/voice ID (Cartesia's `q` param).
        gender: One of "masculine" | "feminine" | "gender_neutral".
        language: Language/locale code, e.g. "en" or "en-GB".
        page_size: Voices per page (Cartesia allows 1-100).
        cursor: A previous response's `next_cursor` — opaque to the caller;
            it may be a Cartesia voice ID (server-side envelope shape) or a
            stringified offset (client-side fallback shape), or `None` for
            the first page. A malformed/foreign cursor falls back to the
            first page rather than raising.

    Returns:
        {"voices": [...], "has_more": bool, "next_cursor": str | None}.
        Empty/`has_more=False` on missing API key or any request failure —
        fails open, same convention as every other Agent 1 suggestion call.
    """
    if not settings.cartesia_api_key:
        logger.warning("CARTESIA_API_KEY not set — returning empty voice list")
        return {"voices": [], "has_more": False, "next_cursor": None}

    params: dict = {
        "limit": max(1, min(page_size, _MAX_PAGE_SIZE)),
        "expand[]": "preview_file_url",
    }
    if search:
        params["q"] = search
    if gender:
        params["gender"] = gender
    if language:
        params["language"] = language
    if cursor:
        params["starting_after"] = cursor

    try:
        response = httpx.get(
            _LIST_URL,
            headers={
                "X-API-Key": settings.cartesia_api_key,
                "Cartesia-Version": DEFAULT_CARTESIA_VERSION,
            },
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.error("Failed to list Cartesia voices: %s", exc)
        return {"voices": [], "has_more": False, "next_cursor": None}

    if isinstance(body, dict):
        # Documented paginated envelope — server already applied our filters
        # and pagination; use its fields directly.
        voices = [_normalize_voice(v) for v in body.get("data", [])]
        logger.info(
            "CARTESIA_VOICE_BROWSE fetched=%d search=%r gender=%r language=%r (server-side envelope)",
            len(voices), search, gender, language,
        )
        return {
            "voices": voices,
            "has_more": bool(body.get("has_more")),
            "next_cursor": body.get("next_page"),
        }

    # Bare list — this API version returns the full, unfiltered catalog in
    # one response regardless of query params. Apply search/gender/language
    # filtering and offset-based pagination ourselves.
    all_voices = [_normalize_voice(v) for v in body]
    filtered = [v for v in all_voices if _matches_filters(v, search=search, gender=gender, language=language)]

    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        offset = 0
    limit = params["limit"]
    page = filtered[offset:offset + limit]
    has_more = offset + limit < len(filtered)

    logger.info(
        "CARTESIA_VOICE_BROWSE fetched=%d/%d search=%r gender=%r language=%r offset=%d (client-side pagination)",
        len(page), len(filtered), search, gender, language, offset,
    )
    return {
        "voices": page,
        "has_more": has_more,
        "next_cursor": str(offset + limit) if has_more else None,
    }
