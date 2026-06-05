"""Assembly Validator — validates fetched media relevance and overall video assembly.

Runs ONCE after the stock fetcher. For REPLACE sections the validator provides a
new search_query and the orchestrator re-fetches using stock_fetcher.

Two dimensions checked by Claude:
  1. Media relevance: does the fetched image/video match the section's narrative intent?
  2. Assembly quality: flow, pacing, duration drift, visual coherence across sections.

Python handles:
  - Duration drift computation (passed to Claude as context).
  - Re-fetch of REPLACE sections via stock_fetcher.
  - No validation loop (runs once — best effort).
"""

import logging

from app.agents.agent5_video.services.stock_fetcher import fetch_for_section
from app.agents.agent5_video.system_prompt import validate_assembly_with_claude

logger = logging.getLogger(__name__)


def validate_assembly(
    sections: list[dict],
    total_duration_ms: int,
    channel_niche: str,
    channel_tone: str,
    channel_style: str = "documentary",
) -> list[dict]:
    """Validate the full assembled video plan and re-fetch any REPLACE sections.

    Steps:
      1. Ask Claude to review media relevance + assembly quality.
      2. For each section marked REPLACE: update search_query and re-fetch media.
      3. Log any assembly-level issues (pacing, flow, drift) — no auto-fix.

    Args:
        sections:          Fully enriched sections (after stock_fetcher).
        total_duration_ms: Expected audio duration from audio_files table.
        channel_niche:     Channel niche for context.
        channel_tone:      Channel tone for context.
        channel_style:     Video style type from channel_config.

    Returns:
        Sections list with any REPLACE sections re-fetched. Assembly issues are
        logged but do not block the pipeline.
    """
    try:
        review = validate_assembly_with_claude(
            sections, total_duration_ms, channel_niche, channel_tone, channel_style,
        )
    except Exception as exc:
        logger.error("Assembly Validator Claude call failed: %s — skipping", exc)
        return sections

    assembly_status = review.get("assembly_status", "APPROVED")
    section_reviews = review.get("section_reviews", [])
    assembly_issues = review.get("assembly_issues", [])
    overall_comment = review.get("overall_comment", "")

    logger.info("Assembly Validator: %s — %s", assembly_status, overall_comment)

    # Log assembly-level issues (pacing, flow, drift)
    for issue in assembly_issues:
        logger.warning(
            "Assembly issue (section %s): %s",
            issue.get("section_order", "?"),
            issue.get("issue", ""),
        )

    if assembly_status == "APPROVED":
        return sections

    # Build lookup from review list
    reviews_by_order = {r["section_order"]: r for r in section_reviews if "section_order" in r}

    replaced = 0
    for s in sections:
        order = s["section_order"]
        review_entry = reviews_by_order.get(order)
        if not review_entry:
            continue
        if review_entry.get("action") != "REPLACE":
            continue

        new_query = review_entry.get("new_search_query", "").strip()
        if not new_query:
            logger.warning("Section %d REPLACE but no new_search_query provided — skipping", order)
            continue

        old_url = s.get("media_url", "")
        s["search_query"] = new_query
        fetch_for_section(s)
        replaced += 1
        logger.info(
            "Section %d: replaced media — query=%r  old=%s  new=%s",
            order, new_query, old_url[:60], s.get("media_url", "")[:60],
        )

    logger.info(
        "Assembly validation complete: status=%s replaced=%d assembly_issues=%d",
        assembly_status, replaced, len(assembly_issues),
    )
    return sections
