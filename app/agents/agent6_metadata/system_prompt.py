"""Agent 6 Claude prompts and schemas — mirrors agent4_visuals/system_prompt.py's
role for Agent 4.

Phase C (code_report/agent6_metadata_roadmap.md) implements the
``thumbnail_prompt_generation`` task: writes the Flux image prompt for a
content's YouTube thumbnail (stage 1 of the thumbnail pipeline), grounded in
story_blueprint + channel visual/image style (Check 5).

Phase D adds ``platform_metadata_generation`` (titles/descriptions/hashtags,
and — for platform="youtube" — thumbnail_text, Check 1 v2 decision).

Both task keys are already registered in app.services.model_routing (Phase B).
"""

import logging
import re

from app.services.claude_client import call_claude_structured

logger = logging.getLogger(__name__)

# Mirrors app.agents.agent4_visuals.services.flux_generator._TEXT_PROP_NO_TEXT_CLAUSE
# verbatim (Finding 3.7) — a local copy, not a cross-agent import, since Agent 6
# must not import app.agents.agent4_visuals (CLAUDE.md §6A ownership boundary;
# see the roadmap's Check 3 decision on why the shared Flux client stopped at
# app.services.flux_client and did not pull in Agent 4's storyboard-specific
# text-prop sanitization).
_NO_TEXT_CLAUSE = "no readable text or legible words in the frame"

# Same detection shape as storyboard_validator.py's check 19
# (ai_text_rendering_requested) — a quoted phrase, or an instruction asking
# the image model to render a specific piece of readable text. Deliberately
# narrow/deterministic (CLAUDE.md's accepted heuristic tradeoff, §11.4) — a
# self-contained equivalent, not a cross-agent import of Agent 4's checker.
_QUOTED_PHRASE_RE = re.compile(r'["“”].{2,}?["“”]')
_TEXT_RENDERING_PHRASES = (
    "sign that says", "sign reading", "text reads", "the text reads",
    "label reading", "banner that says", "banner reading", "words that read",
    "reads the text", "caption that says", "writing that says",
)

# Deterministic, always-safe fallback (mirrors flux_generator._ENV_SAFE_PROMPTS'
# philosophy — a generic, no-people, no-text scene) used only if two real
# Claude attempts both fail to produce a clean prompt. A thumbnail has no
# storyboard "environment" field to key off, so this is one fixed prompt, not
# a table.
_THUMBNAIL_SAFE_FALLBACK_PROMPT = (
    "Dramatic cinematic scene, dark atmospheric lighting, strong contrast "
    "between light and shadow, a single striking focal subject, "
    "photorealistic, sharp focus, no people, no text"
)

_THUMBNAIL_PROMPT_SYSTEM_PROMPT = """\
You are a thumbnail art director for Content Factory. Write ONE Flux image \
generation prompt for a YouTube thumbnail — a single, physical, camera-\
pointable scene, not a mood board.

The thumbnail must maximize click-through: a dramatic focal subject, high \
contrast, and a composition that reads clearly at small size (a viewer's \
feed thumbnail, not a full-screen image). Ground the scene in the story's \
own hook and characters/setting so it looks like it belongs to this video, \
not a generic stock photo.

Rules:
1. Describe ONE concrete physical scene: subject, framing, lighting, setting.
2. Never request readable text, a sign, a label, a caption, or any quoted \
phrase to appear in the image — describe the physical scene only, never the \
words on anything in it.
3. Match the channel's configured visual/image style when given.
4. Do not write a list, do not write multiple alternatives, do not include \
meta-commentary — the prompt is sent directly to an image model.

Return ONLY valid JSON matching the provided schema. No markdown. No code fence.
"""

_THUMBNAIL_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "flux_prompt": {
            "type": "string",
            "description": "One concrete, physical Flux image prompt for the thumbnail.",
        },
    },
    "required": ["flux_prompt"],
    "additionalProperties": False,
}


def _violates_no_text_rule(prompt: str) -> bool:
    """True if ``prompt`` asks the image model to render readable text —
    same detection shape as storyboard_validator.py's check 19."""
    if _QUOTED_PHRASE_RE.search(prompt):
        return True
    lowered = prompt.lower()
    return any(phrase in lowered for phrase in _TEXT_RENDERING_PHRASES)


