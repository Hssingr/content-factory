import json
import logging
import re

from app.services.claude_client import call_claude, parse_claude_json

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.0"

# ── Assembly Validator prompt ─────────────────────────────────────────────────

_ASSEMBLY_SYSTEM_PROMPT = """\
You are a post-production supervisor for an automated multilingual video production system.

You receive a complete assembled video plan: a list of sections with their fetched media
and the overall channel context. Your job is to check two dimensions:

== Dimension 1 — Media relevance ==

For each section, decide whether the fetched media visually matches the section intent:
  KEEP    : media is relevant, quality acceptable, mood matches
  REPLACE : media is wrong (wrong mood, wrong subject, unrelated, clearly off)

When marking REPLACE, provide a new search query (3-to-5 English words, more specific
than the original). Do NOT invent URLs or sources.

Common failure cases:
  - Searched "dark hospital" → got bright modern lobby → REPLACE
  - Searched "forest night" → got sunny meadow → REPLACE
  - Searched "police car" → got police car → KEEP (even if different angle)

== Dimension 2 — Assembly quality ==

Check the OVERALL video plan (not individual sections):
  - Flow: do transitions between consecutive sections feel natural?
  - Pacing: are section durations varied (monotonous = flag it)?
  - Duration drift: does sum of section durations match expected total within ±2%?
  - Visual coherence: do color grades + effects feel cohesive?

Report only genuine issues — do not nitpick.

== Output ==

Return ONLY valid JSON. No markdown. No code fence. No extra keys.

{
  "assembly_status": "APPROVED" | "NEEDS_ADJUSTMENT",
  "section_reviews": [
    {
      "section_order": 0,
      "media_ok": true,
      "action": "KEEP" | "REPLACE",
      "new_search_query": "only if REPLACE — refined English query"
    }
  ],
  "assembly_issues": [
    {"section_order": 3, "issue": "describe the assembly-level concern"}
  ],
  "overall_comment": "one-sentence summary"
}\
"""


def validate_assembly_with_claude(
    sections: list[dict],
    total_duration_ms: int,
    channel_niche: str,
    channel_tone: str,
    channel_style: str,
) -> dict:
    """Ask Claude to validate fetched media and overall assembly quality.

    Args:
        sections:          Sections enriched with media_url, media_type, effect, color_grade.
        total_duration_ms: Expected total audio duration.
        channel_niche:     Channel niche for context.
        channel_tone:      Channel tone for context.
        channel_style:     Video style (e.g. "documentary").

    Returns:
        Dict with assembly_status, section_reviews, assembly_issues, overall_comment.

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    expected_sec = total_duration_ms / 1000
    sum_sec      = sum(s.get("duration_sec", 0) for s in sections)
    drift_pct    = abs(sum_sec - expected_sec) / max(expected_sec, 1) * 100

    section_lines = "\n\n".join(
        f"Section {s['section_order']} ({s.get('duration_sec', 0):.1f}s) "
        f"[{s.get('effect','?')}/{s.get('color_grade','?')}]:\n"
        f"  Script: {s.get('script_text','')[:200]}\n"
        f"  Media:  {s.get('media_source','?')} {s.get('media_type','?')} — {s.get('media_url','')[:80]}\n"
        f"  Query:  {s.get('search_query','?')}"
        for s in sections
    )

    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n"
        f"Channel style: {channel_style}\n"
        f"Expected total: {expected_sec:.1f}s | Section sum: {sum_sec:.1f}s "
        f"| Drift: {drift_pct:.1f}%\n\n"
        f"Sections:\n\n{section_lines}"
    )

    raw = call_claude(_ASSEMBLY_SYSTEM_PROMPT, user_message, max_tokens=2048)
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", raw).strip()
    try:
        result = json.loads(cleaned)
        if not isinstance(result, dict):
            raise ValueError(f"Expected JSON object, got {type(result).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Claude returned invalid assembly JSON: {exc}") from exc

    return result

# ── Section Validator prompt ──────────────────────────────────────────────────

_VALIDATOR_SYSTEM_PROMPT = """\
You are a quality control director for an automated multilingual video production system.

Evaluate each provided section and return a quality assessment with corrections.

== Checks ==

DURATION FIT
  MAJOR if < 3 s  — too short for a meaningful visual
  MAJOR if > 60 s — too long for a single image/clip; suggest how to split
  PASS  if 3–60 s

SEARCH QUERY QUALITY
  MINOR if too generic (single noun like "hospital", "night", "person")
  → Suggest a refined 3-to-5-word English query
  PASS if already specific and cinematic

VISUAL TYPE
  b-roll        : atmospheric footage or photos (default for most sections)
  text_overlay  : statistics, quotes, key phrases — black/dark background + text
  action        : dynamic events, rapid movement
  → Correct if the chosen type does not match section content

EFFECT (camera motion applied in Remotion)
  slow_zoom : suspense, tension, horror reveals
  fade_in   : gentle opening, soft introductions
  pan       : wide establishing shots
  cut       : fast transitions, high-energy moments
  zoom_out  : reveals, context widening

