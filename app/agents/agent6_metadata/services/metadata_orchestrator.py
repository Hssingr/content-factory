"""Agent 6 — per-content metadata generation orchestrator.

Roadmap: code_report/agent6_metadata_roadmap.md, Phase D. The real
per-content entrypoint (``run_metadata_generation_for_content()``) will be
called by the Celery task wrapper added in Phase E
(``run_agent6_metadata_for_content`` in app/scheduler/tasks.py), the same
task/service split every other agent uses (CLAUDE.md §6A).

Runtime sequence (stated explicitly because it does not match the phases'
build order and is easy to get backwards — the roadmap's own "Roadmap
Requirements — The thumbnail pipeline" section flags this):
  (a) metadata generation for every (language, platform) pair FIRST, so
      thumbnail_text exists for every language before any overlay is
      attempted;
  (b) the shared base image (Phase C) — independent of (a), may run
      before/after/concurrently; idempotent, keyed on the canonical
      ``thumbnails/{content_id}/base.jpg`` path (Phase C's own contract for
      where a successful generation always ends up), never re-derived from
      the Flux cache hash;
  (c) only once both (a) and (b) have produced their outputs, the
      per-language Pillow overlay + all VideoMetadata persistence.
A language whose metadata call failed (no thumbnail_text) skips its own
overlay — logged, not fatal to other languages, exactly like every other
per-language failure in this codebase degrades independently.
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.agent6_metadata import system_prompt
from app.agents.agent6_metadata.services import platform_limits, thumbnail
from app.config import settings
from app.models import (
    Channel, ChannelConfig, ChannelPlatform, Content, Script, VideoMetadata, VideoRender,
)
from app.services.video_sections import load_video_sections

logger = logging.getLogger(__name__)


def _resolve_story_blueprint(content: Content, db: Session) -> dict:
    """Same "a child-of-parent Short has no own blueprint" fallback
    ``app.agents.agent5_render.services.video._resolve_proper_nouns_for_content()``
    already established (roadmap Finding 4.1) — walk to ``parent_content_id``
    only when this content is a short with no own blueprint and a real
    parent. Never fires for a Solo Short (``parent_content_id`` is ``NULL``
    by definition) or for a parent itself (never short-shaped)."""
    blueprint = content.story_blueprint
    is_short_episode = bool(getattr(content, "is_short_episode", False))
    parent_content_id = getattr(content, "parent_content_id", None)
    if is_short_episode and not blueprint and parent_content_id:
        parent_content = db.get(Content, parent_content_id)
        blueprint = parent_content.story_blueprint if parent_content else None
    return blueprint or {}


def _resolve_languages(content: Content, db: Session) -> list[str]:
    """Languages come from real, already-persisted artifacts — the
    intersection of (a) languages with a real VideoRender row for this
    content (proves the video actually exists to be described) and (b)
    languages with a validated Script row (proves narration text exists to
    summarize) — roadmap Finding 4.5, not from ChannelLanguage."""
    render_format = "short" if bool(getattr(content, "is_short_episode", False)) else "main"
    rendered_languages = {
        r.language
        for r in db.query(VideoRender)
        .filter(VideoRender.content_id == content.id, VideoRender.format == render_format)
        .all()
    }
    validated_script_languages = {
        s.language
        for s in db.query(Script)
        .filter(Script.content_id == content.id, Script.validated.is_(True))
        .all()
    }
    return sorted(rendered_languages & validated_script_languages)


def _resolve_verified_platforms(channel_id: uuid.UUID, db: Session) -> list[str]:
    """Roadmap Finding 4.4: query ChannelPlatform filtered on channel_id +
    verified=True — no shape filtering, text metadata is generated for
    every configured, verified platform regardless of render format."""
    rows = (
        db.query(ChannelPlatform)
        .filter(ChannelPlatform.channel_id == channel_id, ChannelPlatform.verified.is_(True))
        .all()
    )
    return sorted({r.platform for r in rows})


def _format_timestamp(ms: int) -> str:
    total_seconds = max(0, ms) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def build_youtube_timestamps(beats: list[dict]) -> str:
    """Check 5: YouTube description timestamps are Python-computed from real
    VideoSection.audio_start_ms values, never Claude-generated — must match
    the real rendered video exactly. Pure function over already-loaded beats
    (no DB access) so it's directly testable."""
    lines = []
    for beat in beats:
        start_ms = beat.get("audio_start_ms")
        if start_ms is None:
            continue
        label = str(beat.get("script_text") or "").strip()
        if len(label) > 50:
            truncated = label[:50].rsplit(" ", 1)[0]
            label = (truncated or label[:50]) + "…"
        line = f"{_format_timestamp(start_ms)} {label}".strip() if label else _format_timestamp(start_ms)
        lines.append(line)
    return "\n".join(lines)


def _ensure_base_image(content: Content, db: Session) -> str | None:
    """Phase C's base image, idempotent — keyed on the canonical
    ``thumbnails/{content_id}/base.jpg`` path Phase C's own
    ``_place_base_image()`` always writes to on success, never on trying to
    re-derive the Flux prompt-hash cache filename. A content item whose base
    image already exists on disk from a prior run costs zero additional
    Flux calls here."""
    if bool(getattr(content, "is_short_episode", False)):
        return None  # Check 4 — same gate thumbnail.py itself also enforces

    canonical_relative = f"thumbnails/{content.id}/base.jpg"
    canonical_path = Path(settings.media_path) / canonical_relative
    if canonical_path.is_file():
        logger.info(
            "THUMBNAIL_BASE_IMAGE_REUSED content=%s path=%s", content.id, canonical_relative,
        )
        return canonical_relative

    return thumbnail.generate_thumbnail_base_image(content.id, db)