def _build_user_message(
    hook: str, character_descriptors: list[dict], era_setting: str,
    visual_style: str, image_style: str,
) -> str:
    lines = []
    if hook:
        lines.append(f"Story hook: {hook}")
    if character_descriptors:
        names = ", ".join(
            f"{c.get('name', '')} ({c.get('description', '')})"
            for c in character_descriptors if c.get("name")
        )
        if names:
            lines.append(f"Characters: {names}")
    if era_setting:
        lines.append(f"Era/setting: {era_setting}")
    if visual_style:
        lines.append(f"Channel visual style: {visual_style}")
    if image_style:
        lines.append(f"Channel image style: {image_style}")
    if not lines:
        lines.append("No story details available — invent a striking, generic dramatic scene.")
    return "\n".join(lines)


def generate_thumbnail_prompt(
    story_blueprint: dict | None,
    visual_style: str = "",
    image_style: str = "",
) -> str:
    """Return the final Flux prompt for a content's YouTube thumbnail base
    image, with the no-text clause always appended deterministically.

    Claude is advisory only (CLAUDE.md "Claude reasons, Python decides"):
    Python enforces the no-text rule by keyword/quoted-phrase detection,
    retries the Claude call exactly once on a violation, and falls back to a
    fixed, always-safe template prompt if both attempts fail — this function
    never raises and never returns an empty string.

    Args:
        story_blueprint: Content.story_blueprint dict (hook,
            character_descriptors, era_setting) — grounding only, may be
            None/empty (a legitimately thin or missing blueprint).
        visual_style:    ChannelConfig.visual_style, may be empty.
        image_style:     ChannelConfig.image_style, may be empty.

    Returns:
        A non-empty Flux prompt string, always ending with the no-text clause.
    """
    blueprint = story_blueprint or {}
    user_message = _build_user_message(
        hook=str(blueprint.get("hook") or ""),
        character_descriptors=blueprint.get("character_descriptors") or [],
        era_setting=str(blueprint.get("era_setting") or ""),
        visual_style=visual_style,
        image_style=image_style,
    )

    raw_prompt = ""
    for attempt in range(2):
        try:
            result = call_claude_structured(
                task="thumbnail_prompt_generation",
                system_prompt=_THUMBNAIL_PROMPT_SYSTEM_PROMPT,
                user_message=user_message,
                schema_name="thumbnail_prompt",
                input_schema=_THUMBNAIL_PROMPT_SCHEMA,
                max_tokens=512,
            )
            candidate = str(result.get("flux_prompt") or "").strip()
        except Exception as exc:
            logger.warning(
                "THUMBNAIL_PROMPT_CLAUDE_CALL_FAILED attempt=%d exception_type=%s "
                "exception_message=%s",
                attempt + 1, type(exc).__name__, str(exc)[:200],
            )
            candidate = ""

        if candidate and not _violates_no_text_rule(candidate):
            return f"{candidate.rstrip('. ')}, {_NO_TEXT_CLAUSE}."

        if candidate:
            logger.warning(
                "THUMBNAIL_PROMPT_TEXT_VIOLATION attempt=%d prompt=%s",
                attempt + 1, candidate[:200],
            )

    logger.warning("THUMBNAIL_PROMPT_FALLBACK_TO_SAFE_TEMPLATE — both attempts unusable")
    return f"{_THUMBNAIL_SAFE_FALLBACK_PROMPT}, {_NO_TEXT_CLAUSE}."


# ── platform_metadata_generation (Phase D) ──────────────────────────────────
# Titles/descriptions/hashtags per platform, and — for platform="youtube"
# only — thumbnail_text (Check 1 v2 decision: the short overlay phrase,
# never the title). One Claude call per (language, platform) pair — no
# content-quality retry loop here (unlike thumbnail_prompt_generation's
# no-text retry): CLAUDE.md's Elimination Mandate retired quality-judging
# retries throughout this codebase; length/hashtag-count issues are fixed
# deterministically afterward by platform_limits.py, which needs no retry
# either. A hard transport failure returns None — the caller treats that
# (language, platform) pair as having no metadata, skipping its overlay
# non-fatally (never blocks the rest of the content item).

