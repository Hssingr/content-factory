"""Shared VideoSection loader for the visual/render pipeline (roadmap 6.5 / audit AR-2).

Agent 4 (`visual_orchestrator.py`) and Agent 5 (`video.py`) each carried their
own byte-identical copy of this loader. It is a pure read with no agent-owned
business logic — a shared deterministic utility per CLAUDE.md §7 (Shared
Services Architecture), not a new architectural layer.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy.orm import Session

from app.models import VideoSection

# Legacy sentinel that pre-subtitles-only code wrote onto a beat's media_url
# when Flux generation failed. Normalized away on load so callers never see it.
_TEXT_CARD_SENTINEL = "__text_card__"


def load_video_sections(content_id: uuid.UUID, language: str, db: Session) -> list[dict]:
    """Load VideoSection rows as dicts compatible with the visual/render pipeline.

    Args:
        content_id: Content row id.
        language:   BCP-47 language code, or `"__visual__"` for the shared
                    parent storyboard/Flux pass.
        db:         SQLAlchemy session.

    Returns:
        List of beat-section dicts ordered by `section_order`, with
        `generation_prompt` JSON extras merged in and legacy text-card
        fields normalized away.
    """
    rows = (
        db.query(VideoSection)
        .filter(
            VideoSection.content_id == content_id,
            VideoSection.language == language,
        )
        .order_by(VideoSection.section_order)
        .all()
    )
    result: list[dict] = []
    for s in rows:
        section: dict = {
            "section_order": s.section_order,
            "beat_order": s.section_order,
            "script_text": s.script_text,
            "audio_start_ms": s.audio_start_ms,
            "audio_end_ms": s.audio_end_ms,
            "duration_sec": (s.audio_end_ms - s.audio_start_ms) / 1000,
            "flux_prompt": s.flux_prompt or "",
            "effect": s.effect or "slow_zoom",
            "color_grade": s.color_grade or "desaturated",
            "beat_intensity": s.beat_intensity or "medium",
            "suggested_duration_sec": s.suggested_duration_sec,
            "media_strategy": s.media_strategy or "flux_generated",
            "text_card_style": s.text_card_style or "default",
        }
        if s.generation_prompt:
            try:
                extras = json.loads(s.generation_prompt)
            except (json.JSONDecodeError, TypeError):
                extras = {}
            if isinstance(extras, dict):
                section.update(extras)

        # Legacy-row normalization (subtitles-only rendering, audit G-0/G-8):
        # rows persisted before text cards were removed may still carry the
        # retired fields. Normalize on load so downstream code only ever sees
        # flux_generated image beats — a legacy sentinel media_url becomes ""
        # (pending), which the flux-incomplete detection regenerates for real.
        if section.get("media_url") == _TEXT_CARD_SENTINEL:
            section["media_url"] = ""
        if section.get("visual_type") == "text_card":
            section["visual_type"] = "b-roll"
        if section.get("media_strategy") == "remotion_text_card":
            section["media_strategy"] = "flux_generated"
        section["overlay_text"] = ""
        section["overlay_position"] = "none"
        result.append(section)
    return result
