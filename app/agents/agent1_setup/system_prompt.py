import logging
import re

from app.services.claude_client import call_claude, parse_claude_json

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.1"  # bump when any prompt below changes behaviour

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
3. `optimal_hour_start` and `optimal_hour_end` are integers 0–23 in the LOCAL timezone.
4. `timezone` must be a valid IANA timezone string matching the language audience
   (e.g. "Europe/Paris" for French, "America/New_York" for English US).
5. `shorts_spread_hours` is the delay between the main video and Shorts uploads (default 6).

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
    raw = call_claude(_TIMING_SYSTEM_PROMPT, user_message, max_tokens=256, task="channel_suggestion")

    data = parse_claude_json(
        raw,
        required_keys=["timezone", "optimal_days", "optimal_hour_start", "optimal_hour_end"],
        type_checks={"timezone": str, "optimal_days": list,
                     "optimal_hour_start": int, "optimal_hour_end": int},
    )

    # Warn if Claude returned wrong number of publish days
    days = data.get("optimal_days", [])
    if len(days) != videos_per_week:
        logger.warning(
            "suggest_publish_timing: days count %d != videos_per_week %d — using as-is",
            len(days), videos_per_week,
        )
    return data
