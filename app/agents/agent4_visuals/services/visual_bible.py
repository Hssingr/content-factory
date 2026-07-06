"""Read-only access to legacy visual-bible artifacts.

The visual-bible generation layer (Claude call, validation, config-context
building, parent/child copy logic) was deleted entirely per the Elimination
Mandate (code_report/forensic_output_audit_borrasca_run.md, D2.1): a real
production run showed it generated 0 locations, 0 motifs, and 0 first-15
rules — a total no-op that still paid for a Claude call and injected an
empty compact-JSON block into every storyboard batch. It is replaced by a
deterministic continuity line built from the blueprint — see
``storyboard.build_continuity_line_from_blueprint()``.

``get_visual_bible_path()``/``load_visual_bible_for_content()`` remain as
read-only helpers so ``visual_review.py`` (local-debug only) can still
display a bible file left on disk by a run from before this change. No new
bible file is ever written by current code.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from app.services.local_run_paths import get_visuals_dir


def get_visual_bible_path(content_id: int | str | UUID) -> Path:
    return get_visuals_dir(content_id) / "visual_bible.json"


def load_visual_bible_for_content(content_id: int | str | UUID) -> dict | None:
    path = get_visual_bible_path(content_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
