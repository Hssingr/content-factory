import json
import logging
import re

from app.services.claude_client import call_claude, call_claude_structured

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.3"  # v1.3: research_channel_ideas schema gains references_used array;
                        #        system prompt instructs Claude to include any relevant
                        #        URLs or named sources it knows about. Web search not wired
                        #        yet — references_used is populated from Claude's training
                        #        knowledge; a future phase will add real call_claude_with_tools
                        #        web search and replace these with live citations.
                        # v1.2: bump when any prompt below changes behaviour

# Keep this prompt stable across releases — edits invalidate the API-level cache
# for all users. Must stay above ~800 chars to trigger cache_control: ephemeral.
_SYSTEM_PROMPT = """\
== Your expertise ==

Content strategy
  • Deep knowledge of high-performing niches: technology, science, history, true crime, \
personal finance, health, lifestyle, geopolitics, space, psychology, philosophy, \
sports, and more.
  • Understanding of what makes content shareable and high-retention on each platform.

Multilingual content
  • Cultural adaptation, not translation. A French channel about history should reference \
Gallic heroes, not American ones. A Japanese channel about finance should reference the \
Nikkei, not the S&P 500.
  • Native naming conventions: channel names that sound natural in the target language, \
not transliterations.

Platform optimisation
  • YouTube: SEO-rich titles, 8–15 minute videos, evergreen topics, strong hooks.
  • TikTok: minimum 60 seconds for Creator Rewards monetization eligibility. \
Optimal range 60–180 seconds. First 3 seconds critical for hook. \
Sub-60s videos grow followers but earn zero revenue — avoid for monetized channels.
  • Instagram Reels: visual storytelling, emotion-first, 15–90 seconds optimal. \
Monetization via Ads on Reels — no strict minimum length but 30s+ performs better.
  • Facebook: community angle, longer descriptions, news-adjacent content performs well.

Voice & audio
  • ElevenLabs voice emotions: neutral, enthusiastic, calm, authoritative, dramatic, warm.
  • Music styles: cinematic, upbeat, ambient, dramatic, minimal, electronic, orchestral.
  • Match voice emotion and music to niche tone (e.g. true crime → calm + dramatic).

Content sources
  • RSS feeds, Reddit communities, YouTube channels, Hacker News, newsapi.org topics.
  • Always prefer sources that publish frequently and have high signal-to-noise ratio.
  • Match source language to the channel's source language, not necessarily the output language.

Publishing timing
  • Optimal slots per platform and timezone.
  • YouTube: weekday evenings (18h–21h local), Saturday morning.
  • TikTok: lunch (12h–14h) and evening (19h–22h).
  • Instagram: Tuesday/Thursday 11h–13h and 19h–21h.
  • Facebook: Wednesday/Thursday 13h–16h.

Niche-specific guidance
  • Technology: prioritise recency (< 72h), cite primary sources (Hacker News, vendor blogs).
  • True crime: calm+dramatic voice; avoid naming suspects in open cases; r/truecrime, \
CrimeReads RSS.
  • Personal finance: educational tone; avoid specific financial advice framing; \
r/personalfinance, Investopedia RSS.
  • History: evergreen beats current events; lesser-known events outperform famous ones; \
documentary tone dominates; Wikipedia Featured Articles RSS, JSTOR Daily RSS.
  • Science: metric units for non-US markets; NASA press releases RSS, Nature News RSS.
  • Shorts vs long-form: Shorts (60s–3min, vertical 9:16) drive discovery AND revenue since \
Oct 2024. Long-form (8–15 min) drives higher RPM. Publish Shorts first, then long-form.

== Fields you may be asked to suggest ==

  name           — Channel name for the given language/market. Unique, memorable, niche-relevant.
                   Do NOT append generic words like "Channel", "TV", "Official".
  description    — 1–2 sentence channel description in the user's language.
                   If no name or niche is provided in context (empty form), suggest a currently
                   trending social media topic as the channel concept — specific, timely, high-interest.
                   Otherwise describe the channel's value proposition clearly and concisely.
  niche          — Specific topic area. Be precise (e.g. "cold war espionage" not "history").
  tone           — Delivery tone: documentary | conversational | educational | entertaining | investigative
  voice_use_case — Best ElevenLabs use case for the channel's content style. Respond with ONLY one of:
                   conversational | narrative_story | characters_animation | social_media | informative_educational | advertisement | entertainment_tv
  voice_emotion  — TTS narrator emotion: neutral | enthusiastic | calm | authoritative | dramatic | warm
  music_style    — Background music: cinematic | upbeat | ambient | dramatic | minimal | electronic | orchestral
  voice_id       — Select the single best voice from the `available_voices` list provided in the context.
                   Each voice has: voice_id, name, gender, age, descriptive, description.
                   Choose based on the channel niche, tone, and target audience.
                   Respond with ONLY the voice_id string — no name, no explanation.
  source         — A real, working content source (full RSS URL, subreddit "r/name", or site URL).
                   Must match the channel's source language and niche.
  publish_timing — Return valid JSON only: {"days": [...], "hour_start": int, "hour_end": int}

== Rules ==

1. Respond with ONLY the suggested value. No explanation, no preamble, no trailing period \
   unless it is naturally part of the value.
2. Always respond in the language specified by `user_language` in the context. \
   Exception: when the field is `name` AND a specific `language` key is present, respond \
   in that target language (the channel name must be in the channel's own language).
3. For `source` fields, provide a real working URL or subreddit — never a placeholder. \
   The context may include `existing_sources` — never repeat a value already in that list.
4. For `publish_timing`, return raw JSON with no code fence.
5. Never suggest the same value twice if the user provides prior attempts in context.\
"""


