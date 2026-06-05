"""Remotion renderer — calls the Remotion CLI via subprocess to render video files.

Each render (main + shorts) is saved to:
  {media_path}/video/{content_id}/{language}_main.mp4
  {media_path}/video/{content_id}/{language}_short_{n}.mp4

After a successful render the function returns a dict with file_path, duration_seconds,
hook_modified, and render_time_seconds for the caller (orchestrator) to persist to DB.

Remotion is invoked with:
  npx remotion render <composition> <output_path> --props <props_json>

The function does NOT commit to the database — the orchestrator does that.
"""

import logging
import subprocess
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# Remotion composition IDs (must match the Remotion project's composition names)
_COMP_MAIN  = "MainVideo"
_COMP_SHORT = "Short"


def render_main_video(
    content_id: str,
    language: str,
    props_path: str,
    duration_ms: int,
) -> dict:
    """Render the main 16:9 video using Remotion.

    Args:
        content_id:  UUID of the content record.
        language:    Language code (e.g. "fr").
        props_path:  Absolute path to the main props JSON file.
        duration_ms: Expected video duration (used to record in DB — not passed to Remotion).

    Returns:
        Dict with file_path, duration_seconds, hook_modified (False), render_time_seconds.

    Raises:
        RuntimeError: If the Remotion CLI exits with a non-zero code.
    """
    output_path = _ensure_output_path(content_id, f"{language}_main.mp4")
    render_time = _run_remotion(_COMP_MAIN, output_path, props_path)

    logger.info("Main video rendered: %s (%.1fs)", output_path, render_time)
    return {
        "file_path":          str(output_path),
        "duration_seconds":   duration_ms / 1000,
        "hook_modified":      False,
        "render_time_seconds": render_time,
    }


def render_short(
    content_id: str,
    language: str,
    short_index: int,
    props_path: str,
    duration_ms: int,
    hook_modified: bool = True,
) -> dict:
    """Render a single Short (9:16) using Remotion.

    Args:
        content_id:    UUID of the content record.
        language:      Language code.
        short_index:   0-based index of the Short.
        props_path:    Absolute path to this Short's props JSON file.
        duration_ms:   Duration of this Short in ms.
        hook_modified: Whether the hook was modified (always True for Shorts).

    Returns:
        Dict with file_path, duration_seconds, hook_modified, render_time_seconds.

    Raises:
        RuntimeError: If the Remotion CLI exits with a non-zero code.
    """
    file_name   = f"{language}_short_{short_index}.mp4"
    output_path = _ensure_output_path(content_id, file_name)
    render_time = _run_remotion(_COMP_SHORT, output_path, props_path)

    logger.info("Short %d rendered: %s (%.1fs)", short_index, output_path, render_time)
    return {
        "file_path":           str(output_path),
        "duration_seconds":    duration_ms / 1000,
        "hook_modified":       hook_modified,
        "render_time_seconds": render_time,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ensure_output_path(content_id: str, file_name: str) -> Path:
    output_dir = Path(settings.media_path) / "video" / content_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / file_name


def _run_remotion(composition: str, output_path: Path, props_path: str) -> float:
    """Invoke the Remotion CLI and return wall-clock render time in seconds.

    Args:
        composition: Remotion composition ID (e.g. "MainVideo").
        output_path: Destination MP4 path.
        props_path:  Absolute path to the props JSON file.

    Returns:
        Render time in seconds.

    Raises:
        RuntimeError: If the subprocess exits with a non-zero return code.
    """
    cmd = [
        "npx", "remotion", "render",
        composition,
        str(output_path),
        "--props", props_path,
        "--concurrency", "2",
        "--log", "verbose",
    ]

    logger.info("Remotion render: %s → %s", composition, output_path)
    t0 = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            cwd=settings.remotion_path,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Remotion CLI not found — is Node.js installed and `npx` in PATH? ({exc})"
        ) from exc

    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        logger.error("Remotion stderr:\n%s", result.stderr[-3000:])
        raise RuntimeError(
            f"Remotion render failed (exit {result.returncode}) for {composition}: "
            f"{result.stderr[-500:]}"
        )

    return elapsed
