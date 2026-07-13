"""Deterministic local media validation for persisted Agent 4 visual sections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from PIL import Image, ImageStat, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Content, VideoSection
from app.services.local_run_paths import get_run_root

SUPPORTED_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
TEXT_CARD_SENTINEL = "__text_card__"
_REMOTE_PREFIXES = ("http://", "https://")
_PARENT_IMAGE_ASPECT_RATIO = 16 / 9
_SHORT_IMAGE_ASPECT_RATIO = 9 / 16
_IMAGE_ASPECT_TOLERANCE = 0.03
# Same floor as flux_generator.py's generation-time luminance gate — this is
# the backstop for a near-black frame that reached persistence anyway (e.g.
# a row generated before the gate existed, or CSS color-grade filters could
# still darken a borderline-passing frame further at render time).
_NEAR_BLACK_LUMINANCE_FLOOR = 24.0


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
    beats_by_lang: dict[str, list[dict]] | None = None,
) -> MediaValidationResult:
    """Validate persisted Agent 4 media references before visual-ready status.

    Roadmap 6.4 / audit V-7, AR-2: this is now the single media validator —
    `storyboard_validator.validate_media_assets()` (a separate, per-language,
    observability-only check that ran during generation) was folded in here
    and deleted; this function is the sole remaining media validator and the
    sole one that can block visual-ready status.

    Args:
        content_id:    Content row id.
        db:            SQLAlchemy session.
        language:      Optional filter to a single language's rows.
        beats_by_lang: Optional ``{language: [beat dict, ...]}`` — the
            in-memory beats each language was generated with, as returned by
            `run_visual_generation()`. When provided, additionally runs the
            persistence round-trip check (folded in from the deleted
            `visual_orchestrator._check_media_assets()`): each beat's
            `media_url`/`media_type` is compared against what was actually
            reloaded from the DB, catching a future `_build_beat_section()`-
            style bug where a field silently changes across persistence.
            Round-trip findings are diagnostic — like the original check,
            they are always appended to `warnings`, never `blocking_issues`,
            so this call's `passed` result is unaffected either way.
    """
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

        # Subtitles-only rendering (audit G-0/G-8): the text-card exemption is
        # retired — every section, legacy text-card rows included, must carry a
        # real local image. A legacy row still marked as a text card is counted
        # for observability but validated exactly like any other section.
        if inspected.is_text_card:
            text_card_count += 1

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
            # Legacy failure sentinel — text cards are removed product-wide;
            # a section still carrying it has no real image and blocks
            # visual-ready status (regenerate with --force-visuals).
            missing_media_count += 1
            blocking.append(_issue(
                row,
                "legacy_text_card_sentinel",
                "Legacy '__text_card__' sentinel found — text cards are removed "
                "(subtitles-only rendering); this section has no local image.",
                media_path,
            ))
            continue

        resolved, path_issue = _resolve_safe_media_path(media_path, content_id)
        if path_issue is not None:
            blocking.append(_issue(row, path_issue[0], path_issue[1], media_path))
            continue

        assert resolved is not None
        file_issue = _validate_local_image_file(
            resolved,
            expected_aspect_ratio=_expected_image_aspect_ratio(content),
        )
        if file_issue is not None:
            if file_issue[0] == "local_media_missing":
                missing_media_count += 1
            blocking.append(_issue(row, file_issue[0], file_issue[1], media_path))
            continue

        luminance_issue = _validate_image_luminance(resolved)
        if luminance_issue is not None:
            blocking.append(_issue(row, luminance_issue[0], luminance_issue[1], media_path))
            continue

        valid_local_media_count += 1

    if beats_by_lang:
        warnings.extend(_check_persistence_round_trip(rows, beats_by_lang))

    return _result(
        blocking=blocking,
        warnings=warnings,
        checked_count=len(rows),
        valid_local_media_count=valid_local_media_count,
        missing_media_count=missing_media_count,
        remote_media_count=remote_media_count,
        text_card_count=text_card_count,
    )


def _check_persistence_round_trip(
    rows: list[VideoSection],
    beats_by_lang: dict[str, list[dict]],
) -> list[MediaValidationIssue]:
    """Compare each in-memory beat against what was actually reloaded from
    the DB, per (language, section_order). Diagnostic only — folded in from
    the deleted `visual_orchestrator._check_media_assets()` (roadmap 6.4)."""
    reloaded_by_key: dict[tuple[str, int], VideoSection] = {
        (row.language, row.section_order): row for row in rows
    }
    issues: list[MediaValidationIssue] = []

    for lang, beats in beats_by_lang.items():
        for beat in beats:
            order = beat.get("section_order", beat.get("beat_order", 0))
            reloaded = reloaded_by_key.get((lang, order))
            if reloaded is None:
                issues.append(MediaValidationIssue(
                    severity="WARNING",
                    code="persistence_row_missing",
                    section_order=order,
                    language=lang,
                    message=(
                        f"beat_order={order} was saved but no matching VideoSection "
                        f"row was found on reload (language={lang})."
                    ),
                ))
                continue

            reloaded_inspected = _inspect_section(reloaded)
            expected_url = beat.get("media_url", "")
            if expected_url != reloaded_inspected.media_path:
                issues.append(MediaValidationIssue(
                    severity="WARNING",
                    code="persistence_media_url_mismatch",
                    section_order=order,
                    language=lang,
                    message=(
                        f"beat_order={order} media_url changed across persistence: "
                        f"saved={expected_url!r} but reloaded={reloaded_inspected.media_path!r} "
                        f"(language={lang})."
                    ),
                    media_path=expected_url,
                ))

            expected_type = beat.get("media_type", "image")
            if expected_type != reloaded_inspected.media_type:
                issues.append(MediaValidationIssue(
                    severity="WARNING",
                    code="persistence_media_type_mismatch",
                    section_order=order,
                    language=lang,
                    message=(
                        f"beat_order={order} media_type changed across persistence: "
                        f"saved={expected_type!r} but reloaded={reloaded_inspected.media_type!r} "
                        f"(language={lang})."
                    ),
                ))

    return issues


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


def _expected_image_aspect_ratio(content: Content) -> float:
    return _SHORT_IMAGE_ASPECT_RATIO if getattr(content, "is_short_episode", False) else _PARENT_IMAGE_ASPECT_RATIO


def _validate_local_image_file(
    path: Path,
    *,
    expected_aspect_ratio: float | None = None,
) -> tuple[str, str] | None:
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
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return "local_image_unreadable", f"Local image file cannot be opened/read: {exc}"

    if expected_aspect_ratio is not None:
        dimension_issue = _validate_image_dimensions(width, height, expected_aspect_ratio)
        if dimension_issue is not None:
            return dimension_issue
    return None


def _validate_image_dimensions(
    width: int,
    height: int,
    expected_aspect_ratio: float,
) -> tuple[str, str] | None:
    if width <= 0 or height <= 0:
        return "local_image_invalid_dimensions", f"Local image has invalid dimensions {width}x{height}."

    actual = width / height
    relative_delta = abs(actual - expected_aspect_ratio) / expected_aspect_ratio
    if relative_delta > _IMAGE_ASPECT_TOLERANCE:
        return (
            "local_image_aspect_mismatch",
            f"Local image dimensions {width}x{height} have aspect {actual:.3f}; "
            f"expected {expected_aspect_ratio:.3f} within {_IMAGE_ASPECT_TOLERANCE:.0%}.",
        )
    return None


def _validate_image_luminance(path: Path) -> tuple[str, str] | None:
    """Reject a persisted image that is pure-black or near-black.

    A real production run persisted a beat whose Flux generation was a 100%
    black JPEG — it passed every other check here, since none of them
    inspect actual pixel content. Opens the file independently of
    ``_validate_local_image_file()`` (which already called ``.verify()`` on
    its own handle — PIL documents ``verify()`` as a one-shot, load-before-
    anything-else call, so pixel data is read from a fresh handle here
    rather than reused).
    """
    try:
        with Image.open(path) as image:
            mean = ImageStat.Stat(image.convert("L")).mean[0]
    except (UnidentifiedImageError, OSError, ValueError):
        # Already caught by _validate_local_image_file()'s own open/verify —
        # do not double-report a corrupt/unreadable file under a second code.
        return None
    if mean < _NEAR_BLACK_LUMINANCE_FLOOR:
        return (
            "near_black_image",
            f"Local image is near-black (mean luminance {mean:.1f}/255, floor "
            f"{_NEAR_BLACK_LUMINANCE_FLOOR:.0f}) — a Flux generation likely "
            "produced a blank/failed frame.",
        )
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
