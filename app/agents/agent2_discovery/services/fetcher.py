import json
import logging
import re
from datetime import datetime, timezone

from app.services.claude_client import call_claude, call_claude_with_tools
from app.agents.agent2_discovery.services.story import MAX_SOURCE_EXCERPT_CHARS, Story

logger = logging.getLogger(__name__)

_WEB_SEARCH_TOOL: dict = {"type": "web_search_20250305", "name": "web_search"}

# Roadmap 4.1 / audit S-1 (exec-2): raised from 4096 so Claude has headroom to
# return the full verbatim post + top comments instead of a short summary.
# 8192 is this codebase's own established, proven-working output-token
# ceiling (matches STORYBOARD_BATCH_MAX_TOKENS in agent4's system_prompt.py)
# — kept here rather than a larger unverified value since a live API call to
# confirm a higher ceiling is not permitted.
_STORY_FETCH_MAX_TOKENS = 8192
_STORY_REFORMAT_MAX_TOKENS = 8192

# Roadmap 6.2 / audit C-4: capped from 20 — since roadmap 4.1, this call only
# has to FIND and relay a story via web_search, not additionally condense it
# into a summary, so it no longer needs 20 tool rounds to converge.
_STORY_FETCH_MAX_ROUNDS = 8

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

Source material — this is the single most important rule in this prompt:
- "body" must be the FULL text you actually retrieved via web_search, not a summary.
- Reproduce the original post's text as completely and verbatim as possible, followed by
  the top comments (verbatim, most upvoted/most relevant first) if the source page shows
  comments. Preserve concrete names, dates, numbers, and quotes exactly as written.
- Do NOT condense, paraphrase, or write "a factual summary" — every fact downstream is
  generated from this field alone, so compressing it starves the rest of the pipeline.
- If the real post/comments are long, include as much of the real text as you can up to
  your own output limit — never pad or invent text to reach any target length.

Return ONLY a valid JSON object. No markdown. No code fence.
Start immediately with { and end with }.

Required format:
{"title":"...","body":"full verbatim post text + top comments, not a summary","url":"...","language":"en","published_at":"ISO8601 or null","upvotes":0,"comments":0}\
"""

_SINGLE_STORY_REFORMAT_PROMPT = """\
Extract the story information from the text below and convert it into a single JSON object.
Return ONLY valid JSON. No markdown. No code fence. Start with {.
Required keys: title, body, url, language, published_at (ISO8601 or null), upvotes, comments.
"body" must be the full verbatim source text already present in the input below — reproduce
it completely, do not summarize or shorten it.
Never invent facts, URLs, or details not present in the input.\
"""

# How many existing channel stories to include in the nuclear-retry exclusion list.
# Keeps the user message within token budget while covering the full history.
_MAX_NUCLEAR_EXCLUSION = 80


def fetch_batch(
    sources: list[tuple[str, str, float]],
    niche: str,
    rejected_stories: list[dict] | None = None,
) -> list[Story]:
    """Browse sources and return the single highest-engagement story in one Claude call.

    Runs one ``call_claude_with_tools`` pass (story_research / Sonnet + web_search).
    Falls back to a reformat pass if Claude returns prose, then to an empty list on failure.

    Args:
        sources:          List of ``(source_value, source_type, trust_score)`` tuples.
        niche:            Channel niche description.
        rejected_stories: Optional list of ``{"title": str, "url": str}`` dicts that
                          Claude must not return again. Injected as a hard exclusion block
                          at the end of the user message.

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

    if rejected_stories:
        rejected_block = "\n".join(
            f"  {i + 1}. Title: {r['title']}\n     URL: {r['url']}"
            for i, r in enumerate(rejected_stories)
        )
        user_message += (
            f"\n\nDo NOT return any of these stories (already used or seen):\n"
            f"{rejected_block}\n\n"
            "Find a completely different story that is not in the list above."
        )
        logger.info(
            "fetch_batch: %d story/stories in exclusion list",
            len(rejected_stories),
        )

    try:
        raw = call_claude_with_tools(
            _SINGLE_STORY_SYSTEM_PROMPT,
            user_message,
            tools=[_WEB_SEARCH_TOOL],
            max_tokens=_STORY_FETCH_MAX_TOKENS,
            max_rounds=_STORY_FETCH_MAX_ROUNDS,
            task="story_research",
        )
    except Exception as exc:
        logger.error("Story fetch web search failed: %s", exc)
        return []

    # Pass 1: try to parse directly
    story = _parse_story(raw)
    if story:
        logger.info(
            "fetch_batch: parsed story directly (title=%r, body_chars=%d)",
            story.title[:80], len(story.body),
        )
        return [story]

    # Pass 2: Claude gave prose — reformat to JSON object. Truncate the raw
    # input to MAX_SOURCE_EXCERPT_CHARS (not the old 6000-char cap) so the
    # full verbatim body Claude already wrote isn't lost before reformatting.
    logger.info("Response was not JSON — sending reformat pass...")
    try:
        reformatted = call_claude(
            _SINGLE_STORY_REFORMAT_PROMPT,
            f"Story information:\n{raw[:MAX_SOURCE_EXCERPT_CHARS]}",
            max_tokens=_STORY_REFORMAT_MAX_TOKENS,
            task="content_reformat",
        )
        story = _parse_story(reformatted)
        if story:
            logger.info(
                "Reformat pass succeeded (title=%r, body_chars=%d)",
                story.title[:80], len(story.body),
            )
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
        body=(data.get("body") or "").strip()[:MAX_SOURCE_EXCERPT_CHARS],
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