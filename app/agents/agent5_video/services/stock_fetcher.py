"""Stock media fetcher — retrieves image/video clips for each section.

Each section receives a list of clips (not a single clip). The clip count
scales with section duration:
  < 8 s  → 1 clip  (too short to rotate)
  8–20 s → 2 clips
  > 20 s → 3 clips

Fetching strategy per visual_source:
  pexels   → try videos first (for b-roll), fall back to images
  unsplash → images only (Unsplash has no video endpoint)
  runway   → skipped here; handled after Assembly Validator in Step 5

Provider fallback chain (if primary returns nothing):
  pexels → unsplash → pexels generic dark → hardcoded fallback marker

The primary media_url / media_thumb / media_type fields are preserved on the
section dict (set to the first clip) for backward compatibility. The full
list is available under section["clips"].
"""

import logging

from app.services.pexels_client import search_images as pexels_images, search_videos as pexels_videos
from app.services.unsplash_client import search_images as unsplash_images

logger = logging.getLogger(__name__)

_FALLBACK_QUERY = "dark cinematic abstract background"
_FALLBACK_URL   = "__dark_fallback__"
_GENERATED_PLACEHOLDER_URL = "__generated_pending__"

# Storyboard visual_type values that need no stock search — rendered from a
# dark background (with on-screen text or a generated-visual placeholder)
_NO_MEDIA_VISUAL_TYPES = {"text_overlay", "generated_visual"}

_MAX_VIDEO_WIDTH = 1920   # cap at FHD — UHD causes disk/proxy issues in Remotion


def fetch_for_section(section: dict) -> dict:
    """Fetch a list of clips for a validated section.

    The number of clips scales with the section's duration so that longer
    sections get more visual variety. The first clip is also stored in the
    legacy ``media_url`` / ``media_thumb`` / ``media_type`` fields.

    Args:
        section: Validated section dict with at least ``visual_source``,
                 ``search_query``, ``suggested_visual``, and ``duration_sec``.

    Returns:
        Section dict enriched with ``clips`` (list of clip dicts) and the
        legacy ``media_url``, ``media_thumb``, ``media_type``, ``media_source``.
    """
    source  = section.get("visual_source", "pexels")
    query   = section.get("search_query", "")
    visual  = section.get("suggested_visual", "b-roll")
    dur_sec = float(section.get("duration_sec", 0))

    if source == "runway":
        section["clips"]       = [{"url": "__runway_pending__", "thumb_url": "", "media_type": "video"}]
        section["media_url"]   = "__runway_pending__"
        section["media_thumb"] = ""
        section["media_type"]  = "video"
        section["media_source"] = "runway"
        return section

    clip_count = _clip_count_for_duration(dur_sec)
    clips = _fetch_clips(source, query, visual, clip_count)

    if not clips:
        clips = _fetch_clips_fallback(source, query, clip_count)

    if not clips:
        logger.error(
            "Section %d (query=%r): all providers empty — using dark fallback",
            section.get("section_order", "?"), query,
        )
        clips = [_dark_clip()]

    section["clips"]       = clips
    section["media_url"]   = clips[0]["url"]
    section["media_thumb"] = clips[0].get("thumb_url", "")
    section["media_type"]  = clips[0].get("media_type", "image")
    section["media_source"] = clips[0].get("source", "fallback")

    logger.info(
        "Section %d: %d clip(s) from %s/%s — %s",
        section.get("section_order", "?"),
        len(clips),
        clips[0].get("source", "?"),
        clips[0].get("media_type", "?"),
        clips[0]["url"][:80],
    )
    return section


