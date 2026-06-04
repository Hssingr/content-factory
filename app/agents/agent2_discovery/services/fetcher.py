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

CRITICAL — your FINAL response must be ONLY a raw JSON object with NO explanation,
NO reasoning, NO preamble, NO code fence. Start immediately with { and end with }.

Required format:
{"title":"...","body":"200-500 word summary","url":"...","language":"en","published_at":"ISO8601 or null","upvotes":0,"comments":0}\
"""

_REFORMAT_SYSTEM_PROMPT = """\
Convert the story information below into a single JSON object.
Respond with ONLY the JSON — no explanation, no code fence, start with {.
Required keys: title, body, url, language, published_at (ISO8601 or null), upvotes, comments.\
"""


def fetch(
    sources: list[tuple[str, str, float]],
    niche: str,
) -> Story | None:
    """Ask Claude to autonomously browse all sources and return the best story.

    Uses a two-pass approach:
    1. Claude browses the web with web_search_20250305 and identifies the best story.
    2. If the response is not valid JSON (Claude explained in prose), a second
       single-turn call reformats it into the required JSON structure.

    Args:
        sources: List of (source_value, source_type, trust_score) tuples.
        niche:   Channel niche description.

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
    user_message = (
        f"Channel niche: {niche}\n\n"
        f"Sources to explore:\n{source_lines}\n\n"
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
            max_tokens=512,
        )
        story = _parse_story(reformatted)
        if story:
            logger.info("Reformat pass succeeded")
            return story
    except Exception as exc:
        logger.error("Reformat pass failed: %s", exc)

    logger.error("Could not extract a valid story from Claude's response")
    return None


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
        data = json.loads(cleaned)
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
        upvotes=int(data.get("upvotes") or 0),
        comments=int(data.get("comments") or 0),
    )