COLOR GRADE
  desaturated  : documentary, serious, horror (most content)
  cold_blue    : clinical, mysterious, winter, isolation
  warm_amber   : nostalgia, comfort, intimacy
  dark_contrast: thriller, horror, high drama
  neutral      : balanced, professional

== Output ==

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
JSON array — one object per section in the same order received:
[
  {
    "section_order": 0,
    "validation_status": "PASS" | "MINOR" | "MAJOR",
    "visual_source": "pexels" | "unsplash" | "runway",
    "search_query": "refined English query here",
    "effect": "slow_zoom" | "fade_in" | "cut" | "pan" | "zoom_out",
    "color_grade": "desaturated" | "cold_blue" | "warm_amber" | "dark_contrast" | "neutral",
    "issues": ["describe any issue found, or empty list if PASS"]
  }
]\
"""


def validate_sections_with_claude(
    sections: list[dict],
    channel_niche: str,
    channel_tone: str,
) -> list[dict]:
    """Ask Claude to validate and enrich sections with production metadata.

    Args:
        sections:      List of section dicts (from section_splitter).
        channel_niche: Channel niche for context.
        channel_tone:  Channel tone for tone-match checking.

    Returns:
        Validated sections enriched with effect, color_grade, and issues.

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    section_lines = "\n\n".join(
        f"Section {s['section_order']} ({s.get('duration_sec', 0):.1f}s) "
        f"[{s.get('suggested_visual','b-roll')} / {s.get('search_query','')}]:\n"
        f"{s['script_text'][:300]}"
        for s in sections
    )
    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n\n"
        f"Sections to validate:\n\n{section_lines}"
    )

    raw = call_claude(_VALIDATOR_SYSTEM_PROMPT, user_message, max_tokens=2048)
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", raw).strip()
    try:
        results: list[dict] = json.loads(cleaned)
        if not isinstance(results, list):
            raise ValueError(f"Expected JSON array, got {type(results).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Claude returned invalid validation JSON: {exc}") from exc

    return results


# ── Section Splitter — visual enrichment prompt ───────────────────────────────
# Python handles timing; Claude handles creative decisions (search query + visual type).
# > 800 chars → cache_control: ephemeral applied automatically.

_SPLITTER_SYSTEM_PROMPT = """\
You are a visual director for an automated multilingual video production system.

You receive a list of script sections with their timings and narrative text.
For each section you must decide:

1. SEARCH QUERY — a 3-to-5-word English phrase to find a relevant stock image or video.
   Rules:
   - Always write in English, regardless of the script language.
   - Be specific and descriptive (e.g. "abandoned dark hospital hallway" not "hospital").
   - Avoid people's faces unless the section explicitly calls for human presence.
   - Prefer atmospheric, cinematic compositions.
   - Never invent places, people, or events — base the query on the section text only.

2. SUGGESTED VISUAL — the type of visual that fits the section:
   - "b-roll"       : atmospheric footage or photos (most sections)
   - "text_overlay" : black/dark background with text (intro hooks, statistics, key phrases)
   - "action"       : dynamic movement footage (chase scenes, dramatic events)

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
Output format — a JSON array, one object per section, in the same order received:
[
  {
    "section_order": 0,
    "search_query": "...",
    "suggested_visual": "b-roll" | "text_overlay" | "action"
  }
]\
"""


def enrich_sections_with_visuals(sections: list[dict], channel_niche: str, channel_tone: str) -> list[dict]:
    """Ask Claude to add search_query and suggested_visual to each section.

    Receives sections that already have timing (audio_start_ms/audio_end_ms)
    and script_text computed by Python. Claude only decides the visual strategy.

    Args:
        sections:      List of dicts with at least ``section_order`` and ``script_text``.
        channel_niche: Channel niche for context (e.g. "Reddit horror story narration").
        channel_tone:  Channel tone (e.g. "documentary").

    Returns:
        Original sections list enriched with ``search_query`` and ``suggested_visual``.
        Falls back to generic values if Claude fails.

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    section_lines = "\n\n".join(
        f"Section {s['section_order']} ({s.get('duration_sec', 0):.0f}s):\n{s['script_text'][:300]}"
        for s in sections
    )
    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n\n"
        f"Sections to enrich:\n\n{section_lines}"
    )

    raw = call_claude(_SPLITTER_SYSTEM_PROMPT, user_message, max_tokens=1024)

    # Claude should return a JSON array
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", raw).strip()
    try:
        enrichments: list[dict] = json.loads(cleaned)
        if not isinstance(enrichments, list):
            raise ValueError(f"Expected JSON array, got {type(enrichments).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Section enrichment JSON error: %s | raw: %.300s", exc, raw)
        raise ValueError(f"Claude returned invalid enrichment JSON: {exc}") from exc

    # Validate each enrichment entry
    by_order = {e["section_order"]: e for e in enrichments if "section_order" in e}
    for s in sections:
        order = s["section_order"]
        enrichment = by_order.get(order, {})
        s["search_query"]     = enrichment.get("search_query", f"{channel_niche} cinematic")
        s["suggested_visual"] = enrichment.get("suggested_visual", "b-roll")

    return sections
