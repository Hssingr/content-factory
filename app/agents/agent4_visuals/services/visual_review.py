"""Local HTML review artifact for Agent 4 visual output."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AudioFile, Content, Script, VideoSection
from app.agents.agent4_visuals.review_metadata import (
    REVIEW_METADATA_FIELDS as _REVIEW_METADATA_FIELDS,
)
from app.services.local_run_paths import ensure_run_dirs, get_review_dir, get_run_root, get_visuals_dir
from app.agents.agent4_visuals.services.visual_bible import get_visual_bible_path, load_visual_bible_for_content

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_REMOTE_PREFIXES = ("http://", "https://")
_TEXT_CARD_SENTINEL = "__text_card__"
_DASH = "—"


def get_review_metadata_path(content_id: int | str | UUID) -> Path:
    return get_visuals_dir(content_id) / "beat_review_metadata.json"


def save_beat_review_metadata(
    content_id: int | str | UUID,
    language: str,
    beats: list[dict],
) -> None:
    """Persist this language's beat-review snapshot to the run folder.

    Merges into any existing file so each language's entry can be written
    independently (mirroring how `_save_video_sections()` persists one
    language at a time) without clobbering other already-written languages.
    """
    path = get_review_metadata_path(content_id)
    existing = _load_review_metadata_raw(path)
    existing[language] = [
        {
            "section_order": beat.get("section_order", beat.get("beat_order", i)),
            **{
                key: beat[key]
                for key in _REVIEW_METADATA_FIELDS
                if key in beat
            },
        }
        for i, beat in enumerate(beats)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _load_review_metadata_raw(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_review_metadata_by_key(content_id: int | str | UUID) -> dict[tuple[str, int], dict]:
    """Load the run-folder review metadata file, indexed by (language, section_order)."""
    raw = _load_review_metadata_raw(get_review_metadata_path(content_id))
    result: dict[tuple[str, int], dict] = {}
    for language, entries in raw.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or "section_order" not in entry:
                continue
            result[(language, entry["section_order"])] = entry
    return result


@dataclass(frozen=True)
class _MediaInfo:
    display_path: str
    preview_src: str | None
    warnings: tuple[str, ...]
    is_local_image: bool


def generate_visual_review_html(
    content_id: int | str | UUID,
    db: Session,
    *,
    language: str | None = None,
) -> Path:
    """Generate a local read-only visual review page for persisted sections.

    The database is only read. The only write is the local HTML file under
    ``media/runs/<content_id>/review/visual_review.html``.
    """
    ensure_run_dirs(content_id)
    content = db.get(Content, content_id)
    if content is None:
        raise ValueError(f"Content {content_id} not found")

    sections = _load_video_sections(content_id, db, language=language)
    scripts_by_lang = _load_language_rows(Script, content_id, db)
    audio_by_lang = _load_language_rows(AudioFile, content_id, db)
    review_metadata = _load_review_metadata_by_key(content_id)

    review_dir = get_review_dir(content_id)
    review_dir.mkdir(parents=True, exist_ok=True)
    output_path = review_dir / "visual_review.html"

    rows = [_extract_row(section, review_dir, review_metadata) for section in sections]
    html = _render_html(
        content=content,
        rows=rows,
        scripts_by_lang=scripts_by_lang,
        audio_by_lang=audio_by_lang,
        language_filter=language,
        visual_bible_summary=_visual_bible_summary(content.id, review_dir),
    )
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _load_video_sections(
    content_id: int | str | UUID,
    db: Session,
    *,
    language: str | None,
) -> list[VideoSection]:
    query = db.query(VideoSection).filter(VideoSection.content_id == content_id)
    if language:
        query = query.filter(VideoSection.language == language)
    return list(query.order_by(VideoSection.language, VideoSection.section_order).all())


def _load_language_rows(model: type, content_id: int | str | UUID, db: Session) -> dict[str, Any]:
    try:
        rows = db.query(model).filter(model.content_id == content_id).all()
    except Exception:
        return {}
    return {getattr(row, "language", ""): row for row in rows if getattr(row, "language", "")}


def _extract_row(
    section: VideoSection,
    review_dir: Path,
    review_metadata: dict[tuple[str, int], dict] | None = None,
) -> dict[str, Any]:
    extras = _parse_generation_prompt(getattr(section, "generation_prompt", None))
    # Roadmap 6.5 / audit AR-3: review-only fields (first15 diagnostics,
    # cinematic continuity tags, prompt quality warnings) now live in the
    # run-folder JSON file, not generation_prompt. `review` takes priority;
    # `extras` remains the fallback for rows persisted before this change,
    # which still carry these fields in generation_prompt.
    review = (review_metadata or {}).get((section.language, section.section_order), {})
    combined = {**extras, **review}
    media_path = _first_text(
        extras.get("media_url"),
        extras.get("image_path"),
        extras.get("media_path"),
    )
    media_info = _media_info(media_path, review_dir)

    duration_ms = max(0, int(section.audio_end_ms or 0) - int(section.audio_start_ms or 0))
    warnings = list(media_info.warnings)
    if not media_path:
        warnings.append("No media path stored")

    return {
        "order": section.section_order,
        "language": section.language,
        "time_range": f"{_format_ms(section.audio_start_ms)} - {_format_ms(section.audio_end_ms)}",
        "duration": _format_duration(duration_ms, getattr(section, "suggested_duration_sec", None)),
        "script_text": section.script_text or "",
        "flux_prompt": section.flux_prompt or _first_text(extras.get("flux_prompt"), extras.get("prompt")),
        "negative_prompt": _first_text(combined.get("negative_prompt"), combined.get("negativePrompt")),
        "media_path": media_info.display_path,
        "preview_src": media_info.preview_src,
        "warnings": tuple(warnings),
        "badges": _metadata_badges(section, combined, media_info),
        "visual_intent": _first_text(extras.get("visual_intent"), extras.get("visualIntent")),
        "shot_type": _first_text(combined.get("shot_type")),
        "subject": _first_text(combined.get("subject")),
        "action": _first_text(combined.get("action")),
        "emotion": _first_text(combined.get("emotion")),
        "camera": _first_text(combined.get("camera")),
        "lighting": _first_text(combined.get("lighting")),
        "composition": _first_text(combined.get("composition")),
        "continuity_tags": combined.get("continuity_tags") if isinstance(combined.get("continuity_tags"), list) else [],
        "visual_bible_refs": combined.get("visual_bible_refs") if isinstance(combined.get("visual_bible_refs"), list) else [],
        "is_first_15_seconds": bool(combined.get("is_first_15_seconds")),
        "first15_validation_status": _first_text(combined.get("first15_validation_status")),
        "first15_issues": combined.get("first15_issues") if isinstance(combined.get("first15_issues"), list) else [],
        "first15_strength_tags": combined.get("first15_strength_tags") if isinstance(combined.get("first15_strength_tags"), list) else [],
        "first15_enhanced": bool(combined.get("first15_enhanced")),
        "first15_validation_summary": combined.get("first15_validation_summary") if isinstance(combined.get("first15_validation_summary"), dict) else {},
        "prompt_quality_warnings": combined.get("prompt_quality_warnings") if isinstance(combined.get("prompt_quality_warnings"), list) else [],
        "extras": extras,
    }


def _parse_generation_prompt(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"generation_prompt_parse_error": True}
    return parsed if isinstance(parsed, dict) else {}


def _media_info(media_path: str, review_dir: Path) -> _MediaInfo:
    media_path = str(media_path or "").strip()
    if not media_path:
        return _MediaInfo(_DASH, None, (), False)
    if media_path == _TEXT_CARD_SENTINEL:
        return _MediaInfo(media_path, None, ("Text-card fallback; no image file to preview",), False)
    if media_path.lower().startswith(_REMOTE_PREFIXES):
        return _MediaInfo(media_path, None, ("Remote URL stored; not fetched for local review",), False)

    candidate = _resolve_local_media_path(media_path)
    is_image = candidate.suffix.lower() in _IMAGE_EXTENSIONS
    if not candidate.exists():
        return _MediaInfo(media_path, None, ("Local media file missing",), is_image)
    if not candidate.is_file():
        return _MediaInfo(media_path, None, ("Local media path is not a file",), is_image)
    if not is_image:
        return _MediaInfo(media_path, None, ("Local media is not a previewable image",), False)

    try:
        preview_src = os.path.relpath(candidate, start=review_dir)
    except ValueError:
        preview_src = str(candidate)
    return _MediaInfo(media_path, Path(preview_src).as_posix(), (), True)


def _resolve_local_media_path(media_path: str) -> Path:
    path = Path(media_path).expanduser()
    if path.is_absolute():
        return path
    return Path(settings.media_path).resolve() / path


def _metadata_badges(section: VideoSection, extras: dict[str, Any], media_info: _MediaInfo) -> list[str]:
    badges: list[str] = []
    for key in (
        "visual_category",
        "visual_type",
        "environment",
        "motif",
        "shot_type",
        "camera_angle",
        "location",
        "character",
        "transition_to_next",
        "media_type",
    ):
        value = extras.get(key)
        if value not in (None, ""):
            badges.append(f"{key}: {value}")

    strategy = getattr(section, "media_strategy", None) or extras.get("media_strategy")
    if strategy:
        badges.append(f"strategy: {strategy}")
    if getattr(section, "beat_intensity", None):
        badges.append(f"intensity: {section.beat_intensity}")
    if getattr(section, "text_card_style", None):
        badges.append(f"text card: {section.text_card_style}")
    if extras.get("source_parent_section_id") not in (None, ""):
        badges.append(f"source parent: {extras['source_parent_section_id']}")
    if extras.get("remap_score") not in (None, ""):
        badges.append(f"remap score: {extras['remap_score']}")
    if extras.get("is_reused") is True:
        badges.append("reused media")
    elif media_info.is_local_image:
        badges.append("local image")
    return badges


def _render_html(
    *,
    content: Content,
    rows: list[dict[str, Any]],
    scripts_by_lang: dict[str, Any],
    audio_by_lang: dict[str, Any],
    language_filter: str | None,
    visual_bible_summary: dict[str, str],
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    languages = sorted({str(row["language"]) for row in rows})
    preview_count = sum(1 for row in rows if row.get("preview_src"))
    warning_count = sum(1 for row in rows for warning in row["warnings"] if "missing" in warning.lower() or "remote" in warning.lower() or "unreadable" in warning.lower())
    run_root = get_run_root(content.id)
    first15_summary = _first15_summary(rows)

    summary_items = [
        ("Content ID", str(content.id)),
        ("Title", getattr(content, "title", "") or _DASH),
        ("Type", _content_type(content)),
        ("Status", getattr(content, "status", "") or _DASH),
        ("Generated UTC", generated_at),
        ("Total visual sections", str(len(rows))),
        ("Languages included", ", ".join(languages) if languages else _DASH),
        ("Language filter", language_filter or _DASH),
        ("Local image previews", str(preview_count)),
        ("Missing/remote/unreadable media warnings", str(warning_count)),
        ("First 15 Seconds", first15_summary.get("status", _DASH)),
        ("First 15 checked beats", first15_summary.get("checked", "0")),
        ("First 15 strong hooks", first15_summary.get("strong", "0")),
        ("First 15 warnings/errors", first15_summary.get("issues", "0")),
        ("Scripts loaded", str(len(scripts_by_lang))),
        ("Audio rows loaded", str(len(audio_by_lang))),
        ("Run root", str(run_root)),
        ("Visual bible", visual_bible_summary.get("path", _DASH)),
        ("Bible config source", visual_bible_summary.get("config_source", _DASH)),
        ("Bible style", visual_bible_summary.get("style", _DASH)),
        ("Bible counts", visual_bible_summary.get("counts", _DASH)),
        ("Bible summary", visual_bible_summary.get("summary", _DASH)),
    ]

    body = "\n".join(_render_row(row) for row in rows) or _empty_state()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Visual Review - {escape(str(content.id))}</title>
  <style>
    :root {{ color-scheme: light; --ink: #1d2430; --muted: #667085; --line: #d7dce3; --soft: #f5f7fa; --warn: #9a3412; --ok: #166534; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: #ffffff; }}
    header {{ padding: 24px 28px 18px; border-bottom: 1px solid var(--line); background: var(--soft); }}
    h1 {{ margin: 0 0 16px; font-size: 24px; line-height: 1.2; }}
    h2 {{ margin: 0 0 10px; font-size: 18px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }}
    .summary div {{ padding: 9px 10px; border: 1px solid var(--line); background: #fff; border-radius: 6px; }}
    .summary dt {{ font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .summary dd {{ margin: 0; font-size: 14px; overflow-wrap: anywhere; }}
    main {{ padding: 20px 28px 32px; }}
    .beat {{ display: grid; grid-template-columns: minmax(220px, 320px) minmax(0, 1fr); gap: 16px; padding: 16px 0; border-bottom: 1px solid var(--line); }}
    .media {{ min-width: 0; }}
    .preview {{ display: block; width: 100%; max-height: 260px; object-fit: contain; border: 1px solid var(--line); background: var(--soft); border-radius: 6px; }}
    .placeholder {{ min-height: 120px; display: flex; align-items: center; justify-content: center; border: 1px dashed var(--line); border-radius: 6px; color: var(--muted); background: var(--soft); }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 10px; }}
    .badge {{ font-size: 12px; padding: 3px 7px; border: 1px solid var(--line); border-radius: 999px; background: #fff; color: #344054; }}
    .warning {{ color: var(--warn); font-weight: 600; }}
    .ok {{ color: var(--ok); font-weight: 600; }}
    .field {{ margin: 9px 0; }}
    .label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 3px; }}
    .value {{ overflow-wrap: anywhere; white-space: pre-wrap; }}
    .excerpt {{ max-height: 140px; overflow: auto; }}
    @media (max-width: 760px) {{ .beat {{ grid-template-columns: 1fr; }} header, main {{ padding-left: 16px; padding-right: 16px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Visual Review</h1>
    <dl class="summary">
      {_render_summary(summary_items)}
    </dl>
  </header>
  <main>
    <h2>Visual Sections</h2>
    {body}
  </main>
</body>
</html>
"""