def _upsert_video_metadata(
    db: Session,
    content_id: uuid.UUID,
    language: str,
    platform: str,
    title: str,
    description: str,
    hashtags: list[str],
    thumbnail_text: str | None,
    thumbnail_file_path: str | None,
) -> None:
    existing = (
        db.query(VideoMetadata)
        .filter(
            VideoMetadata.content_id == content_id,
            VideoMetadata.language == language,
            VideoMetadata.platform == platform,
        )
        .first()
    )
    if existing:
        existing.title = title
        existing.description = description
        existing.hashtags = hashtags
        existing.thumbnail_text = thumbnail_text
        existing.thumbnail_file_path = thumbnail_file_path
    else:
        db.add(
            VideoMetadata(
                content_id=content_id,
                language=language,
                platform=platform,
                title=title,
                description=description,
                hashtags=hashtags,
                thumbnail_text=thumbnail_text,
                thumbnail_file_path=thumbnail_file_path,
            )
        )


def run_metadata_generation_for_content(content_id: uuid.UUID, db: Session) -> bool:
    """Run the Agent 6 metadata generation pass for one content item.

    Sequencing (see module docstring): (a) metadata generation for every
    (language, platform) pair, (b) the shared base image, (c) per-language
    overlay + persistence — in that dependency order, not build order.

    Args:
        content_id: UUID of content with status ``RENDERED``.
        db:         SQLAlchemy session managed by the caller.

    Returns:
        ``True`` if at least one VideoMetadata row was produced, ``False``
        otherwise (no languages/platforms to process, or every metadata call
        failed) — mirrors ``run_video_generation()``'s bool contract.
    """
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.warning("METADATA_SKIPPED content=%s reason=content_not_found", content_id)
        return False

    channel: Channel | None = db.get(Channel, content.channel_id)
    config: ChannelConfig | None = db.get(ChannelConfig, content.channel_id)

    languages = _resolve_languages(content, db)
    platforms = _resolve_verified_platforms(content.channel_id, db)
    if not languages or not platforms:
        logger.info(
            "METADATA_SKIPPED content=%s reason=no_languages_or_platforms "
            "languages=%s platforms=%s",
            content_id, languages, platforms,
        )
        return False

    blueprint = _resolve_story_blueprint(content, db)
    scripts_by_language: dict[str, Script] = {
        s.language: s
        for s in db.query(Script)
        .filter(Script.content_id == content_id, Script.validated.is_(True))
        .all()
    }

    # ── (a) metadata generation for every (language, platform) pair ────────
    metadata_results: dict[tuple[str, str], dict | None] = {}
    for language in languages:
        script = scripts_by_language.get(language)
        voice_script = script.voice_script if script else ""
        for platform in platforms:
            metadata_results[(language, platform)] = system_prompt.generate_platform_metadata(
                platform=platform,
                language=language,
                voice_script=voice_script,
                story_blueprint=blueprint,
                niche=channel.niche if channel else "",
                tone=channel.tone if channel else "",
            )

    # ── (b) shared base image — independent of (a) ──────────────────────────
    base_image_path = _ensure_base_image(content, db)

    # ── (c) per-language overlay + persistence, only once (a) and (b) exist ─
    success_count = 0
    for language in languages:
        youtube_timestamps = (
            build_youtube_timestamps(load_video_sections(content_id, language, db))
            if "youtube" in platforms
            else ""
        )
        for platform in platforms:
            result = metadata_results.get((language, platform))
            if result is None:
                logger.warning(
                    "METADATA_GENERATION_FAILED content=%s language=%s platform=%s "
                    "— skipping, no VideoMetadata row for this pair",
                    content_id, language, platform,
                )
                continue

            description = result["description"]
            if platform == "youtube" and youtube_timestamps:
                description = f"{description}\n\n{youtube_timestamps}"

            try:
                title, description, hashtags = platform_limits.enforce_platform_limits(
                    platform, result["title"], description, result["hashtags"],
                )
            except ValueError as exc:
                # An unknown platform (e.g. a stale/legacy ChannelPlatform row
                # for a decommissioned platform not in PLATFORM_LIMITS) must
                # degrade only this (language, platform) pair — never abort
                # the whole content item and lose every other pair's already-
                # generated metadata.
                logger.error(
                    "METADATA_PLATFORM_LIMITS_UNKNOWN_PLATFORM content=%s language=%s "
                    "platform=%s error=%s — skipping this pair only",
                    content_id, language, platform, exc,
                )
                continue

            thumbnail_text: str | None = None
            thumbnail_file_path: str | None = None
            if platform == "youtube":
                raw_thumbnail_text = result.get("thumbnail_text") or ""
                if raw_thumbnail_text and base_image_path:
                    thumbnail_text = platform_limits.enforce_thumbnail_text_limit(
                        raw_thumbnail_text,
                    )
                    thumbnail_file_path = thumbnail.composite_thumbnail_overlay(
                        base_image_path, thumbnail_text, language, content_id,
                    )

            _upsert_video_metadata(
                db, content_id, language, platform,
                title, description, hashtags, thumbnail_text, thumbnail_file_path,
            )
            success_count += 1

    db.commit()
    logger.info(
        "METADATA_GENERATION_DONE content=%s rows=%d languages=%d platforms=%d",
        content_id, success_count, len(languages), len(platforms),
    )
    return success_count > 0
