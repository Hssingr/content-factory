"""Remotion composition builder — assembles all video data into a JSON props file.

The props file is the single source of truth consumed by the Remotion React project.
It is written to ``{media_path}/remotion_props/`` and passed to the Remotion CLI.

Layout of the main props file (build_main_props):
  {
    "content_id": "...",
    "language": "fr",
    "audio_file": "/media/audio/.../fr.mp3",
    "duration_ms": 479300,
    "sections": [...],
    "subtitles": {"style": "standard", "captions": [...]},
    "config": {"style": "documentary", "color_grade": "desaturated"}
  }

Layout of a Short props file (build_short_props — used by Standalone short architecture child short episodes
when they eventually render with the Short.tsx 9:16 composition):
  {
    "content_id": "...",
    "language": "fr",
    "audio_file": "/media/audio/.../fr.mp3",
    "short_index": 0,
    "start_ms": 0,
    "end_ms": 57000,
    "duration_ms": 57000,
    "sections": [...],
    "subtitles": {"style": "karaoke", "captions": [...]},
    "part_label": "Partie 1/3",
    "total_parts": 3,
    "config": {"style": "documentary", "color_grade": "desaturated"}
  }
"""

import json
import logging
import os
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


def _audio_rel(audio_file_path: str) -> str:
    """Return audio_file_path relative to media_path (for Remotion staticFile)."""
    media_root = Path(settings.media_path).resolve()
    try:
        return str(Path(audio_file_path).resolve().relative_to(media_root))
    except ValueError:
        # Path outside media_root — return as-is and let Remotion handle it
        return audio_file_path


def build_main_props(
    content_id: str,
    language: str,
    audio_file_path: str,
    duration_ms: int,
    sections: list[dict],
    standard_subtitles: list[dict],
    karaoke_subtitles: list[dict],
    channel_style: str = "documentary",
    channel_color_grade: str = "desaturated",
) -> str:
    """Write the main video props JSON and return the file path.

    Args:
        content_id:          UUID of the content record.
        language:            Language code (e.g. "fr").
        audio_file_path:     Absolute path to the language audio file.
        duration_ms:         Total audio duration in milliseconds.
        sections:            All validated + media-enriched sections.
        standard_subtitles:  Caption chunks for the 16:9 video.
        karaoke_subtitles:   Karaoke chunks (kept for future use; not embedded here).
        channel_style:       ``video_style_type`` from channel_config.
        channel_color_grade: ``video_color_grade`` from channel_config.

    Returns:
        Absolute path to the written props JSON file.
    """
    props_dir = _ensure_props_dir()
    file_name = f"{content_id}_{language}_main.json"
    file_path = props_dir / file_name

    props = {
        "content_id": content_id,
        "language":   language,
        "audio_file": _audio_rel(audio_file_path),   # relative to media_path (Remotion --public-dir)
        "duration_ms": duration_ms,
        "sections": [_section_for_remotion(s) for s in sections],
        "subtitles": {"style": "standard", "captions": standard_subtitles},
        "config": {
            "style":       channel_style,
            "color_grade": channel_color_grade,
        },
    }

    _write_json(file_path, props)
    logger.info("Main props written: %s", file_path)
    return str(file_path)