def _visual_bible_summary(content_id: Any, review_dir: Path) -> dict[str, str]:
    bible = load_visual_bible_for_content(content_id)
    path = get_visual_bible_path(content_id)
    try:
        rel_path = Path(os.path.relpath(path, start=review_dir)).as_posix()
    except ValueError:
        rel_path = str(path)
    if not bible:
        return {"path": rel_path}

    config = bible.get("config_context") if isinstance(bible.get("config_context"), dict) else {}
    global_style = bible.get("global_style") if isinstance(bible.get("global_style"), dict) else {}
    style_parts = [
        config.get("visual_style") or global_style.get("visual_style"),
        config.get("image_style") or global_style.get("image_style"),
        config.get("video_color_grade") or global_style.get("color_grade"),
    ]
    counts = (
        f"characters={len(bible.get('characters') or [])}, "
        f"locations={len(bible.get('locations') or [])}, "
        f"motifs={len(bible.get('recurring_motifs') or [])}"
    )
    return {
        "path": rel_path,
        "config_source": str(config.get("source") or _DASH),
        "style": " / ".join(str(part) for part in style_parts if part) or _DASH,
        "counts": counts,
        "summary": _excerpt(str(bible.get("story_visual_summary") or ""), 260) or _DASH,
    }


def _render_summary(items: list[tuple[str, str]]) -> str:
    return "\n      ".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>"
        for label, value in items
    )


