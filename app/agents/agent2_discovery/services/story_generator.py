"""AI-generated story discovery (``ChannelConfig.script_source="ai_generated"``).

Two-stage premise/expansion design — see
``code_report/ai_generated_story_discovery_design.md`` for the full design
record and CLAUDE.md §9.5 for the runtime architecture. Kept in a file
separate from ``fetcher.py`` specifically so "costs web_search money"
(fetcher.py) vs. "cheap synthesis, no web_search" (this file) stays
architecturally auditable at a glance.

Stage 1 (discovery time, cheap): ``generate_story_premise()`` — a short,
concrete premise/pitch grounded only in the channel's existing
niche/tone/description. Goes through the *existing* dedup/scoring/Telegram-
approval machinery unchanged, exactly like a Reddit-discovered candidate.

Stage 2 (on APPROVE — the only point real cost is spent):
``expand_story_premise()`` expands the human-approved premise into a full
story body. The caller (``script_workflow._ensure_ai_story_expanded()``)
gates this on the *existing* source-material floor check, reusing the
pipeline's current fail-loud behavior rather than inventing a new one.

Per the Elimination Mandate (CLAUDE.md §9.3/§23), neither function re-rolls
a result it judges too short/weak — a transport failure returns an empty/
``None`` result; a too-short-but-successful generation is left for the
existing deterministic floor check downstream to catch.
"""

import logging
import uuid as _uuid_module

from app.agents.agent2_discovery.services.story import Story
from app.models import Channel
from app.services.claude_client import call_claude_structured

logger = logging.getLogger(__name__)

# A premise is meant to be a short, Telegram-readable pitch (3-6 sentences).
# Not hard-enforced (Elimination Mandate — no quality-judging retry here),
# but a result over this ceiling is logged so an ignored length instruction
# is visible instead of silently accepted.
_PREMISE_SOFT_WORD_CEILING = 150

_PREMISE_MAX_TOKENS = 512
_EXPANSION_MAX_TOKENS = 4096

# Mirrors check_source_material_floor()'s own 900/420-word floor
# (app/services/script_checks.py) with a buffer, so a real expansion clears
# the floor with margin instead of skating the exact line.
_EXPANSION_WORD_TARGETS: dict[str, int] = {
    "youtube_long": 1200,
}
_EXPANSION_WORD_TARGET_DEFAULT = 600

_STORY_PREMISE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "A concrete, specific working title for the story.",
        },
        "body": {
            "type": "string",
            "description": "A short, concrete premise/pitch (3-6 sentences) — not the full story.",
        },
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

_STORY_PREMISE_SYSTEM_PROMPT = """\
You are an original-fiction premise writer for an automated video channel system.

Your task: invent a short, concrete story premise (a pitch, not the full story) that fits the
channel's niche and tone exactly. A human operator will read only this premise on a phone
screen and decide, from these few sentences alone, whether to greenlight full production.

Rules:
- This is ORIGINAL FICTION, written by you, not a real discovered post. Never frame it as a
  real Reddit/forum post: no invented usernames, no "OP", no fake upvote/comment counts, no
  "as told by u/...", no claim that this really happened to a real person online.
- Write 3-6 sentences. Concrete: name the situation, the central tension or mystery, and what
  makes it unsettling/compelling — not a vague theme or mood description.
- Ground it tightly in the channel's niche and tone provided below — it must read as something
  that channel would actually publish, not a generic story.
- Rights/IP safe: never use a real named public figure, a real named franchise/character/
  fictional world, or present a real identifiable person's real biography as fact. Original
  characters and settings only.
- Do not resolve the mystery/tension in the premise — leave the actual answer for the full
  story. The premise should create curiosity, not satisfy it.
- Never pad length to hit a target — stop once the premise is concrete and complete.

Return ONLY valid JSON. No markdown. No code fence.
Required format: {"title": "...", "body": "3-6 sentence premise"}\
"""

_STORY_EXPANSION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "body": {
            "type": "string",
            "description": "The full original story, in prose, in the target language.",
        },
    },
    "required": ["body"],
    "additionalProperties": False,
}

_STORY_EXPANSION_SYSTEM_PROMPT = """\
You are an original-fiction writer for an automated video channel system.

Your task: expand a human-approved story premise into a complete original story that will be
the sole source material for a video script. Nothing outside what you write here will be used
to ground the script — write the real story, not another summary.

Rules:
- This is ORIGINAL FICTION, written by you. Never frame it as a real Reddit/forum post, never
  invent usernames/upvotes/"OP", never claim this really happened to a real person online.
- Preserve the approved premise faithfully — the situation, central tension/mystery, and tone
  it establishes must carry through unchanged. Do not invent a different plot or twist the
  premise into a different story than what was approved.
- Write the complete story: setup, rising tension/escalation, and a real, concrete resolution.
  Include specific scenes, concrete details, and (where natural) dialogue — this must read as
  a full narrative, not an outline or a list of beats.
- Ground it in the channel's niche and tone provided below.
- Rights/IP safe: original characters and settings only — never a real named public figure or
  a real named franchise/character/fictional world.
- Never pad with filler or repetition to reach a length target — write a genuinely complete
  story; length follows naturally from telling it properly.
- Write in the target language provided below.

Return ONLY valid JSON. No markdown. No code fence.
Required format: {"body": "the complete story"}\
"""


