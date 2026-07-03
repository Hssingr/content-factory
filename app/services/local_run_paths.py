"""Local run-folder paths for development/debug artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import settings

RUN_SUBDIRS: tuple[str, ...] = (
    "script",
    "audio",
    "whisper",
    "visuals",
    "review",
    "renders",
)


def get_run_root(content_id: int | str | UUID) -> Path:
    """Return ``<media_path>/runs/<content_id>`` as an absolute path."""
    normalized_id = _normalize_content_id(content_id)
    return _media_root() / "runs" / normalized_id


def ensure_run_dirs(content_id: int | str | UUID) -> dict[str, Path]:
    """Create and return the local run directories for a content item.

    The helper is idempotent: it creates missing directories and manifest keys
    but does not delete artifacts or replace an existing manifest timestamp.
    """
    run_root = get_run_root(content_id)
    run_root.mkdir(parents=True, exist_ok=True)

    directories = {name: run_root / name for name in RUN_SUBDIRS}
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)

    _ensure_manifest(content_id, run_root, directories)
    return directories


def get_script_dir(content_id: int | str | UUID) -> Path:
    return get_run_root(content_id) / "script"


def get_audio_dir(content_id: int | str | UUID) -> Path:
    return get_run_root(content_id) / "audio"


def get_whisper_dir(content_id: int | str | UUID) -> Path:
    return get_run_root(content_id) / "whisper"


def get_visuals_dir(content_id: int | str | UUID) -> Path:
    return get_run_root(content_id) / "visuals"


def get_review_dir(content_id: int | str | UUID) -> Path:
    return get_run_root(content_id) / "review"


def get_renders_dir(content_id: int | str | UUID) -> Path:
    return get_run_root(content_id) / "renders"


def _media_root() -> Path:
    raw_media_path = getattr(settings, "media_path", None)
    if raw_media_path is None or str(raw_media_path).strip() == "":
        raise ValueError("Configured media_path is missing or empty")

    try:
        return Path(str(raw_media_path)).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Configured media_path is invalid: {raw_media_path!r}") from exc


def _normalize_content_id(content_id: int | str | UUID) -> str:
    if content_id is None:
        raise ValueError("content_id is required")

    normalized = str(content_id).strip()
    if not normalized:
        raise ValueError("content_id is required")
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError(f"content_id is not safe for a local run path: {content_id!r}")
    return normalized


def _ensure_manifest(
    content_id: int | str | UUID,
    run_root: Path,
    directories: dict[str, Path],
) -> None:
    manifest_path = run_root / "run_manifest.json"
    existing = _read_manifest(manifest_path)

    created_at = existing.get("created_at") or datetime.now(timezone.utc).isoformat()
    manifest = {
        **existing,
        "content_id": str(content_id),
        "created_at": created_at,
        "run_root": str(run_root),
        "directories": {
            **{
                key: value
                for key, value in existing.get("directories", {}).items()
                if key in RUN_SUBDIRS
            },
            **{key: str(path) for key, path in directories.items()},
        },
    }

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
