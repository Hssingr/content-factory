"""AI-generated story discovery (``ChannelConfig.script_source="ai_generated"``).

Two-stage premise/expansion design — see
``code_report/ai_generated_story_discovery_design.md`` for the full design
record and CLAUDE.md §9.5 for the runtime architecture. Kept in a file
separate from ``fetcher.py`` specifically so "costs web_search money"
(fetcher.py) vs. "cheap synthesis, no web_search" (this file) stays
architecturally auditable at a glance.

Stage 1 (discovery time, cheap): ``generate_story_premise()`` — a short,
clear, DIRECT description of what the story is about (never a mysterious
teaser — the operator approves from this alone, see the prompt), grounded
only in the channel's existing niche/tone/description. Goes through the
*existing* dedup/scoring/Telegram-approval machinery unchanged, exactly like
a Reddit-discovered candidate.

Stage 1b (any CHANGE reply, still cheap): ``revise_story_premise()`` — the
operator's Telegram exchange for an ``ai_generated`` premise is a real
conversation, not a discard-and-search-again loop: a CHANGE reply revises
THE SAME premise in place, with the full prior conversation (every premise
Claude proposed and every operator reply) sent back to Claude each round, so
a later revision never contradicts or forgets an earlier one. This is
distinct from the ``"reddit"`` CHANGE path (``validation._handle_change()``'s
default branch), which rejects the candidate and re-runs discovery — there is
no "premise" to iterate on for a real discovered post, only a different post
to find. See CLAUDE.md §9.5's "Conversational premise revision" subsection.

Stage 2 (on APPROVE — the only point real cost is spent):
``expand_story_premise()`` expands the human-approved premise into a full
story body. The caller (``script_workflow._ensure_ai_story_expanded()``)
gates this on the *existing* source-material floor check, reusing the
pipeline's current fail-loud behavior rather than inventing a new one.

Per the Elimination Mandate (CLAUDE.md §9.3/§23), none of these functions
re-roll a result they judge too short/weak — a transport failure returns an
empty/``None`` result; a too-short-but-successful generation is left for the
existing deterministic floor check downstream to catch.
"""

import logging
import uuid as _uuid_module

from app.agents.agent2_discovery.services.story import Story
from app.models import Channel
from app.services.claude_client import call_claude_structured

logger = logging.getLogger(__name__)

# A premise is meant to be a short, Telegram-readable, DIRECT description
# (2-4 sentences) of what the story is about. Not hard-enforced (Elimination
# Mandate — no quality-judging retry here), but a result over this ceiling
# is logged so an ignored length instruction is visible instead of silently
# accepted.
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
            "description": (
                "A clear, direct, specific title that states exactly what the story is "
                "about — the subject/situation, not a vague or mysterious tease."
            ),
        },
        "body": {
            "type": "string",
            "description": (
                "A clear, direct 2-4 sentence description of exactly what the story is "
                "about — not a suspenseful teaser that withholds the subject."
            ),
        },
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

_STORY_PREMISE_SYSTEM_PROMPT = """\
You are an original-fiction premise writer for an automated video channel system.

Your task: invent a concrete original story idea that fits the channel's niche, tone, AND
description exactly, and describe it PLAINLY. A human operator will read only this title and
description on a phone screen and decide whether to greenlight full production — this is an
approval screen, not a viewer-facing hook. The operator must know EXACTLY what the story is
about from your title and description alone, the same way a clear video title/description
would tell you before you click play — for example, a title like "Hannibal: Carthage's War
Against Rome" and a description like "The story follows Hannibal of Carthage leading his
army, elephants included, across the Mediterranean and the Alps to fight Rome" tells the
operator precisely what they'd be approving. Do not write a title or description this vague or
unclear.

Rules:
- The channel description below is the most specific signal you have for what to write about —
  it is not background color, it is the actual subject-matter brief. If it names a topic, era,
  theme, or focus (e.g. "ancient military history and famous battles", "small-town urban
  legends"), the story you invent must be a concrete instance of exactly that — not a
  generically-themed substitute that only loosely fits the niche/tone. Niche and tone narrow
  the category and register; the description tells you the specific kind of story to write.
- This is ORIGINAL FICTION, written by you, not a real discovered post. Never frame it as a
  real Reddit/forum post: no invented usernames, no "OP", no fake upvote/comment counts, no
  "as told by u/...", no claim that this really happened to a real person online.
- Title: state the specific subject/situation plainly — name who or what the story is about
  and the core situation, not a cryptic or clickbait phrase.
- Description (body): write 2-4 sentences that plainly state the situation, the setting, and
  the people/characters and conflict involved — clearly enough that the operator could
  describe the story to someone else after reading it once. Do NOT hide or withhold the
  subject to build mystery — that works against the operator's ability to judge the pitch.
  You may still describe genuine narrative tension or conflict (a clear description of a
  compelling situation is not the same as a vague one) — just never sacrifice clarity for
  suspense. Leaving the story's eventual ending/resolution open is fine and expected; leaving
  the reader unsure what the story is even about is not.
- Ground it tightly in the channel's niche, tone, AND description provided below — it must
  read as something that channel would actually publish, not a generic story that merely fits
  the broad category.
- Rights/IP safe: never use a real named public figure, a real named franchise/character/
  fictional world, or present a real identifiable person's real biography as fact. Original
  characters and settings only.
- Never pad length to hit a target — stop once the description is clear and complete.

Return ONLY valid JSON. No markdown. No code fence.
Required format: {"title": "...", "body": "clear 2-4 sentence description"}\
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
- Ground it in the channel's niche, tone, AND description provided below — the description is
  the specific subject-matter brief the approved premise was itself written from; stay
  faithful to it, not just to the broader niche/tone category.
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


_STORY_PREMISE_REVISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "The revised title — clear and direct, same standard as the original premise."
            ),
        },
        "body": {
            "type": "string",
            "description": (
                "The revised 2-4 sentence description — clear and direct, addressing the "
                "operator's latest feedback while staying consistent with the conversation."
            ),
        },
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

_STORY_PREMISE_REVISION_SYSTEM_PROMPT = """\
You are revising a story premise for an automated video channel system, based on a real
back-and-forth conversation with the human operator who will approve it.

