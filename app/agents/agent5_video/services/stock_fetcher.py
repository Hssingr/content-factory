"""Stock media fetcher — retrieves actual image or video URLs for each section.

Fetching strategy per visual_source:
  pexels   → try videos first (for b-roll), fall back to images
  unsplash → images only (Unsplash has no video endpoint)
  runway   → skipped here; handled after Assembly Validator in Step 5

Provider fallback chain (if primary returns nothing):
  pexels → unsplash → pexels generic dark → hardcoded fallback marker

The chosen media_url is stored in the section dict and later passed to the
Remotion composition builder (Step 8).
"""

import logging

from app.services.pexels_client import search_images as pexels_images, search_videos as pexels_videos
from app.services.unsplash_client import search_images as unsplash_images

logger = logging.getLogger(__name__)

# Fallback query used when all provider searches return nothing
_FALLBACK_QUERY = "dark cinematic abstract background"

# Marker used in Remotion when even the fallback query fails
# Remotion renders a solid dark frame for this special value
_FALLBACK_URL = "__dark_fallback__"


def fetch_for_section(section: dict) -> dict:
    """Fetch a single image or video URL for a validated section.

    Updates the section dict in-place and returns it.

    Args:
        section: Validated section dict with at least ``visual_source``,
                 ``search_query``, and ``suggested_visual``.

    Returns:
        Section dict enriched with ``media_url``, ``media_thumb``,
        ``media_type`` ("image" | "video"), and ``media_source``
        ("pexels" | "unsplash" | "fallback").
    """
    source    = section.get("visual_source", "pexels")
    query     = section.get("search_query", "")
    visual    = section.get("suggested_visual", "b-roll")

    if source == "runway":
        # Runway sections are skipped here — Assembly Validator (Step 5) handles them
        section["media_url"]    = "__runway_pending__"
        section["media_thumb"]  = ""
        section["media_type"]   = "video"
        section["media_source"] = "runway"
        return section

    result = _fetch_from_primary(source, query, visual)

    if not result:
        result = _fetch_fallback(source, query)

    if not result:
        logger.error(
            "All providers returned nothing for section %d (query=%r) — using dark fallback",
            section.get("section_order", "?"), query,
        )
        result = _dark_fallback()

    section["media_url"]    = result["url"]
    section["media_thumb"]  = result.get("thumb_url", "")
    section["media_type"]   = result.get("media_type", "image")
    section["media_source"] = result.get("source", "fallback")

    logger.info(
        "Section %d: fetched %s/%s — %s",
        section.get("section_order", "?"),
        result.get("source", "?"),
        result.get("media_type", "?"),
        result["url"][:80],
    )
    return section


def fetch_all_sections(sections: list[dict]) -> list[dict]:
    """Fetch media for every section in the list.

    Runway sections are skipped (marked ``__runway_pending__``).

    Args:
        sections: Validated section dicts from ``section_validator``.

    Returns:
        Same list with each section enriched with media metadata.
    """
    for section in sections:
        try:
            fetch_for_section(section)
        except Exception as exc:
            logger.error(
                "Stock fetch failed for section %d: %s",
                section.get("section_order", "?"), exc,
            )
            _apply_fallback(section)

    logger.info(
        "Stock fetch complete: %d section(s) | video=%d image=%d runway=%d fallback=%d",
        len(sections),
        sum(1 for s in sections if s.get("media_type") == "video"),
        sum(1 for s in sections if s.get("media_type") == "image"),
        sum(1 for s in sections if s.get("media_source") == "runway"),
        sum(1 for s in sections if s.get("media_source") == "fallback"),
    )
    return sections


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_from_primary(source: str, query: str, visual: str) -> dict | None:
    """Fetch from the section's primary provider (pexels or unsplash)."""
    if source == "pexels":
        # For b-roll: prefer video clips over static images
        if visual == "b-roll":
            videos = pexels_videos(query, per_page=3)
            if videos:
                return _best_video(videos)
        images = pexels_images(query, per_page=5)
        return _best_image(images)

    if source == "unsplash":
        images = unsplash_images(query, per_page=5)
        return _best_image(images)

    return None


def _fetch_fallback(primary_source: str, original_query: str) -> dict | None:
    """Try the other provider, then a generic dark query."""
    # 1. Try the other provider with the same query
    other = "unsplash" if primary_source == "pexels" else "pexels"
    logger.info("Falling back to %s for query=%r", other, original_query)

    if other == "pexels":
        images = pexels_images(original_query, per_page=5)
    else:
        images = unsplash_images(original_query, per_page=5)

    result = _best_image(images)
    if result:
        return result

    # 2. Try generic dark fallback query on Pexels
    logger.info("Trying generic dark fallback query")
    images = pexels_images(_FALLBACK_QUERY, per_page=5)
    return _best_image(images)


def _dark_fallback() -> dict:
    """Return the hardcoded dark-frame marker when all providers fail."""
    return {"url": _FALLBACK_URL, "thumb_url": "", "media_type": "image", "source": "fallback"}


def _apply_fallback(section: dict) -> None:
    """Apply the dark fallback directly to a section (used on exceptions)."""
    section["media_url"]    = _FALLBACK_URL
    section["media_thumb"]  = ""
    section["media_type"]   = "image"
    section["media_source"] = "fallback"


def _best_image(images: list[dict]) -> dict | None:
    """Pick the highest-resolution landscape image from the list."""
    if not images:
        return None
    # Prefer landscape (width > height), then sort by area (largest first)
    landscape = [i for i in images if i.get("width", 0) > i.get("height", 0)]
    pool = landscape or images
    best = max(pool, key=lambda i: i.get("width", 0) * i.get("height", 0))
    return {**best, "media_type": "image"}


def _best_video(videos: list[dict]) -> dict | None:
    """Pick the highest-resolution landscape video from the list."""
    if not videos:
        return None
    # Sort by width (highest first), prefer HD and above (width ≥ 1280)
    hd = [v for v in videos if v.get("width", 0) >= 1280]
    pool = hd or videos
    best = max(pool, key=lambda v: v.get("width", 0))
    return {**best, "media_type": "video"}