def suggest_field(field: str, context: dict, max_tokens: int = 256) -> str:
    """Return a single AI-generated suggestion for a channel configuration field.

    Delegates to call_claude(). The system prompt exceeds 800 chars so
    cache_control: ephemeral is applied automatically.

    Args:
        field: The configuration field name (e.g. "name", "niche", "tone").
        context: Current channel state dict passed as user-visible context to Claude.
        max_tokens: Maximum tokens in the response (default 256).

    Returns:
        A single suggested value string, stripped of whitespace.

    Raises:
        ValueError: If context is not JSON-serializable or the API returns empty.
        anthropic.RateLimitError: If all retry attempts are exhausted.
        anthropic.APIConnectionError: On network or config errors (not retried).
        anthropic.APIError: On any other non-retryable API error.
    """
    try:
        context_str = json.dumps(context, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        logger.error("Context serialization error for field=%s: %s", field, exc)
        raise ValueError(f"Invalid context for field '{field}'") from exc

    user_message = f"Field: {field}\nContext:\n{context_str}"
    return call_claude(_SYSTEM_PROMPT, user_message, max_tokens=max_tokens, task="channel_suggestion")


# ── Publish timing suggestion ─────────────────────────────────────────────────

_TIMING_SYSTEM_PROMPT = """\
You are a social media publishing expert. Given a channel's language, audience locale,
niche, tone, and videos-per-week target, return the optimal publish schedule as JSON.

Rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Never invent timezone strings — use only valid IANA identifiers (e.g. "Europe/Paris").
3. `optimal_days` must be a JSON array of lowercase weekday names
   (e.g. ["friday", "saturday"]). Length must equal videos_per_week.
4. `optimal_hour_start` and `optimal_hour_end` are integers 0–23 in the LOCAL timezone.
5. `timezone` must be a valid IANA timezone string matching the language audience
   (e.g. "Europe/Paris" for French, "America/New_York" for English US).
6. `shorts_spread_hours` is the delay between the main video and Shorts uploads (default 6).

Output format:
{"timezone":"...","optimal_days":[...],"optimal_hour_start":18,"optimal_hour_end":20,"shorts_spread_hours":6}\
"""


def suggest_publish_timing(
    language: str,
    niche: str,
    videos_per_week: int,
    tone: str,
) -> dict:
    """Return the optimal publish schedule for one language audience.

    Calls Claude with a scheduling-expert prompt. The response is a JSON dict
    ready to be stored as a ``channel_publish_timing`` row.

    Args:
        language:        BCP-47 language code (e.g. "fr", "en", "de").
        niche:           Channel niche (e.g. "cold war espionage").
        videos_per_week: Target publish frequency.
        tone:            Channel tone (documentary | conversational | …).

    Returns:
        Dict with keys: timezone, optimal_days, optimal_hour_start,
        optimal_hour_end, shorts_spread_hours.

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    user_message = (
        f"Language / audience locale: {language}\n"
        f"Channel niche: {niche}\n"
        f"Channel tone: {tone}\n"
        f"Videos per week: {videos_per_week}\n\n"
        "Return the optimal publish schedule JSON."
    )
    data = call_claude_structured(
        task="channel_suggestion",
        system_prompt=_TIMING_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="publish_timing_suggestion",
        input_schema={
            "type": "object",
            "properties": {
                "timezone": {"type": "string"},
                "optimal_days": {"type": "array", "items": {"type": "string"}},
                "optimal_hour_start": {"type": "integer"},
                "optimal_hour_end": {"type": "integer"},
                "shorts_spread_hours": {"type": "integer"},
            },
            "required": ["timezone", "optimal_days", "optimal_hour_start", "optimal_hour_end"],
            "additionalProperties": False,
        },
        max_tokens=256,
    )

    # Warn if Claude returned wrong number of publish days
    days = data.get("optimal_days", [])
    if len(days) != videos_per_week:
        logger.warning(
            "suggest_publish_timing: days count %d != videos_per_week %d — using as-is",
            len(days), videos_per_week,
        )
    return data

# ── Channel idea research ─────────────────────────────────────────────────────

_RESEARCH_IDEAS_SYSTEM_PROMPT = """\
You are a combined YouTube strategist, short-form content strategist,
monetization analyst, and content production advisor for Content Factory.

Your task: analyze the operator's rough channel idea and return ONE primary
channel concept recommendation plus optional alternatives.

Important limits:
1. This is AI-assisted market research, not verified platform analytics.
2. Do not claim you checked live YouTube, TikTok, Instagram, Facebook, Reddit,
   RPM dashboards, or competitor analytics.
3. Do not invent exact verified numbers, exact RPM dollar values, audience sizes,
   or platform statistics. Use qualitative estimates only: low, medium, high,
   very_high.
4. Distinguish platform suitability, monetization potential, audience growth
   potential, production feasibility, sourcing feasibility, and risk level.
5. Optimize for sustainable repeatable content, strong retention, high
   monetization potential, cross-platform adaptation, feasible production with
   this pipeline, and compatibility with single_story mode.
6. If the operator's description is vague, still produce a useful recommendation
   and include an assumption_note explaining what you assumed.
7. Return direct editable config values. For script_source use "reddit" or
   "ai_generated" only. If explaining the source to the user, "ai_generated"
   means Claude Generated.
8. Prefer executable values when practical: content_mode single_story,
   script_source reddit, output_mode youtube_and_shorts. Recommend shorts_only
   only when the concept is genuinely short-form-first and note the tradeoff.
9. If script_source is reddit, include concrete subreddit names like r/name.
   If script_source is ai_generated, include a story_generation_prompt instead.
10. Recommended languages must be BCP-47-style short codes from this set when
    possible: en, fr, es, de, it, pt.
11. Recommended platforms must use: youtube, tiktok, instagram, facebook.
12. Explain WHY the subject was selected. The why_selected field is mandatory
    and should mention opportunity, retention, monetization, sourcing, and
    production feasibility where relevant.
13. In references_used, include any well-known subreddits, YouTube channels,
    RSS feeds, publications, or public reports you are confident exist and are
    directly relevant to this niche (e.g. "r/personalfinance", "Nature News RSS",
    "JSTOR Daily"). Only include sources you are confident are real. Do not invent
    URLs or fabricate source names. Leave the array empty when no well-known
    relevant source comes to mind. This is NOT a web search — these are known
    sources from training data.

Return ONLY valid JSON matching the provided schema. No markdown. No code fence.
"""

_RESEARCH_IDEAS_SCHEMA = {
    "type": "object",
    "properties": {
        "research_label": {
            "type": "string",
            "description": "Must say this is an AI market research estimate, not verified platform analytics.",
        },
        "primary_recommendation": {
            "type": "object",
            "properties": {
                "recommended_channel_concept": {"type": "string"},
                "why_selected": {"type": "string"},
                "rpm_potential": {"type": "string", "enum": ["low", "medium", "high", "very_high"]},
                "follower_growth_potential": {"type": "string", "enum": ["low", "medium", "high", "very_high"]},
                "platform_suitability": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "platform": {"type": "string", "enum": ["youtube", "tiktok", "instagram", "facebook"]},
                            "fit": {"type": "string", "enum": ["low", "medium", "high", "very_high"]},
                            "reasoning": {"type": "string"},
                        },
                        "required": ["platform", "fit", "reasoning"],
                        "additionalProperties": False,
                    },
                },
                "best_script_source": {"type": "string", "enum": ["reddit", "claude_generated"]},
                "recommended_output_mode": {"type": "string", "enum": ["youtube_and_shorts", "shorts_only"]},
                "recommended_visual_style": {"type": "string"},
                "recommended_image_style": {"type": "string"},
                "recommended_tone": {"type": "string"},
                "recommended_target_languages": {"type": "array", "items": {"type": "string"}},
                "recommended_platforms": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["youtube", "tiktok", "instagram", "facebook"]},
                },
                "suggested_channel_names": {"type": "array", "items": {"type": "string"}},
                "example_video_ideas": {"type": "array", "items": {"type": "string"}},
                "risks_difficulty": {"type": "array", "items": {"type": "string"}},
                "final_recommendation_summary": {"type": "string"},
                "assumption_note": {"type": ["string", "null"]},
                "editable_config": {
                    "type": "object",
                    "properties": {
                        "channel_name": {"type": "string"},
                        "description": {"type": "string"},
                        "niche": {"type": "string"},
                        "tone": {"type": "string"},
                        "script_source": {"type": "string", "enum": ["reddit", "ai_generated"]},
                        "output_mode": {"type": "string", "enum": ["youtube_and_shorts", "shorts_only"]},
                        "visual_style": {"type": "string"},
                        "image_style": {"type": "string"},
                        "languages": {"type": "array", "items": {"type": "string"}},
                        "platforms": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["youtube", "tiktok", "instagram", "facebook"]},
                        },
                        "videos_per_week": {"type": "integer", "minimum": 1, "maximum": 21},
                        "subreddits": {"type": "array", "items": {"type": "string"}},
                        "story_generation_prompt": {"type": ["string", "null"]},
                    },
                    "required": [
                        "channel_name", "description", "niche", "tone", "script_source",
                        "output_mode", "visual_style", "image_style", "languages",
                        "platforms", "videos_per_week", "subreddits",
                    ],
                    "additionalProperties": False,
                },
            },
            "required": [
                "recommended_channel_concept", "why_selected", "rpm_potential",
                "follower_growth_potential", "platform_suitability", "best_script_source",
                "recommended_output_mode", "recommended_visual_style",
                "recommended_image_style", "recommended_tone", "recommended_target_languages",
                "recommended_platforms", "suggested_channel_names", "example_video_ideas",
                "risks_difficulty", "final_recommendation_summary", "editable_config",
            ],
            "additionalProperties": False,
        },
        "alternative_ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept": {"type": "string"},
                    "why_it_could_work": {"type": "string"},
                    "main_tradeoff": {"type": "string"},
                },
                "required": ["concept", "why_it_could_work", "main_tradeoff"],
                "additionalProperties": False,
            },
        },
        "references_used": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Well-known subreddits, YouTube channels, RSS feeds, publications, or "
                "public reports directly relevant to this niche that Claude knows from "
                "training data. Only include sources you are confident are real. "
                "Leave empty when none are applicable."
            ),
        },
    },
    "required": ["research_label", "primary_recommendation", "alternative_ideas", "references_used"],
    "additionalProperties": False,
}


def research_channel_ideas(
    channel_description: str,
    content_mode: str = "single_story",
    target_languages: list[str] | None = None,
    target_platforms: list[str] | None = None,
    mode: str = "validate",
) -> dict:
    """Return structured AI market-research estimates for an Agent 1 channel idea.

    mode="explore"  — operator has no idea yet; description may be empty.
                      Claude proposes the best channel opportunity from scratch.
    mode="validate" — operator has an idea; description is required and Claude
                      analyses/refines it.

    This uses Claude only through the shared structured client. It does not call
    platform APIs, scrape platforms, or verify analytics; the returned label must
    keep that limitation visible to the operator.
    """
    description = (channel_description or "").strip()

    if mode == "validate" and not description:
        raise ValueError("channel_description is required for validate mode")

    # For explore mode with no description, give Claude an explicit open-ended brief
    # so rule 6 of the system prompt ("if description is vague, still produce a
    # useful recommendation") works as intended — Claude knows to freely propose.
    if mode == "explore" and not description:
        description = (
            "The operator has not provided a channel idea yet — propose the best "
            "channel opportunity for a new content creator starting from scratch. "
            "Focus on niches that have strong repeatable content potential, work "
            "well with Reddit-sourced stories, and are feasible with the Content "
            "Factory pipeline."
        )

    context = {
        "mode": mode,
        "channel_description": description,
        "content_mode": content_mode,
        "target_languages": target_languages or [],
        "target_platforms": target_platforms or [],
        "pipeline_constraints": {
            "currently_executable_content_mode": "single_story",
            "currently_executable_script_source": "reddit",
            "currently_executable_output_mode": "youtube_and_shorts",
            "no_platform_api_access": True,
            "no_verified_analytics": True,
            "operator_review_required": True,
        },
    }
    user_message = json.dumps(context, ensure_ascii=False, indent=2)
    return call_claude_structured(
        task="channel_research",
        system_prompt=_RESEARCH_IDEAS_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="channel_research_ideas",
        input_schema=_RESEARCH_IDEAS_SCHEMA,
        max_tokens=4096,
    )