You will be shown the full conversation so far: every premise version you have proposed, and
the operator's feedback after each one, in order. Revise the premise to address the operator's
MOST RECENT feedback (the last line of the conversation), while staying consistent with
everything discussed before it — do not undo a change the operator already asked for and you
already made in an earlier turn, and do not change anything the operator did not ask you to
change. This is iterative revision of ONE ongoing premise, not a fresh, unrelated idea.

Rules:
- Same directness standard as the original premise: a clear, direct title naming the specific
  subject/situation, and a 2-4 sentence description that plainly states the situation, setting,
  and people/conflict involved — never a mystery teaser that withholds the subject. The
  operator is deciding whether to approve from this text alone.
- This is ORIGINAL FICTION, written by you, not a real discovered post. Never frame it as a
  real Reddit/forum post: no invented usernames, no "OP", no fake upvote/comment counts, no
  claim this really happened to a real person online.
- Ground it in the channel's niche, tone, AND description provided below — the description
  names the specific kind of story to write, not just the broad category.
- Rights/IP safe: never use a real named public figure, a real named franchise/character/
  fictional world, or present a real identifiable person's real biography as fact. Original
  characters and settings only.
- If the operator's feedback clearly asks for a different story entirely (not an adjustment to
  this one), treat that as the new direction — you are not required to preserve the old
  subject if the operator explicitly wants to abandon it.
- Never pad length to hit a target — stop once the description is clear and complete.

Return ONLY valid JSON. No markdown. No code fence.
Required format: {"title": "...", "body": "revised 2-4 sentence description"}\
"""


def revise_story_premise(
    channel: Channel,
    language: str,
    conversation: list[dict],
) -> dict | None:
    """Revise a story premise in place, based on a real conversation with the operator.

    Unlike ``generate_story_premise()`` (fresh synthesis, no history) and
    ``expand_story_premise()`` (post-approval expansion), this takes the
    FULL conversation so far — every premise version Claude has proposed and
    every operator feedback reply, in order — so a revision can genuinely
    build on the last one instead of discarding it. This is what makes the
    Telegram exchange for ``script_source="ai_generated"`` an actual
    conversation rather than a reject-and-search-again loop; see CLAUDE.md
    §9.5.

    Args:
        channel:      Channel ORM object (niche, tone, description).
        language:     Target BCP-47 language code — must match the
                      conversation's existing premise language.
        conversation: Ordered list of turns. Each turn is either
                      ``{"role": "assistant", "title": ..., "body": ...}``
                      (a premise Claude previously proposed) or
                      ``{"role": "operator", "feedback": ...}`` (the
                      operator's reply to that premise). Must end with an
                      ``"operator"`` turn — the feedback being addressed now.

    Returns:
        ``{"title": ..., "body": ...}`` on success, or ``None`` on any
        failure (transport error, or an empty title/body in the response).
    """
    transcript_lines: list[str] = []
    turn_num = 0
    for turn in conversation:
        if turn.get("role") == "assistant":
            turn_num += 1
            transcript_lines.append(
                f"{turn_num}. Your premise — Title: {turn.get('title', '')} | "
                f"Description: {turn.get('body', '')}"
            )
        elif turn.get("role") == "operator":
            transcript_lines.append(f"   Operator feedback: {turn.get('feedback', '')}")
    transcript = "\n".join(transcript_lines)

    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Channel description: {channel.description or '(none provided)'}\n"
        f"Target language: {language}\n\n"
        f"Conversation so far:\n{transcript}\n\n"
        "Revise the premise now, addressing the operator's most recent feedback above."
    )

    try:
        result = call_claude_structured(
            task="story_synthesis",
            system_prompt=_STORY_PREMISE_REVISION_SYSTEM_PROMPT,
            user_message=user_message,
            schema_name="story_premise_revision",
            input_schema=_STORY_PREMISE_REVISION_SCHEMA,
            max_tokens=_PREMISE_MAX_TOKENS,
        )
    except Exception as exc:
        logger.error("revise_story_premise: Claude call failed: %s", exc)
        return None

    title = (result.get("title") or "").strip()
    body = (result.get("body") or "").strip()
    if not title or not body:
        logger.error("revise_story_premise: empty title or body in response")
        return None

    logger.info(
        "revise_story_premise: revised premise (title=%r, body_words=%d, conversation_turns=%d)",
        title[:80], len(body.split()), len(conversation),
    )
    return {"title": title, "body": body}


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
        channel:       Channel ORM object (niche, tone, description).
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
        f"Channel description: {channel.description or '(none provided)'}\n"
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
