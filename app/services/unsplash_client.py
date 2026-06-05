import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.unsplash.com"


def _headers() -> dict:
    """Unsplash auth uses 'Client-ID <key>' format."""
    return {"Authorization": f"Client-ID {settings.unsplash_api_key}"}


def _available() -> bool:
    if not settings.unsplash_api_key:
        logger.warning("UNSPLASH_API_KEY not set — returning empty results")
        return False
    return True


def search_images(query: str, per_page: int = 5) -> list[dict]:
    """Search Unsplash for landscape photos matching the query.

    Note: Unsplash does not provide video search — use ``pexels_client.search_videos()``
    for video assets.

    Args:
        query:    English search string (e.g. "dark hospital corridor night").
        per_page: Max results to return (default 5).

    Returns:
        List of dicts with keys: url, thumb_url, source, width, height, photographer.
        Empty list if API key is missing, query returns nothing, or a 429 rate-limit hits.
    """
    if not _available():
        return []
    try:
        resp = httpx.get(
            f"{_BASE}/search/photos",
            headers=_headers(),
            params={
                "query":       query,
                "per_page":    per_page,
                "orientation": "landscape",
                "content_filter": "high",   # filter out low-quality / NSFW
            },
            timeout=10,
        )
        if resp.status_code == 429:
            logger.warning("Unsplash rate limit hit for query=%r", query)
            return []
        resp.raise_for_status()
        photos = resp.json().get("results", [])
        return [
            {
                "url":          p["urls"]["full"],
                "thumb_url":    p["urls"]["small"],
                "source":       "unsplash",
                "width":        p["width"],
                "height":       p["height"],
                "photographer": p.get("user", {}).get("name", ""),
            }
            for p in photos
        ]
    except Exception as exc:
        logger.error("Unsplash image search failed (query=%r): %s", query, exc)
        return []