def _render_row(row: dict[str, Any]) -> str:
    badges = "".join(f'<span class="badge">{escape(str(badge))}</span>' for badge in row["badges"])
    warnings = row["warnings"]
    warning_html = "".join(f'<div class="warning">{escape(str(warning))}</div>' for warning in warnings) or '<div class="ok">No local media warnings</div>'
    preview = (
        f'<img class="preview" src="{escape(row["preview_src"], quote=True)}" alt="Preview for beat {escape(str(row["order"]))}">'
        if row.get("preview_src")
        else '<div class="placeholder">No local image preview</div>'
    )
    return f"""
    <article class="beat">
      <div class="media">
        {preview}
        <div class="field"><span class="label">Media path</span><div class="value">{escape(str(row['media_path']))}</div></div>
        {warning_html}
      </div>
      <div class="details">
        <div class="meta"><span class="badge">order: {escape(str(row['order']))}</span><span class="badge">language: {escape(str(row['language']))}</span><span class="badge">time: {escape(str(row['time_range']))}</span><span class="badge">duration: {escape(str(row['duration']))}</span>{badges}</div>
        {_field('Narration/text excerpt', _excerpt(row['script_text']))}
        {_field('Visual intent', row['visual_intent'])}
        {_field('Shot type', row['shot_type'])}
        {_field('Subject', row['subject'])}
        {_field('Action', row['action'])}
        {_field('Emotion', row['emotion'])}
        {_field('Camera/lens/framing', row['camera'])}
        {_field('Lighting/color grade', row['lighting'])}
        {_field('Composition', row['composition'])}
        {_field('Continuity tags', ', '.join(str(v) for v in row['continuity_tags']) or _DASH)}
        {_field('Visual bible references', ', '.join(str(v) for v in row['visual_bible_refs']) or _DASH)}
        {_field('First 15 seconds', 'yes' if row['is_first_15_seconds'] else 'no')}
        {_field('First 15 status', row['first15_validation_status'] or _DASH)}
        {_field('First 15 strength tags', ', '.join(str(v) for v in row['first15_strength_tags']) or _DASH)}
        {_field('First 15 issues', _format_first15_issues(row['first15_issues']))}
        {_field('First 15 enhanced', 'yes' if row['first15_enhanced'] else 'no')}
        {_field('Prompt quality warnings', _format_prompt_warnings(row['prompt_quality_warnings']))}
        {_field('Flux/image prompt', row['flux_prompt'])}
        {_field('Negative prompt', row['negative_prompt'])}
      </div>
    </article>"""


