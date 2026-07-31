import json
import logging
import re

from app.services.claude_client import call_claude, call_claude_structured

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.6"  # v1.6: research_channel_ideas() split into two sequential structured calls
                        #        (channel_concept_validation -> channel_research) — the single
                        #        ~30-required-leaf-field call was unreliable under Anthropic's
                        #        forced tool-use (the "required" list is not strictly enforced);
                        #        step 1 now owns editable_config exclusively (the part that was
                        #        always reliably filled in both real production incidents), step 2
                        #        owns narrative analysis only and is never asked for the same
                        #        decision twice. _backfill_recommendation_from_editable_config()
                        #        (patched around one symptom) is removed — the redesign removes the
                        #        duplication that caused it at the schema level instead.
                        # v1.5: added "cinematic_cartoon" image_style preset (operator request — historical
                        #        channel wanted a cinematic_cartoon-like look distinct from anime/digital_art).
                        #        _IMAGE_STYLE_VALUES gains "cinematic_cartoon", mirrored in
                        #        app/ui/src/constants.js IMAGE_STYLE_OPTIONS and Agent 4's
                        #        storyboard prompt vocabulary (agent4_visuals/system_prompt.py).
                        # v1.4: post-roadmap deep audit — suggest prompt's tone vocabulary
                        #        expanded to include tension registers (suspenseful/ominous/
                        #        dramatic/...) matching the UI TONES dropdown, closing the
                        #        P1-9 root cause (horror channels were forced into
                        #        documentary/educational tones). Dead suggest fields removed
                        #        (voice_use_case/voice_emotion/music_style/voice_id/
                        #        publish_timing — no UI requests them; publish timing has its
                        #        own dedicated prompt). research_channel_ideas visual_style/
                        #        image_style constrained to the canonical preset values so
                        #        "Use this recommendation" can never set a value the setup
                        #        dropdowns cannot display.
                        # v1.3: research_channel_ideas schema gains references_used array;
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
  tone           — Delivery tone. Pick the value that matches the niche's actual register:
                   suspenseful | ominous | dramatic | conversational | documentary | educational |
                   entertaining | investigative | humorous | inspirational
                   Tension/dread niches (horror, true crime, mystery, thriller) need a tension
                   tone (suspenseful/ominous/dramatic) — never documentary/educational for those.
  source         — A real, working content source (full RSS URL, subreddit "r/name", or site URL).
                   Must match the channel's source language and niche.

== Rules ==

1. Respond with ONLY the suggested value. No explanation, no preamble, no trailing period \
   unless it is naturally part of the value.
