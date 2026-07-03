"""Deterministic local media validation for persisted Agent 4 visual sections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Content, VideoSection
from app.services.local_run_paths import get_run_root

SUPPORTED_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
TEXT_CARD_SENTINEL = "__text_card__"
_REMOTE_PREFIXES = ("http://", "https://")


@dataclass(frozen=True)
class MediaValidationIssue:
    severity: str
    code: str
    section_order: int | None
    language: str | None
    message: str
    media_path: str | None = None


@dataclass(frozen=True)
class MediaValidationResult:
    passed: bool
    blocking_issues: list[MediaValidationIssue]
    warnings: list[MediaValidationIssue]
    checked_count: int
    valid_local_media_count: int
    missing_media_count: int
    remote_media_count: int
    text_card_count: int


def validate_visual_media_assets(
    content_id: int | str | UUID,
    db: Session,
    *,
    language: str | None = None,
) -> MediaValidationResult:
    """Validate persisted Agent 4 media references before visual-ready status."""
    content = db.get(Content, content_id)
    if content is None:
        issue = MediaValidationIssue(
            severity="BLOCKING",
            code="content_missing",
            section_order=None,
            language=language,
            message=f"Content {content_id} not found for media validation.",
        )
        return _result(blocking=[issue])

    rows = _load_sections(content_id, db, language=language)
    if not rows:
        issue = MediaValidationIssue(
            severity="BLOCKING",
            code="video_sections_missing",
            section_order=None,
            language=language,
            message="No persisted VideoSection rows found; visual-ready status requires persisted sections.",
        )
        return _result(blocking=[issue])

    blocking: list[MediaValidationIssue] = []
    warnings: list[MediaValidationIssue] = []
    valid_local_media_count = 0
    missing_media_count = 0
    remote_media_count = 0
    text_card_count = 0

    for row in rows:
        inspected = _inspect_section(row)
        if inspected.metadata_warning:
            warnings.append(_issue(row, "metadata_unreadable", inspected.metadata_warning, severity="WARNING"))

        if inspected.is_text_card:
            text_card_count += 1
            if not inspected.media_path or inspected.media_path == TEXT_CARD_SENTINEL:
                warnings.append(_issue(
                    row,
                    "text_card_without_local_image",
                    "Intentional text-card section has no local image; allowed by current architecture.",
                    inspected.media_path,
                    severity="WARNING",
                ))
                continue

        if inspected.media_type and inspected.media_type != "image":
            blocking.append(_issue(
                row,
                "unsupported_media_type",
                f"Persisted media_type={inspected.media_type!r} is not supported for Agent 4 local render readiness.",
                inspected.media_path,
            ))
            continue

        media_path = inspected.media_path
        if not media_path:
            missing_media_count += 1
            blocking.append(_issue(
                row,
                "media_path_missing",
                "Required local media path is missing.",
                media_path,
            ))
            continue

        if media_path.lower().startswith(_REMOTE_PREFIXES):
            remote_media_count += 1
            blocking.append(_issue(
                row,
                "remote_media_url",
                "Remote media URL is not valid for local Agent 4 render readiness.",
                media_path,
            ))
            continue

        if media_path == TEXT_CARD_SENTINEL:
            text_card_count += 1
            warnings.append(_issue(
                row,
                "text_card_sentinel",
                "Text-card sentinel is allowed and has no local image to validate.",
                media_path,
                severity="WARNING",
            ))
            continue

        resolved, path_issue = _resolve_safe_media_path(media_path, content_id)
        if path_issue is not None:
            blocking.append(_issue(row, path_issue[0], path_issue[1], media_path))
            continue

        assert resolved is not None
        file_issue = _validate_local_image_file(resolved)
        if file_issue is not None:
            if file_issue[0] == "local_media_missing":
                missing_media_count += 1
            blocking.append(_issue(row, file_issue[0], file_issue[1], media_path))
            continue

        valid_local_media_count += 1

    return _result(
        blocking=blocking,
        warnings=warnings,
        checked_count=len(rows),
        valid_local_media_count=valid_local_media_count,
        missing_media_count=missing_media_count,
        remote_media_count=remote_media_count,
        text_card_count=text_card_count,
    )


def _load_sections(
    content_id: int | str | UUID,
    db: Session,
    *,
    language: str | None,
) -> list[VideoSection]:
    query = db.query(VideoSection).filter(VideoSection.content_id == content_id)
    if language:
        query = query.filter(VideoSection.language == language)
    return list(query.order_by(VideoSection.language, VideoSection.section_order).all())


@dataclass(frozen=True)
class _InspectedSection:
    media_path: str
    media_type: str
    visual_type: str
    media_strategy: str
    is_text_card: bool
    metadata_warning: str | None


def _inspect_section(section: VideoSection) -> _InspectedSection:
    metadata, warning = _parse_metadata(getattr(section, "generation_prompt", None))
    media_path = _first_text(metadata.get("media_url"), metadata.get("image_path"), metadata.get("media_path"))
    media_type = _first_text(metadata.get("media_type"), "image")
    visual_type = _first_text(metadata.get("visual_type"))
    media_strategy = _first_text(getattr(section, "media_strategy", None), metadata.get("media_strategy"))
    text_card_style = _first_text(getattr(section, "text_card_style", None), metadata.get("text_card_style"))
    is_text_card = (
        media_path == TEXT_CARD_SENTINEL
        or visual_type == "text_card"
        or media_strategy == "remotion_text_card"
        or bool(text_card_style and text_card_style != "default" and visual_type == "text_card")
    )
    return _InspectedSection(
        media_path=media_path,
        media_type=media_type,
        visual_type=visual_type,
        media_strategy=media_strategy,
        is_text_card=is_text_card,
        metadata_warning=warning,
    )


def _parse_metadata(raw: str | None) -> tuple[dict[str, Any], str | None]:
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return {}, f"generation_prompt JSON could not be parsed: {exc}"
    if not isinstance(parsed, dict):
        return {}, "generation_prompt JSON is not an object."
    return parsed, None


def _resolve_safe_media_path(media_path: str, content_id: int | str | UUID) -> tuple[Path | None, tuple[str, str] | None]:
    media_root = Path(settings.media_path).expanduser().resolve()
    run_root = get_run_root(content_id).resolve()
    raw = Path(media_path).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (media_root / raw).resolve()

    if not (_is_relative_to(candidate, media_root) or _is_relative_to(candidate, run_root)):
        return None, (
            "unsafe_media_path",
            "Media path resolves outside the configured media root/run root.",
        )
    return candidate, None


def _validate_local_image_file(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return "local_media_missing", "Local media file does not exist."
    if not path.is_file():
        return "local_media_not_file", "Local media path is not a file."
    if path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        return "unsupported_media_extension", f"Unsupported local media extension {path.suffix!r}."
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "local_media_unreadable", f"Local media file cannot be stat-ed: {exc}"
    if size == 0:
        return "local_media_zero_byte", "Local media file is zero bytes."

    try:
        with Image.open(path) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return "local_image_unreadable", f"Local image file cannot be opened/read: {exc}"
    return None


def _issue(
    section: VideoSection,
    code: str,
    message: str,
    media_path: str | None = None,
    *,
    severity: str = "BLOCKING",
) -> MediaValidationIssue:
    return MediaValidationIssue(
        severity=severity,
        code=code,
        section_order=getattr(section, "section_order", None),
        language=getattr(section, "language", None),
        message=message,
        media_path=media_path,
    )


def _result(
    *,
    blocking: list[MediaValidationIssue],
    warnings: list[MediaValidationIssue] | None = None,
    checked_count: int = 0,
    valid_local_media_count: int = 0,
    missing_media_count: int = 0,
    remote_media_count: int = 0,
    text_card_count: int = 0,
) -> MediaValidationResult:
    return MediaValidationResult(
        passed=not blocking,
        blocking_issues=blocking,
        warnings=warnings or [],
        checked_count=checked_count,
        valid_local_media_count=valid_local_media_count,
        missing_media_count=missing_media_count,
        remote_media_count=remote_media_count,
        text_card_count=text_card_count,
    )


def _first_text(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
