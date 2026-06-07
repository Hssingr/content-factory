import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.pexels.com"
_MAX_VIDEO_WIDTH = 1920   # never return a UHD file — Remotion's proxy cannot handle them


def _best_video_file(files: list[dict]) -> dict | None:
    """Pick the best video file at or below FHD resolution.

    Selection priority:
      1. HD/FHD range: 1280–1920 px wide (highest width in range)
      2. Any file ≤ 1920 px wide (highest width available)
      3. None — skip this video entirely rather than return a UHD URL
    """
    if not files:
        return None
    fhd = [f for f in files if 1280 <= f.get("width", 0) <= _MAX_VIDEO_WIDTH]
    if fhd:
        return max(fhd, key=lambda f: f.get("width", 0))
    capped = [f for f in files if 0 < f.get("width", 0) <= _MAX_VIDEO_WIDTH]
    if capped:
        return max(capped, key=lambda f: f.get("width", 0))
    return None   # only UHD available — skip


def _headers() -> dict:
    """Pexels auth header — no 'Bearer' prefix, just the raw API key."""
    return {"Authorization": settings.pexels_api_key}


def _available() -> bool:
    if not settings.pexels_api_key:
        logger.warning("PEXELS_API_KEY not set — returning empty results")
        return False
    return True


def search_images(query: str, per_page: int = 5) -> list[dict]:
    """Search Pexels for landscape photos matching the query.

    Args:
        query:    English search string (e.g. "dark hospital corridor").
        per_page: Max results to return (default 5).

    Returns:
        List of dicts with keys: url, thumb_url, source, width, height, photographer.
        Empty list if API key is missing, query returns nothing, or a 429 rate-limit hits.
    """
    if not _available():
        return []
    try:
        resp = httpx.get(
            f"{_BASE}/v1/search",
            headers=_headers(),
            params={"query": query, "per_page": per_page, "orientation": "landscape"},
            timeout=10,
        )
        if resp.status_code == 429:
            logger.warning("Pexels image rate limit hit for query=%r", query)
            return []
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        return [
            {
                "url":          p["src"]["original"],
                "thumb_url":    p["src"]["small"],
                "source":       "pexels",
                "width":        p["width"],
                "height":       p["height"],
                "photographer": p.get("photographer", ""),
            }
            for p in photos
        ]
    except Exception as exc:
        logger.error("Pexels image search failed (query=%r): %s", query, exc)
        return []


def search_videos(query: str, per_page: int = 3) -> list[dict]:
    """Search Pexels for landscape videos matching the query.

    Picks the highest-quality HD video file available for each result.

    Args:
        query:    English search string.
        per_page: Max results to return (default 3).

    Returns:
        List of dicts with keys: url, thumb_url, source, duration_seconds, width, height.
        Empty list on any error or missing API key.
    """
    if not _available():
        return []
    try:
        resp = httpx.get(
            f"{_BASE}/videos/search",
            headers=_headers(),
            params={"query": query, "per_page": per_page, "orientation": "landscape"},
            timeout=10,
        )
        if resp.status_code == 429:
            logger.warning("Pexels video rate limit hit for query=%r", query)
            return []
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
        results = []
        for v in videos:
            best = _best_video_file(v.get("video_files", []))
            if not best:
                continue
            results.append({
                "url":              best["link"],
                "thumb_url":        v.get("image", ""),
                "source":           "pexels",
                "duration_seconds": v.get("duration", 0),
                "width":            best.get("width", 0),
                "height":           best.get("height", 0),
            })
        return results
    except Exception as exc:
        logger.error("Pexels video search failed (query=%r): %s", query, exc)
        return []