2. Always respond in the language specified by `user_language` in the context. \
   Exception: when the field is `name` AND a specific `language` key is present, respond \
   in that target language (the channel name must be in the channel's own language).
3. For `source` fields, provide a real working URL or subreddit — never a placeholder. \
   The context may include `existing_sources` — never repeat a value already in that list.
4. Never suggest the same value twice if the user provides prior attempts in context.\
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
        task="publish_timing_suggestion",
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

# Canonical style presets — mirror app/ui/src/constants.js VISUAL_STYLE_OPTIONS /
# IMAGE_STYLE_OPTIONS values exactly. The research schema constrains Claude's
# style recommendations to these so "Use this recommendation" always produces a
# value the setup dropdowns can display (the ChannelConfig columns themselves
# stay free-form).
_VISUAL_STYLE_VALUES = [
    "story_driven", "documentary", "true_crime", "investigative", "cinematic",
    "historical", "noir", "suspense_thriller", "nature", "educational", "retro",
]
_IMAGE_STYLE_VALUES = [
    "photorealistic", "cinematic_realism", "dark_realistic", "vintage_film",
    "digital_art", "cinematic_cartoon", "oil_painting", "watercolor", "anime",
]
# Mirrors app/ui/src/constants.js TONES exactly, same reason as the two style
# lists above: recommended_tone/editable_config.tone were previously free
# strings, the one field in this schema NOT enum-constrained to its UI
# dropdown's preset list despite carrying the identical mismatch risk
# visual_style/image_style were already fixed for.
_TONE_VALUES = [
    "suspenseful", "ominous", "dramatic", "conversational", "documentary",
    "educational", "entertaining", "investigative", "humorous", "inspirational",
]

# `research_channel_ideas()` is split into two sequential structured Claude
# calls (roadmap: two-call reliability redesign) instead of one combined
# call. The combined call's schema required ~30 leaf fields in one
# forced-tool-use response (18 under primary_recommendation, 12 more nested
# inside its own editable_config) and failed twice in real production runs
# with FastAPI ResponseValidationError — Anthropic's forced tool-use does
# not strictly enforce a JSON Schema's "required" list. Critically, in BOTH
# real failures `editable_config` itself was always complete and correct;
# what got dropped was either most of the narrative fields (a genuine
# max_tokens truncation) or exactly six fields that are literal semantic
# duplicates of editable_config's own fields (recommended_output_mode/
# recommended_visual_style/recommended_image_style/recommended_tone/
# recommended_target_languages/recommended_platforms all mirror
# editable_config's output_mode/visual_style/image_style/tone/languages/
# platforms) — Claude appears to treat the top-level copy as redundant once
# the same decision exists elsewhere in the response, and simply omits it.
#
# Step 1 (_CONCEPT_VALIDATION_SCHEMA, task="channel_concept_validation")
# owns exactly the editable_config shape that survived both incidents
# unscathed, plus a cheap validation-issues list — 13 required leaf fields.
# Step 2 (_RESEARCH_NARRATIVE_SCHEMA, task="channel_research") owns only the
# narrative/analysis half and is structurally never given best_script_source
# or any recommended_* field to fill in — they don't exist in its schema at
# all, so it cannot omit them. research_channel_ideas() calls step 1, then
# step 2 (grounded in step 1's finalized editable_config), then merges both
# into today's unchanged ResearchIdeasResponse shape in _merge_research_steps()
# — every recommended_* field and best_script_source are derived once, in
# Python, directly from editable_config; Claude is never asked for the same
# decision twice. See CLAUDE.md §8.5 for the full design writeup.

_CONCEPT_VALIDATION_SYSTEM_PROMPT = """\
You are a channel-setup configuration advisor for Content Factory. Your task:
read the operator's rough channel idea and produce ONE fully-populated,
directly-executable editable configuration for a new channel. Return direct,
concrete values for every field — never a placeholder, never a list of
options, never "TBD".

Do not write marketing copy, growth analysis, or platform strategy here —
that is a separate pass. Focus entirely on turning the description into
concrete, valid configuration values.

Rules:
1. If the operator's description is vague, incomplete, or contradictory,
   still produce a complete, usable configuration — never ask a follow-up
   question. List every material gap or assumption in description_issues
   (e.g. "no target platform given — defaulted to YouTube + TikTok"). Leave
   description_issues empty when the description is already clear.
2. Set assumption_note to one short operator-facing sentence naming the
   single most important assumption behind your configuration choices. Set
   it to null when nothing meaningful was assumed.
3. Choose configuration values (script_source, output_mode, platforms,
   videos_per_week) that support strong retention and monetization potential
   where relevant, while staying feasible to produce with this pipeline and
   compatible with single_story mode. Deeper reasoning about WHY belongs to a
   later pass — here, make the concrete decision.
4. Prefer executable values when practical: script_source reddit, output_mode
   youtube_and_shorts (or youtube_long_only when Shorts genuinely do not fit
   the concept). Recommend shorts_only only when the concept is genuinely
   short-form-first; it is not executable yet.
5. For script_source use "reddit" or "ai_generated" only. If script_source is
   reddit, include concrete subreddit names like r/name in subreddits. If
   script_source is ai_generated, include a story_generation_prompt instead
   and leave subreddits empty.
6. Languages must be BCP-47-style short codes from this set when possible:
   en, fr, es, de, it, pt.
7. Platforms must use only: youtube, tiktok, instagram, facebook.

Return ONLY valid JSON matching the provided schema. No markdown. No code
fence.
"""

_CONCEPT_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "description_issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Gaps/assumptions the description required filling. Empty when already clear.",
        },
        "assumption_note": {"type": ["string", "null"]},
        "editable_config": {
            "type": "object",
            "properties": {
                "channel_name": {"type": "string"},
                "description": {"type": "string"},
                "niche": {"type": "string"},
                "tone": {"type": "string", "enum": _TONE_VALUES},
                "script_source": {"type": "string", "enum": ["reddit", "ai_generated"]},
                "output_mode": {"type": "string", "enum": ["youtube_and_shorts", "youtube_long_only", "shorts_only"]},
                "visual_style": {"type": "string", "enum": _VISUAL_STYLE_VALUES},
                "image_style": {"type": "string", "enum": _IMAGE_STYLE_VALUES},
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
    "required": ["description_issues", "editable_config"],
    "additionalProperties": False,
}

_RESEARCH_NARRATIVE_SYSTEM_PROMPT = """\
You are a combined YouTube strategist, short-form content strategist, and
monetization analyst for Content Factory. A channel configuration has
already been finalized (finalized_editable_config in the context) — your
job is to analyze and explain that already-decided concept, not to change or
re-derive any configuration value.

Important limits:
1. This is AI-assisted market research, not verified platform analytics.
2. Do not claim you checked live YouTube, TikTok, Instagram, Facebook,
   Reddit, RPM dashboards, or competitor analytics.
3. Do not invent exact verified numbers, exact RPM dollar values, audience
   sizes, or platform statistics. Use qualitative estimates only: low,
   medium, high, very_high.
4. Distinguish platform suitability, monetization potential, audience growth
   potential, and risk level; ground each judgment in why THIS configuration
   specifically is or isn't well suited, not generic platform advice.
5. Explain how this configuration supports sustainable, repeatable content,
   strong retention, cross-platform adaptation, and monetization — this is
   analysis for the operator to decide whether to proceed, not a request to
   change any configuration value.
6. Explain WHY the subject was selected. why_selected is mandatory and
   should mention opportunity, retention, monetization, sourcing, and
   production feasibility where relevant.
7. In references_used, include any well-known subreddits, YouTube channels,
   RSS feeds, publications, or public reports you are confident exist and
   are directly relevant to this niche. Only include sources you are
   confident are real — do not invent URLs or fabricate source names. Leave
   the array empty when none come to mind. This is NOT a web search — these
   are known sources from training data.
8. If description_issues/assumption_note in the context describe a gap or
   assumption, stay consistent with it — do not contradict an assumption
   already made about platforms, languages, tone, or sourcing.

Return ONLY valid JSON matching the provided schema. No markdown. No code
fence.
"""

_RESEARCH_NARRATIVE_SCHEMA = {
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
        "suggested_channel_names": {"type": "array", "items": {"type": "string"}},
        "example_video_ideas": {"type": "array", "items": {"type": "string"}},
        "risks_difficulty": {"type": "array", "items": {"type": "string"}},
        "final_recommendation_summary": {"type": "string"},
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
    "required": [
        "recommended_channel_concept", "why_selected", "rpm_potential",
        "follower_growth_potential", "platform_suitability",
        "suggested_channel_names", "example_video_ideas", "risks_difficulty",
        "final_recommendation_summary", "references_used",
    ],
    "additionalProperties": False,
}

_RESEARCH_LABEL = "AI market research estimate — not verified platform analytics"


def _merge_research_steps(concept: dict, narrative: dict) -> dict:
    """Deterministically merge step 1 (channel_concept_validation) and step 2
    (channel_research) outputs into the exact ResearchIdeasResponse shape the
    frontend already expects. The six recommended_* fields and
    best_script_source are never asked of Claude twice — they are always
    copied from step 1's editable_config here, in Python (CLAUDE.md §8.5)."""
    editable_config = concept["editable_config"]
    primary_recommendation = {
        "recommended_channel_concept": narrative["recommended_channel_concept"],
        "why_selected": narrative["why_selected"],
        "rpm_potential": narrative["rpm_potential"],
        "follower_growth_potential": narrative["follower_growth_potential"],
        "platform_suitability": narrative["platform_suitability"],
        "best_script_source": editable_config["script_source"],
        "recommended_output_mode": editable_config["output_mode"],
        "recommended_visual_style": editable_config["visual_style"],
        "recommended_image_style": editable_config["image_style"],
        "recommended_tone": editable_config["tone"],
        "recommended_target_languages": editable_config["languages"],
        "recommended_platforms": editable_config["platforms"],
        "suggested_channel_names": narrative["suggested_channel_names"],
        "example_video_ideas": narrative["example_video_ideas"],
        "risks_difficulty": narrative["risks_difficulty"],
        "final_recommendation_summary": narrative["final_recommendation_summary"],
        "assumption_note": concept.get("assumption_note"),
        "editable_config": editable_config,
    }
    return {
        "research_label": _RESEARCH_LABEL,
        "primary_recommendation": primary_recommendation,
        "references_used": narrative.get("references_used", []),
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

    Two sequential Claude calls (see the module comment above): step 1
    (task="channel_concept_validation") turns the description into a fully
    -populated editable_config; step 2 (task="channel_research") analyzes
    and explains that already-decided configuration. Neither call is asked
    for the same decision twice.

    This uses Claude only through the shared structured client. It does not call
    platform APIs, scrape platforms, or verify analytics; the returned label must
    keep that limitation visible to the operator.
    """
    description = (channel_description or "").strip()

    if mode == "validate" and not description:
        raise ValueError("channel_description is required for validate mode")

    # For explore mode with no description, give Claude an explicit open-ended brief
    # so step 1's rule 1 ("if description is vague, still produce a useful
    # configuration") works as intended — Claude knows to freely propose.
    if mode == "explore" and not description:
        description = (
            "The operator has not provided a channel idea yet — propose the best "
            "channel opportunity for a new content creator starting from scratch. "
            "Focus on niches that have strong repeatable content potential, work "
            "well with Reddit-sourced stories, and are feasible with the Content "
            "Factory pipeline."
        )

    pipeline_constraints = {
        "currently_executable_content_mode": "single_story",
        "currently_executable_script_source": "reddit",
        "currently_executable_output_modes": ["youtube_and_shorts", "youtube_long_only"],
        "no_platform_api_access": True,
        "no_verified_analytics": True,
        "operator_review_required": True,
    }

    # ── Step 1: turn the description into a concrete, valid configuration ──
    concept_user_message = json.dumps({
        "mode": mode,
        "channel_description": description,
        "content_mode": content_mode,
        "target_languages": target_languages or [],
        "target_platforms": target_platforms or [],
        "pipeline_constraints": pipeline_constraints,
    }, ensure_ascii=False, indent=2)
    concept = call_claude_structured(
        task="channel_concept_validation",
        system_prompt=_CONCEPT_VALIDATION_SYSTEM_PROMPT,
        user_message=concept_user_message,
        schema_name="channel_concept_validation",
        input_schema=_CONCEPT_VALIDATION_SCHEMA,
        max_tokens=2048,
    )

    # ── Step 2: analyze and explain that already-decided configuration ──
    narrative_user_message = json.dumps({
        "mode": mode,
        "channel_description": description,
        "content_mode": content_mode,
        "pipeline_constraints": pipeline_constraints,
        "finalized_editable_config": concept["editable_config"],
        "description_issues": concept.get("description_issues", []),
        "assumption_note": concept.get("assumption_note"),
    }, ensure_ascii=False, indent=2)
    narrative = call_claude_structured(
        task="channel_research",
        system_prompt=_RESEARCH_NARRATIVE_SYSTEM_PROMPT,
        user_message=narrative_user_message,
        schema_name="channel_research_narrative",
        input_schema=_RESEARCH_NARRATIVE_SCHEMA,
        max_tokens=8192,
    )

    return _merge_research_steps(concept, narrative)
