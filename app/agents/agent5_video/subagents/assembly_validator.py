"""Assembly Validator — checks overall video assembly quality at a macro level.

Runs ONCE after media validation. Sends high-level statistics (distribution counts,
duration drift, stddev) instead of a full beat list so token usage is bounded
regardless of video length.

This validator is purely advisory: it logs issues but never blocks the pipeline.
It may return a set of beat list-indices whose media should be re-fetched if a
CONCRETE technical issue is detected (currently always empty — the validator
identifies macro-level structural patterns, not individual beat failures).

Per-beat media replacement is handled exclusively by the Media Validation Agent.
"""

import logging
from collections import Counter

from app.agents.agent5_video.system_prompt import validate_assembly_with_claude

logger = logging.getLogger(__name__)


def validate_assembly(
    sections: list[dict],
    total_duration_ms: int,
    channel_niche: str,
    channel_tone: str,
    channel_style: str = "documentary",
) -> tuple[list[dict], set[int]]:
    """Validate overall video assembly quality from high-level statistics.

    Sends a structured summary (distribution counts, duration drift/stddev, overlay
    percentage) to Claude rather than the full beat list — this prevents truncation
    on long storyboard videos (60+ beats) and keeps each call under 1024 output tokens.

    Assembly issues are logged as warnings and never block the pipeline on their own.
    The second return value is a set of beat list-indices marked for incremental
    media re-validation; currently always empty (assembly identifies macro issues,
    not individual beat failures). The interface exists so the orchestrator can
    optionally route specific beats back through media validation in future.

    Args:
        sections:          Fully enriched sections (after stock_fetcher + media validation).
        total_duration_ms: Expected audio duration from audio_files table.
        channel_niche:     Channel niche for context.
        channel_tone:      Channel tone for context.
        channel_style:     Video style type from channel_config.

    Returns:
        ``(sections, dirty_beat_indices)`` — sections unchanged; dirty_beat_indices
        is currently always an empty set.
    """
    try:
        review = validate_assembly_with_claude(
            sections, total_duration_ms, channel_niche, channel_tone, channel_style,
        )
    except Exception as exc:
        logger.error("Assembly Validator Claude call failed: %s — skipping", exc)
        return sections, set()

    assembly_status = review.get("assembly_status", "APPROVED")
    assembly_issues = review.get("assembly_issues", [])
    overall_comment = review.get("overall_comment", "")

    logger.info("Assembly Validator: %s — %s", assembly_status, overall_comment)

    for issue in assembly_issues:
        logger.warning(
            "Assembly issue [%s/%s]: %s — suggestion: %s",
            issue.get("severity", "?"),
            issue.get("category", "?"),
            issue.get("issue", ""),
            issue.get("suggestion", ""),
        )

    severity_dist = Counter(i.get("severity", "?") for i in assembly_issues)
    category_dist = Counter(i.get("category", "?") for i in assembly_issues)
    logger.info(
        "Assembly validation complete: status=%s issues=%d "
        "HIGH=%d MEDIUM=%d categories=%s",
        assembly_status, len(assembly_issues),
        severity_dist.get("HIGH", 0), severity_dist.get("MEDIUM", 0),
        dict(category_dist),
    )

    # Advisory only — no beats are currently marked for re-validation by assembly
    return sections, set()
