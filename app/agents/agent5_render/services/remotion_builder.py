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
    "subtitles": {"style": "standard", "captions": [...]}
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
    "total_parts": 3
  }
"""

import json
import logging
import os
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# Roadmap 2b / audit P0-2 (code_report/forensic_output_audit_borrasca_run.md):
# mirrors Agent 3's cross-timeline invariant at the props-building layer.
_TIMELINE_DRIFT_TOLERANCE = 0.02  # 2%
_REMOTION_FPS = 30


def _tile_section_frame_ranges(
    sections: list[dict], *, timeline_start_ms: int, duration_ms: int,
    fps: int = _REMOTION_FPS,
) -> list[tuple[int, int]]:
    """Convert shared millisecond boundaries to gap-free nominal frame ranges.

    Every internal boundary is rounded exactly once and shared by both adjacent
    beats. This avoids the one-frame holes caused by independently rounding a
    beat's start and duration (e.g. 59,680→64,125 ms at 30 fps).
    """
    if not sections:
        return []
    starts = [
        round(((int(section.get("audio_start_ms", 0)) - timeline_start_ms) / 1000) * fps)
        for section in sections
    ]
    starts[0] = 0
    total_frames = max(1, -(-duration_ms * fps // 1000))
    ranges: list[tuple[int, int]] = []
    for index, start in enumerate(starts):
        start = max(0, start)
        end = starts[index + 1] if index + 1 < len(starts) else total_frames
        ranges.append((start, max(start + 1, end)))
    return ranges


def _sections_for_remotion(
    sections: list[dict], *, timeline_start_ms: int, duration_ms: int,
) -> list[dict]:
    ranges = _tile_section_frame_ranges(
        sections, timeline_start_ms=timeline_start_ms, duration_ms=duration_ms,
    )
    return [
        {
            **_section_for_remotion(section),
            "render_start_frame": start_frame,
            "render_end_frame": end_frame,
        }
        for section, (start_frame, end_frame) in zip(sections, ranges)
    ]


def _assert_timeline_alignment(
    *,
    duration_ms: int,
    sections: list[dict],
    captions: list[dict],
    context: str,
) -> None:
    """Raise ValueError if the last caption end or last section end drifts
    from ``duration_ms`` by more than ``_TIMELINE_DRIFT_TOLERANCE``.

    Roadmap 2b / audit P0-2: three independent timelines flow into a render
    (``AudioFile.duration_ms``, Whisper-derived captions, ``VideoSection``-
    derived section spans) and a real production run shipped with captions
    ending at 616,580 ms while sections and the DB's own ``duration_ms``
    ended at 161,724 ms — nothing anywhere compared the three, so the props
    file silently carried both numbers into the same render. This mirrors
    ``agent3_audio.services.audio._assert_duration_transcript_alignment()``
    at the last checkpoint before a corrupted timeline reaches Remotion.

    Args:
        duration_ms: Expected audio duration in milliseconds.
        sections:    Raw section/beat dicts (before `_section_for_remotion()`
                     field trimming — `audio_end_ms` is unaffected either way).
        captions:    Caption chunks as they will be embedded in the props file.
        context:     Human-readable identifier for the error message.

    Raises:
        ValueError: If either comparison exceeds the tolerance.
    """
    if duration_ms <= 0:
        return

    last_section_end_ms = max((s.get("audio_end_ms") or 0 for s in sections), default=0)
    last_caption_end_ms = max((c.get("end_ms") or 0 for c in captions), default=0)

    mismatches: list[str] = []
    if last_section_end_ms > 0:
        drift = abs(last_section_end_ms - duration_ms) / duration_ms
        if drift > _TIMELINE_DRIFT_TOLERANCE:
            mismatches.append(
                f"last_section_end_ms={last_section_end_ms} vs duration_ms={duration_ms} "
                f"(drift={drift:.1%})"
            )
    if last_caption_end_ms > 0:
        drift = abs(last_caption_end_ms - duration_ms) / duration_ms
        if drift > _TIMELINE_DRIFT_TOLERANCE:
            mismatches.append(
                f"last_caption_end_ms={last_caption_end_ms} vs duration_ms={duration_ms} "
                f"(drift={drift:.1%})"
            )

    if mismatches:
        raise ValueError(
            f"Timeline alignment invariant violated in {context}: " + "; ".join(mismatches)
        )


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

    Returns:
        Absolute path to the written props JSON file.
    """
    _assert_timeline_alignment(
        duration_ms=duration_ms,
        sections=sections,
        captions=standard_subtitles,
        context=f"main props content={content_id} language={language}",
    )

    props_dir = _ensure_props_dir()
    file_name = f"{content_id}_{language}_main.json"
    file_path = props_dir / file_name

    props = {
        "content_id": content_id,
        "language":   language,
        "audio_file": _audio_rel(audio_file_path),   # relative to media_path (Remotion --public-dir)
        "duration_ms": duration_ms,
        "sections": _sections_for_remotion(
            sections, timeline_start_ms=0, duration_ms=duration_ms,
        ),
        "subtitles": {"style": "standard", "captions": standard_subtitles},
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
) -> str:
    """Write a props JSON file for a single Short and return the file path.

    Args:
        content_id:          UUID of the content record.
        language:            Language code.
        audio_file_path:     Absolute path to the language audio file.
        short:               Short segment dict with short_index, start_ms, end_ms, sections, etc.
        karaoke_subtitles:   All karaoke captions (filtered to this Short's window).

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

    _assert_timeline_alignment(
        duration_ms=short_end - short_start,
        sections=short.get("sections", []),
        captions=short_captions,
        context=f"short props content={content_id} language={language} short_index={short_index}",
    )

    props = {
        "content_id":  content_id,
        "language":    language,
        "audio_file":  _audio_rel(audio_file_path),  # relative to media_path
        "short_index": short_index,
        "start_ms":    short_start,
        "end_ms":      short_end,
        "duration_ms": short_end - short_start,
        "sections":    _sections_for_remotion(
            short.get("sections", []),
            timeline_start_ms=short_start,
            duration_ms=short_end - short_start,
        ),
        "subtitles":   {"style": "karaoke", "captions": short_captions},
        "part_label":  short.get("part_label", ""),
        "total_parts": short.get("total_parts", 1),
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

    Subtitles-only rendering (audit G-0/G-8): Remotion draws no text except
    the subtitle track, so no overlay/text-card key is emitted — the retired
    ``overlay_text`` fallback to ``script_text`` once made a failed beat
    display its entire narration as a giant card, which is exactly the class
    of defect this rule removes. Every section is an image beat; a legacy
    ``text_card`` visual_type or ``__text_card__`` sentinel from old rows is
    normalized to a plain image section (with no media, if none exists — the
    render blocker owns that failure).

    All media URLs are validated to be local paths (not http/https) before the
    props are written. This invariant is also enforced by _audit_props_for_remote_urls
    in video.py before the render starts.

    Raises:
        ValueError: If media_url is a remote http URL.
    """
    order       = s.get("section_order", 0)
    visual_type = s.get("visual_type", "b-roll")
    if visual_type == "text_card":
        visual_type = "b-roll"

    media_url = s.get("media_url", "")
    if media_url and media_url != "__text_card__":
        _assert_local_url(media_url, f"section {order} media_url")
    else:
        media_url = ""
    media_type = s.get("media_type", "image")
    clips = [{"url": media_url, "type": media_type}] if media_url else []

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
    }