def _first15_summary(rows: list[dict[str, Any]]) -> dict[str, str]:
    first_rows = [row for row in rows if row.get("is_first_15_seconds")]
    summaries = [row.get("first15_validation_summary") for row in first_rows if isinstance(row.get("first15_validation_summary"), dict)]
    summary = summaries[0] if summaries else {}
    issues = summary.get("issues") if isinstance(summary.get("issues"), list) else []
    status = str(summary.get("status") or (first_rows[0].get("first15_validation_status") if first_rows else _DASH) or _DASH)
    return {
        "status": status,
        "checked": str(summary.get("checked_count") if summary.get("checked_count") is not None else len(first_rows)),
        "strong": str(summary.get("strong_hook_count") if summary.get("strong_hook_count") is not None else sum(1 for row in first_rows if row.get("first15_strength_tags"))),
        "issues": str(len(issues) if issues else sum(len(row.get("first15_issues") or []) for row in first_rows)),
    }


def _format_first15_issues(issues: list) -> str:
    if not issues:
        return _DASH
    parts: list[str] = []
    for issue in issues:
        if isinstance(issue, dict):
            code = issue.get("code", "issue")
            severity = issue.get("severity", "")
            message = issue.get("message", "")
            prefix = f"{severity} {code}".strip()
            parts.append(f"{prefix}: {message}" if message else prefix)
        else:
            parts.append(str(issue))
    return "\n".join(parts)


