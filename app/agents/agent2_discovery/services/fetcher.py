import json
import logging
import re
from datetime import datetime, timezone

from app.services.claude_client import call_claude, call_claude_with_tools
from app.agents.agent2_discovery.services.story import Story

logger = logging.getLogger(__name__)

_WEB_SEARCH_TOOL: dict = {"type": "web_search_20250305", "name": "web_search"}

_SYSTEM_PROMPT = """\
You are a content discovery agent for an automated multilingual video channel system.

Your task: browse the provided sources and find the SINGLE most relevant and engaging
story for the channel's niche.

Discovery criteria:
1. Relevance   — the story must clearly and strongly match the channel niche
2. Engagement  — prefer stories with high upvotes, comments, or shares when visible
3. Substance   — the story must have enough depth for a 6–12 minute video script

Rules:
- Use the web_search tool to browse the sources provided by the user
- Skip: promotional content, ads, stickied posts, meta announcements, duplicate stories
- Check multiple sources if needed before deciding on the best story

CRITICAL — Return ONLY valid JSON. No markdown. No code fence. No extra keys.
Start immediately with { and end with }. Never invent facts, URLs, titles, or statistics.
Only include information you actually found by browsing.

Required format:
{"title":"...","body":"200-500 word summary","url":"...","language":"en","published_at":"ISO8601 or null","upvotes":0,"comments":0}\
"""

_REFORMAT_SYSTEM_PROMPT = """\
Convert the story information below into a single JSON object.
Return ONLY valid JSON. No markdown. No code fence. No extra keys. Start with {.
Required keys: title, body, url, language, published_at (ISO8601 or null), upvotes, comments.
Never invent facts, URLs, or details not present in the input.\
"""


def fetch(
    sources: list[tuple[str, str, float]],
    niche: str,
    exclude: list[dict] | None = None,
) -> Story | None:
    """Ask Claude to autonomously browse all sources and return the best story.

    Uses a two-pass approach:
    1. Claude browses the web with web_search_20250305 and identifies the best story.
    2. If the response is not valid JSON (Claude explained in prose), a second
       single-turn call reformats it into the required JSON structure.

    Args:
        sources: List of (source_value, source_type, trust_score) tuples.
        niche:   Channel niche description.
        exclude: Optional list of ``{"title", "url"}`` dicts for stories already
                 rejected this run (by the Story Scoring Gate) — Claude is told
                 not to propose any of them again, so retries surface a genuinely
                 different candidate instead of looping on the same story.

    Returns:
        The best Story found, or None on error.
    """
    if not sources:
        logger.warning("fetch() called with empty sources list")
        return None

    source_lines = "\n".join(
        f"  - [{stype}] {svalue}  (trust={trust:.1f})"
        for svalue, stype, trust in sources
    )
    exclude_block = ""
    if exclude:
        exclude_lines = "\n".join(
            f"  - {c.get('title', '')!r} ({c.get('url', '')})" for c in exclude
        )
        exclude_block = (
            "\n\nDo NOT propose any of these stories again — they were already "
            f"rejected this run, find a genuinely different one:\n{exclude_lines}"
        )
    user_message = (
        f"Channel niche: {niche}\n\n"
        f"Sources to explore:\n{source_lines}"
        f"{exclude_block}\n\n"
        "Browse the sources, find the best story, then output ONLY the JSON object."
    )

    try:
        raw = call_claude_with_tools(
            _SYSTEM_PROMPT,
            user_message,
            tools=[_WEB_SEARCH_TOOL],
            max_tokens=2048,
            max_rounds=15,
        )
    except Exception as exc:
        logger.error("Claude web search failed: %s", exc)
        return None

    # Pass 1: try to parse directly
    story = _parse_story(raw)
    if story:
        return story

    # Pass 2: Claude gave prose — extract the JSON by asking Claude to reformat
    logger.info("Response was not JSON — sending reformat pass...")
    try:
        reformatted = call_claude(
            _REFORMAT_SYSTEM_PROMPT,
            f"Story information:\n{raw[:3000]}",
            max_tokens=2048,   # body can be 200-500 words; 512 was too small
        )
        story = _parse_story(reformatted)
        if story:
            logger.info("Reformat pass succeeded")
            return story
    except Exception as exc:
        logger.error("Reformat pass failed: %s", exc)

    logger.error("Could not extract a valid story from Claude's response")
    return None


def _safe_int(value) -> int:
    """Convert a Claude-returned value to int without crashing on bad input."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_story(text: str) -> Story | None:
    """Parse Claude's JSON response into a Story. Returns None on parse failure."""
    # Strip code fences if present
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", text).strip()

    # Try to extract a JSON object if there's prose around it
    if not cleaned.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            cleaned = match.group(0)

    try:
        # Use raw_decode so we stop at the end of the first valid JSON object
        # and silently ignore any trailing text Claude added after the closing }.
        decoder = json.JSONDecoder()
        data, end_idx = decoder.raw_decode(cleaned)
        if end_idx < len(cleaned.rstrip()):
            logger.debug(
                "Ignored extra content after JSON (first 60 chars): %.60s",
                cleaned[end_idx:].strip(),
            )
    except json.JSONDecodeError as exc:
        logger.error("Fetcher JSON parse error: %s | Raw (first 300): %.300s", exc, text)
        return None

    url   = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()
    if not url or not title:
        logger.error("Fetcher response missing url or title: %.200s", text)
        return None

    language = (data.get("language") or "en").strip()

    published_at: datetime | None = None
    if raw_date := data.get("published_at"):
        try:
            published_at = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass
    if published_at is None:
        published_at = datetime.now(timezone.utc)

    return Story(
        url=url,
        title=title,
        body=(data.get("body") or "").strip(),
        language=language,
        source_type="web",
        source_value="claude_web_search",
        published_at=published_at,
        upvotes=_safe_int(data.get("upvotes")),
        comments=_safe_int(data.get("comments")),
    )
