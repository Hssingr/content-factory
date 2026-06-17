import json
import logging
import re
from datetime import datetime, timezone

from app.services.claude_client import call_claude, call_claude_with_tools
from app.agents.agent2_discovery.services.story import Story

logger = logging.getLogger(__name__)

_WEB_SEARCH_TOOL: dict = {"type": "web_search_20250305", "name": "web_search"}

_SINGLE_STORY_SYSTEM_PROMPT = """\
You are a content discovery agent for an automated multilingual video channel system.

Your task: browse the provided sources and find the SINGLE most engaged, highest-signal story
for the channel's niche — not just the most recent, but the one with the most genuine audience
response: highest comments, reactions, upvotes, or shares when visible.

Discovery criteria:
1. Engagement — prefer stories with the most comments, reactions, or shares over recency
2. Relevance  — the story must clearly and strongly match the channel niche
3. Substance  — the story must have enough depth for a 6–12 minute video script

Rules:
- Use web_search to browse all provided sources before deciding
- Compare multiple stories across sources before picking the highest-engagement one
- Skip: promotional content, ads, stickied posts, meta announcements
- Never invent facts, URLs, titles, or statistics — only include what you actually found

Return ONLY a valid JSON object. No markdown. No code fence.
Start immediately with { and end with }.

Required format:
{"title":"...","body":"200-500 word factual summary","url":"...","language":"en","published_at":"ISO8601 or null","upvotes":0,"comments":0}\
"""

_SINGLE_STORY_REFORMAT_PROMPT = """\
Extract the story information from the text below and convert it into a single JSON object.
Return ONLY valid JSON. No markdown. No code fence. Start with {.
Required keys: title, body, url, language, published_at (ISO8601 or null), upvotes, comments.
Never invent facts, URLs, or details not present in the input.\
"""


def fetch_batch(
    sources: list[tuple[str, str, float]],
    niche: str,
    count: int = 1,  # kept for call-site compatibility; always fetches exactly 1 story
) -> list[Story]:
    """Browse sources and return the single highest-engagement story in one Claude call.

    Runs one ``call_claude_with_tools`` pass (story_research / Sonnet + web_search).
    Falls back to a reformat pass if Claude returns prose, then to an empty list on failure.

    Args:
        sources: List of ``(source_value, source_type, trust_score)`` tuples.
        niche:   Channel niche description.
        count:   Ignored — always returns at most 1 story.

    Returns:
        List containing 0 or 1 Story objects.
    """
    if not sources:
        logger.warning("fetch_batch() called with empty sources list")
        return []

    source_lines = "\n".join(
        f"  - [{stype}] {svalue}  (trust={trust:.1f})"
        for svalue, stype, trust in sources
    )
    user_message = (
        f"Channel niche: {niche}\n\n"
        f"Sources to explore:\n{source_lines}\n\n"
        "Browse the sources, find the highest-engagement story, then output ONLY the JSON object."
    )

    try:
        raw = call_claude_with_tools(
            _SINGLE_STORY_SYSTEM_PROMPT,
            user_message,
            tools=[_WEB_SEARCH_TOOL],
            max_tokens=4096,
            max_rounds=20,
            task="story_research",
        )
    except Exception as exc:
        logger.error("Story fetch web search failed: %s", exc)
        return []

    # Pass 1: try to parse directly
    story = _parse_story(raw)
    if story:
        logger.info("fetch_batch: parsed story directly (title=%r)", story.title[:80])
        return [story]

    # Pass 2: Claude gave prose — reformat to JSON object
    logger.info("Response was not JSON — sending reformat pass...")
    try:
        reformatted = call_claude(
            _SINGLE_STORY_REFORMAT_PROMPT,
            f"Story information:\n{raw[:6000]}",
            max_tokens=2048,
            task="content_reformat",
        )
        story = _parse_story(reformatted)
        if story:
            logger.info("Reformat pass succeeded (title=%r)", story.title[:80])
            return [story]
    except Exception as exc:
        logger.error("Reformat pass failed: %s", exc)

    logger.error("Could not extract a story from fetch response")
    return []


def _safe_int(value) -> int:
    """Convert a Claude-returned value to int without crashing on bad input."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _story_from_dict(data: dict) -> Story | None:
    """Build a Story from a parsed dict. Returns None if required fields are missing."""
    url   = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()
    if not url or not title:
        logger.debug("Fetcher entry missing url or title — skipping")
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


def _parse_story(text: str) -> Story | None:
    """Parse Claude's single-object JSON response into a Story."""
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", text).strip()

    if not cleaned.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            cleaned = match.group(0)

    try:
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

    story = _story_from_dict(data)
    if story is None:
        logger.error("Fetcher response missing url or title: %.200s", text)
    return story