def generate_story_premise(
    channel: Channel,
    language: str,
    rejected_stories: list[dict] | None = None,
) -> list[Story]:
    """Synthesize a short, concrete story premise grounded in channel config.

    Mirrors ``fetcher.fetch_batch()``'s contract exactly so callers in
    ``discovery.py`` can use either interchangeably: returns ``[Story]`` on
    success or ``[]`` on any failure, never raises. No ``web_search`` tool
    call — this is the cheap discovery-time stage; see the module docstring.

    Args:
        channel:          Channel ORM object (niche, tone, description).
        language:         Target BCP-47 language code for the premise.
        rejected_stories: Optional list of ``{"title": ..., "url": ...,
                          "feedback": ...}`` dicts — titles (and feedback,
                          when present) are threaded in as an "avoid similar
                          plots/themes" instruction. URLs are ignored (the
                          synthetic ``discovery://`` URL carries no meaning
                          to Claude here).

    Returns:
        List containing 0 or 1 Story objects.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Channel description: {channel.description or '(none provided)'}\n"
        f"Target language: {language}\n"
    )
    if rejected_stories:
        avoid_lines = "\n".join(
            f"  {i + 1}. {r['title']}" + (f" — {r['feedback']}" if r.get("feedback") else "")
            for i, r in enumerate(rejected_stories)
        )
        user_message += (
            f"\nAvoid similar plots/themes to these already-used premises:\n{avoid_lines}\n"
            "Invent a genuinely different premise — different situation, different central "
            "tension, not a reskin of any of the above."
        )

    try:
        result = call_claude_structured(
            task="story_synthesis",
            system_prompt=_STORY_PREMISE_SYSTEM_PROMPT,
            user_message=user_message,
            schema_name="story_premise",
            input_schema=_STORY_PREMISE_SCHEMA,
            max_tokens=_PREMISE_MAX_TOKENS,
        )
    except Exception as exc:
        logger.error("generate_story_premise: Claude call failed: %s", exc)
        return []

    title = (result.get("title") or "").strip()
    body = (result.get("body") or "").strip()
    if not title or not body:
        logger.error("generate_story_premise: empty title or body in response")
        return []

    word_count = len(body.split())
    if word_count > _PREMISE_SOFT_WORD_CEILING:
        logger.warning(
            "generate_story_premise: premise longer than expected (%d words, soft ceiling %d) "
            "— Claude ignored the length instruction; proceeding anyway (telemetry only)",
            word_count, _PREMISE_SOFT_WORD_CEILING,
        )

    story = Story(
        url=f"discovery://ai_generated/{channel.id}/{_uuid_module.uuid4()}",
        title=title,
        body=body,
        language=language,
        source_type="ai_generated",
        source_value="claude_synthesis",
        upvotes=0,
        comments=0,
    )
    logger.info(
        "generate_story_premise: synthesized premise (title=%r, body_words=%d)",
        title[:80], word_count,
    )
    return [story]


def expand_story_premise(
    premise: str,
    channel: Channel,
    script_format: str,
    language: str,
) -> str | None:
    """Expand a human-approved premise into a complete original story.

    Single Claude call, no retry loop beyond transport-failure handling
    (Elimination Mandate — a too-short result is not re-rolled here; it is
    caught by the existing ``check_source_material_floor()`` check
    downstream, exactly like a too-thin Reddit candidate).

    Args:
        premise:       The approved premise text (``Content.source_excerpt``
                       as it stood at approval time).
        channel:       Channel ORM object (niche, tone).
        script_format: Format key from ``channel_config.script_format`` —
                       determines the target word count.
        language:      Target BCP-47 language code — must match the
                       premise's language (preserved, not translated).

    Returns:
        The expanded story body text, or ``None`` on any failure (transport
        error, or an empty/missing body in the response).
    """
    word_target = _EXPANSION_WORD_TARGETS.get(script_format, _EXPANSION_WORD_TARGET_DEFAULT)
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Target language: {language}\n"
        f"Target length: approximately {word_target} words (a complete story, not a summary).\n\n"
        f"Approved premise to expand:\n{premise}"
    )

    try:
        result = call_claude_structured(
            task="story_synthesis",
            system_prompt=_STORY_EXPANSION_SYSTEM_PROMPT,
            user_message=user_message,
            schema_name="story_expansion",
            input_schema=_STORY_EXPANSION_SCHEMA,
            max_tokens=_EXPANSION_MAX_TOKENS,
        )
    except Exception as exc:
        logger.error("expand_story_premise: Claude call failed: %s", exc)
        return None

    body = (result.get("body") or "").strip()
    if not body:
        logger.error("expand_story_premise: empty body in response")
        return None

    logger.info(
        "expand_story_premise: expanded premise to %d words (target %d)",
        len(body.split()), word_target,
    )
    return body