def fetch_all_sections(sections: list[dict]) -> list[dict]:
    """Fetch clips for every section in the list.

    Runway sections are skipped (marked ``__runway_pending__``).

    Args:
        sections: Validated section dicts from ``section_validator``.

    Returns:
        Same list with each section enriched with ``clips`` and media metadata.
    """
    for section in sections:
        try:
            fetch_for_section(section)
        except Exception as exc:
            logger.error(
                "Stock fetch failed for section %d: %s",
                section.get("section_order", "?"), exc,
            )
            _apply_dark_fallback(section)

    video_count    = sum(1 for s in sections if s.get("media_type") == "video")
    image_count    = sum(1 for s in sections if s.get("media_type") == "image")
    runway_count   = sum(1 for s in sections if s.get("media_source") == "runway")
    fallback_count = sum(1 for s in sections if s.get("media_source") == "fallback")

    logger.info(
        "Stock fetch complete: %d section(s) | video=%d image=%d runway=%d fallback=%d",
        len(sections), video_count, image_count, runway_count, fallback_count,
    )
    return sections


# ── Storyboard beat fetching ──────────────────────────────────────────────────
# Beats carry a Claude-decided ``visual_type`` (b-roll/action/text_overlay/document/
# map/screenshot/generated_visual) instead of ``visual_source``/``suggested_visual``.
# These wrappers translate that into the existing fetch strategy so the rest of
# the pipeline (stored sections, Assembly Validator, Shorts Cutter, Remotion) can
# keep working unchanged.

def fetch_for_beat(beat: dict) -> dict:
    """Fetch stock media for a storyboard beat based on its Claude-decided visual_type.

    ``text_overlay`` and ``generated_visual`` beats need no stock search — they
    render from a dark background (with on-screen text or a placeholder caption
    until real generation is implemented). All other visual types are mapped onto
    ``fetch_for_section``'s pexels/unsplash strategy.

    Args:
        beat: Beat-section dict with at least ``search_query``, ``visual_type``,
              and ``duration_sec`` (from ``storyboard.split_into_beats``).

    Returns:
        Beat dict enriched with ``clips``, ``media_url``, ``media_thumb``,
        ``media_type``, ``media_source``.
    """
    visual_type = beat.get("visual_type", "b-roll")

    if visual_type == "text_overlay":
        clip = {"url": _FALLBACK_URL, "thumb_url": "", "media_type": "image", "source": "text_overlay"}
        beat["clips"]        = [clip]
        beat["media_url"]    = clip["url"]
        beat["media_thumb"]  = ""
        beat["media_type"]   = "image"
        beat["media_source"] = "text_overlay"
        return beat

    if visual_type == "generated_visual":
        clip = {"url": _GENERATED_PLACEHOLDER_URL, "thumb_url": "", "media_type": "image", "source": "generated"}
        beat["clips"]        = [clip]
        beat["media_url"]    = clip["url"]
        beat["media_thumb"]  = ""
        beat["media_type"]   = "image"
        beat["media_source"] = "generated"
        return beat

    beat.setdefault("visual_source", "pexels")
    # "action" also searches video first (dynamic footage); document/map/screenshot
    # are static visuals — go straight to images.
    beat["suggested_visual"] = "b-roll" if visual_type in ("b-roll", "action") else "image"
    return fetch_for_section(beat)


def fetch_all_beats(beats: list[dict]) -> list[dict]:
    """Fetch media for every storyboard beat, applying a dark fallback on failure.

    Args:
        beats: Beat-section dicts from ``storyboard.split_into_beats``.

    Returns:
        Same list with each beat enriched with ``clips`` and media metadata.
    """
    for beat in beats:
        if beat.get("visual_type") in _NO_MEDIA_VISUAL_TYPES:
            fetch_for_beat(beat)
            continue
        try:
            fetch_for_beat(beat)
        except Exception as exc:
            logger.error(
                "Stock fetch failed for beat %s: %s",
                beat.get("beat_order", beat.get("section_order", "?")), exc,
            )
            _apply_dark_fallback(beat)

    video_count    = sum(1 for b in beats if b.get("media_type") == "video")
    image_count    = sum(1 for b in beats if b.get("media_type") == "image")
    placeholder_count = sum(
        1 for b in beats if b.get("media_source") in ("fallback", "text_overlay", "generated")
    )

    logger.info(
        "Beat stock fetch complete: %d beat(s) | video=%d image=%d placeholder=%d",
        len(beats), video_count, image_count, placeholder_count,
    )
    return beats