def build_short_props(
    content_id: str,
    language: str,
    audio_file_path: str,
    short: dict,
    karaoke_subtitles: list[dict],
    channel_style: str = "documentary",
    channel_color_grade: str = "desaturated",
) -> str:
    """Write a props JSON file for a single Short and return the file path.

    Args:
        content_id:          UUID of the content record.
        language:            Language code.
        audio_file_path:     Absolute path to the language audio file.
        short:               Short segment dict with short_index, start_ms, end_ms, sections, etc.
        karaoke_subtitles:   All karaoke captions (filtered to this Short's window).
        channel_style:       ``video_style_type`` from channel_config.
        channel_color_grade: ``video_color_grade`` from channel_config.

    Returns:
        Absolute path to the written props JSON file.
    """
    props_dir   = _ensure_props_dir()
    short_index = short["short_index"]
    file_name   = f"{content_id}_{language}_short_{short_index}.json"
    file_path   = props_dir / file_name

    short_start = short.get("start_ms", 0)
    short_end   = short.get("end_ms", 0)
    short_captions = [
        c for c in karaoke_subtitles
        if c.get("start_ms", 0) >= short_start and c.get("end_ms", 0) <= short_end
    ]

    props = {
        "content_id":  content_id,
        "language":    language,
        "audio_file":  _audio_rel(audio_file_path),  # relative to media_path
        "short_index": short_index,
        "start_ms":    short_start,
        "end_ms":      short_end,
        "duration_ms": short_end - short_start,
        "sections":    [_section_for_remotion(s) for s in short.get("sections", [])],
        "subtitles":   {"style": "karaoke", "captions": short_captions},
        "part_label":  short.get("part_label", ""),
        "total_parts": short.get("total_parts", 1),
        "config": {
            "style":       channel_style,
            "color_grade": channel_color_grade,
        },
    }

    _write_json(file_path, props)
    logger.info("Short %d props written: %s", short_index, file_path)
    return str(file_path)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ensure_props_dir() -> Path:
    path = Path(settings.media_path).resolve() / "remotion_props"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(file_path: Path, data: dict) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _assert_local_url(url: str, context: str) -> None:
    """Raise ValueError if url is a remote http(s) URL.

    Remotion must never receive a remote URL — all assets must be local
    cache/ paths (resolved via Remotion's staticFile / --public-dir).

    Args:
        url:     The media URL to check.
        context: Human-readable identifier for error messages (e.g. "section 3 clip 0").

    Raises:
        ValueError: If url starts with "http://" or "https://".
    """
    if url.startswith("http"):
        raise ValueError(
            f"Remotion builder invariant violated — remote URL in {context}: {url[:120]!r}. "
            "All media must be downloaded to local cache before building props."
        )


def _section_for_remotion(s: dict) -> dict:
    """Return only the keys Remotion needs from a section dict.

    Under the Flux architecture every section has exactly one image in ``media_url``
    (a local cache/ path). Deliberate text-card beats also carry a generated
    background image; Remotion renders the readable text overlay itself.

    All media URLs are validated to be local paths (not http/https) before the
    props are written. This invariant is also enforced by _audit_props_for_remote_urls
    in video.py before the render starts.

    Raises:
        ValueError: If media_url is a remote http URL.
    """
    order      = s.get("section_order", 0)
    visual_type = s.get("visual_type", "b-roll")
    is_text_card = visual_type == "text_card"

    media_url = s.get("media_url", "")
    if media_url and media_url != "__text_card__":
        _assert_local_url(media_url, f"section {order} media_url")
    else:
        media_url = ""
    media_type = s.get("media_type", "image")
    clips = [{"url": media_url, "type": media_type}] if media_url else []

    if is_text_card and media_url:
        media_type = "image"
        clips = [{"url": media_url, "type": media_type}]

    return {
        "order":          order,
        "clips":          clips,
        "media_url":      media_url,
        "media_type":     media_type,
        "effect":         s.get("effect", "slow_zoom"),
        "color_grade":    s.get("color_grade", "desaturated"),
        "audio_start_ms": s.get("audio_start_ms", 0),
        "audio_end_ms":   s.get("audio_end_ms", 0),
        "visual_intent":      s.get("visual_intent", ""),
        "visual_type":        visual_type,
        "transition_to_next": s.get("transition_to_next", "cut"),
        "overlay_text":       s.get("overlay_text", "") or s.get("script_text", ""),
        "overlay_position":   s.get("overlay_position", "none"),
        "text_card_style":    s.get("text_card_style", "default"),
    }