_PLATFORM_STYLE_GUIDANCE: dict[str, str] = {
    "youtube": (
        "YouTube: write a longer, SEO-optimized title (naming the specific "
        "subject/hook, not clickbait) and a longer, informative description "
        "suitable for YouTube search and suggested-video placement."
    ),
    "tiktok": (
        "TikTok: write a short, punchy title/caption. Keep the description "
        "brief and hook-driven — TikTok captions are read in passing, not "
        "studied."
    ),
    "instagram": (
        "Instagram: write an emotional-hook title. Make the description "
        "hashtag-forward and scroll-stopping."
    ),
    "facebook": (
        "Facebook: write a conversational title, like a friend telling the "
        "story. The description may be longer and more narrative than the "
        "other platforms."
    ),
}

_PLATFORM_METADATA_SYSTEM_PROMPT = """\
You are a platform metadata copywriter for Content Factory. Write a title, \
description, and hashtag list for ONE specific platform, from a video's own \
narration and story details — never a generic, reusable-anywhere summary.

Write in the requested language. Match the platform-specific style \
guidance given to you exactly — a YouTube title and a TikTok title for the \
same video should read differently, not be the same string reused.

If asked for thumbnail_text (only requested for platform=youtube), write a \
short, punchy phrase — 2 to 5 words — that works ALONGSIDE the title, not a \
restatement of it. If thumbnail_text is not requested for this platform, \
return an empty string for it.

Return ONLY valid JSON matching the provided schema. No markdown. No code fence.
"""

_PLATFORM_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "thumbnail_text": {
            "type": "string",
            "description": "Only meaningful when platform=youtube; empty string otherwise.",
        },
    },
    "required": ["title", "description", "hashtags", "thumbnail_text"],
    "additionalProperties": False,
}


def _build_platform_metadata_user_message(
    platform: str, language: str, voice_script: str,
    hook: str, central_question: str, niche: str, tone: str,
) -> str:
    lines = [
        _PLATFORM_STYLE_GUIDANCE.get(platform, f"Platform: {platform}."),
        f"Write in language: {language}",
        f"Request thumbnail_text: {'yes' if platform == 'youtube' else 'no'}",
    ]
    if niche:
        lines.append(f"Channel niche: {niche}")
    if tone:
        lines.append(f"Channel tone: {tone}")
    if hook:
        lines.append(f"Story hook: {hook}")
    if central_question:
        lines.append(f"Central question: {central_question}")
    lines.append(f"Full narration:\n{voice_script}")
    return "\n".join(lines)


def generate_platform_metadata(
    platform: str,
    language: str,
    voice_script: str,
    story_blueprint: dict | None,
    niche: str = "",
    tone: str = "",
) -> dict | None:
    """Generate title/description/hashtags (and, for platform="youtube",
    thumbnail_text) for one (language, platform) pair.

    Args:
        platform:        One of platform_limits.PLATFORM_LIMITS's keys.
        language:        BCP-47 language code to write in.
        voice_script:    Full narration text (Script.voice_script) —
            untruncated, matching the "no truncated grounding" lesson
            CLAUDE.md §9.4 already learned for the Shorts planner.
        story_blueprint: Content.story_blueprint dict — hook/central_question
            grounding only, may be None/empty.
        niche, tone:     Channel.niche / Channel.tone register.

    Returns:
        ``{"title": str, "description": str, "hashtags": list[str],
        "thumbnail_text": str}`` (raw — platform_limits enforcement is the
        caller's job, not this function's) on success, or ``None`` on a hard
        transport failure — never raises.
    """
    blueprint = story_blueprint or {}
    user_message = _build_platform_metadata_user_message(
        platform=platform,
        language=language,
        voice_script=voice_script,
        hook=str(blueprint.get("hook") or ""),
        central_question=str(blueprint.get("central_question") or ""),
        niche=niche,
        tone=tone,
    )

    try:
        result = call_claude_structured(
            task="platform_metadata_generation",
            system_prompt=_PLATFORM_METADATA_SYSTEM_PROMPT,
            user_message=user_message,
            schema_name="platform_metadata",
            input_schema=_PLATFORM_METADATA_SCHEMA,
            max_tokens=2048,
        )
    except Exception as exc:
        logger.warning(
            "PLATFORM_METADATA_CLAUDE_CALL_FAILED platform=%s language=%s "
            "exception_type=%s exception_message=%s",
            platform, language, type(exc).__name__, str(exc)[:200],
        )
        return None

    return {
        "title": str(result.get("title") or ""),
        "description": str(result.get("description") or ""),
        "hashtags": [str(h) for h in (result.get("hashtags") or [])],
        "thumbnail_text": str(result.get("thumbnail_text") or ""),
    }