def _format_prompt_warnings(warnings: list) -> str:
    if not warnings:
        return _DASH
    parts: list[str] = []
    for warning in warnings:
        if isinstance(warning, dict):
            code = warning.get("code", "warning")
            message = warning.get("message", "")
            parts.append(f"{code}: {message}" if message else str(code))
        else:
            parts.append(str(warning))
    return "\n".join(parts)


def _field(label: str, value: Any) -> str:
    text = str(value or _DASH)
    return f'<div class="field"><span class="label">{escape(label)}</span><div class="value excerpt">{escape(text)}</div></div>'


def _empty_state() -> str:
    return '<div class="placeholder">No VideoSection rows found for this content.</div>'


def _excerpt(value: str, limit: int = 420) -> str:
    value = " ".join(str(value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _format_ms(value: int | None) -> str:
    total_ms = max(0, int(value or 0))
    total_seconds, ms = divmod(total_ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


def _format_duration(duration_ms: int, suggested_sec: float | None) -> str:
    actual = f"{duration_ms / 1000:.2f}s"
    if suggested_sec is None:
        return actual
    return f"{actual} suggested {float(suggested_sec):.2f}s"


def _content_type(content: Content) -> str:
    if bool(getattr(content, "is_short_episode", False)):
        part = getattr(content, "short_part_number", None)
        total = getattr(content, "short_total_parts", None)
        suffix = f" part {part}/{total}" if part and total else ""
        return f"child short{suffix}"
    return "parent long-form"


def _first_text(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""
