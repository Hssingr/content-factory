"""Remotion composition builder — assembles all video data into a JSON props file.

The props file is the single source of truth consumed by the Remotion React project.
It is written to ``{media_path}/remotion_props/`` and passed to the Remotion CLI.

Layout of the main props file:
  {
    "content_id": "...",
    "language": "fr",
    "audio_file": "/media/audio/.../fr.mp3",
    "duration_ms": 479300,
    "sections": [...],
    "subtitles": {"style": "standard", "captions": [...]},
    "shorts": [{"part_label": "Partie 1/8", "sections": [...], "subtitles": {...}, ...}],
    "config": {"style": "documentary", "color_grade": "desaturated"}
  }

A separate props file is written for each Short so it can be rendered independently:
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
    "part_label": "Partie 1/8",
    "total_parts": 8,
    "config": {"style": "documentary", "color_grade": "desaturated"}
  }
"""

import json
import logging
import os
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


def build_main_props(
    content_id: str,
    language: str,
    audio_file_path: str,
    duration_ms: int,
    sections: list[dict],
    standard_subtitles: list[dict],
    shorts: list[dict],
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
        shorts:              Short segments from shorts_cutter (with section lists).
        karaoke_subtitles:   Karaoke chunks for the Shorts (shared across all Shorts).
        channel_style:       ``video_style_type`` from channel_config.
        channel_color_grade: ``video_color_grade`` from channel_config.

    Returns:
        Absolute path to the written props JSON file.
    """
    props_dir = _ensure_props_dir()
    file_name = f"{content_id}_{language}_main.json"
    file_path = props_dir / file_name

    # Attach karaoke subtitles to each Short
    shorts_with_subs = []
    for short in shorts:
        # Filter karaoke captions to those within the Short's time range
        short_start = short.get("start_ms", 0)
        short_end   = short.get("end_ms", 0)
        short_captions = [
            c for c in karaoke_subtitles
            if c.get("start_ms", 0) >= short_start and c.get("end_ms", 0) <= short_end
        ]
        shorts_with_subs.append({
            **short,
            "subtitles": {"style": "karaoke", "captions": short_captions},
        })

    props = {
        "content_id": content_id,
        "language":   language,
        "audio_file": audio_file_path,
        "duration_ms": duration_ms,
        "sections": [_section_for_remotion(s) for s in sections],
        "subtitles": {"style": "standard", "captions": standard_subtitles},
        "shorts": shorts_with_subs,
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
        short:               Short dict from shorts_cutter.
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
        "audio_file":  audio_file_path,
        "short_index": short_index,
        "start_ms":    short_start,
        "end_ms":      short_end,
        "duration_ms": short_end - short_start,
        "sections":    [_section_for_remotion(s) for s in short.get("sections", [])],
        "subtitles":   {"style": "karaoke", "captions": short_captions},
        "part_label":  short.get("part_label", ""),
        "total_parts": short.get("total_parts", 1),
        "hook_modified": short.get("hook_modified", True),
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
    path = Path(settings.media_path) / "remotion_props"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(file_path: Path, data: dict) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _section_for_remotion(s: dict) -> dict:
    """Return only the keys Remotion needs from a section dict."""
    return {
        "order":         s.get("section_order", 0),
        "media_url":     s.get("media_url", ""),
        "media_thumb":   s.get("media_thumb", ""),
        "media_type":    s.get("media_type", "image"),
        "effect":        s.get("effect", "slow_zoom"),
        "color_grade":   s.get("color_grade", "desaturated"),
        "audio_start_ms": s.get("audio_start_ms", 0),
        "audio_end_ms":   s.get("audio_end_ms", 0),
    }