# ── Internal helpers ──────────────────────────────────────────────────────────

def _clip_count_for_duration(duration_sec: float) -> int:
    """Return how many distinct clips a section needs based on its duration."""
    if duration_sec < 8:
        return 1
    if duration_sec < 20:
        return 2
    return 3


def _fetch_clips(source: str, query: str, visual: str, count: int) -> list[dict]:
    """Fetch up to ``count`` distinct clips from the primary provider."""
    if source == "pexels":
        if visual == "b-roll":
            videos = pexels_videos(query, per_page=min(count * 3, 9))
            if videos:
                return _top_n_videos(videos, count)
        images = pexels_images(query, per_page=min(count * 3, 15))
        return _top_n_images(images, count)

    if source == "unsplash":
        images = unsplash_images(query, per_page=min(count * 3, 15))
        return _top_n_images(images, count)

    return []


def _fetch_clips_fallback(primary_source: str, query: str, count: int) -> list[dict]:
    """Try the other provider, then a generic dark query."""
    other = "unsplash" if primary_source == "pexels" else "pexels"
    logger.info("Primary %s empty — trying %s for query=%r", primary_source, other, query)

    if other == "pexels":
        images = pexels_images(query, per_page=min(count * 3, 15))
    else:
        images = unsplash_images(query, per_page=min(count * 3, 15))

    clips = _top_n_images(images, count)
    if clips:
        return clips

    logger.info("Both providers empty — trying generic dark fallback query")
    fallback_images = pexels_images(_FALLBACK_QUERY, per_page=count * 3)
    return _top_n_images(fallback_images, count)


def _top_n_images(images: list[dict], n: int) -> list[dict]:
    """Return up to n distinct landscape images sorted by quality (area)."""
    if not images:
        return []
    landscape = [i for i in images if i.get("width", 0) > i.get("height", 0)]
    pool = landscape or images
    pool_sorted = sorted(pool, key=lambda i: i.get("width", 0) * i.get("height", 0), reverse=True)
    seen: set[str] = set()
    result = []
    for img in pool_sorted:
        url = img.get("url", "")
        if url and url not in seen:
            seen.add(url)
            result.append({**img, "media_type": "image"})
        if len(result) >= n:
            break
    return result


def _top_n_videos(videos: list[dict], n: int) -> list[dict]:
    """Return up to n distinct HD videos strictly capped at 1920 px wide.

    Any video wider than _MAX_VIDEO_WIDTH is silently dropped — UHD files
    crash Remotion's OffthreadVideo proxy.
    """
    if not videos:
        return []
    # Hard cap: never use anything wider than FHD regardless of fallback tier
    capped = [v for v in videos if v.get("width", 0) <= _MAX_VIDEO_WIDTH]
    fhd    = [v for v in capped if v.get("width", 0) >= 1280]
    pool   = fhd or capped
    if not pool:
        return []   # only UHD available — caller will fall back to images
    pool_sorted = sorted(pool, key=lambda v: v.get("width", 0), reverse=True)
    seen: set[str] = set()
    result = []
    for vid in pool_sorted:
        url = vid.get("url", "")
        if url and url not in seen:
            seen.add(url)
            result.append({**vid, "media_type": "video"})
        if len(result) >= n:
            break
    return result


def _dark_clip() -> dict:
    return {"url": _FALLBACK_URL, "thumb_url": "", "media_type": "image", "source": "fallback"}


def _apply_dark_fallback(section: dict) -> None:
    clip = _dark_clip()
    section["clips"]       = [clip]
    section["media_url"]   = clip["url"]
    section["media_thumb"] = ""
    section["media_type"]  = "image"
    section["media_source"] = "fallback"
