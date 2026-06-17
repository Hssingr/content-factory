import json
import logging
import re

from app.services.claude_client import (
    call_claude,
    call_claude_structured_with_usage,
    parse_claude_json,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "3.0"  # v3.0: Replaced stock-fetcher / media-scoring infrastructure with
                        #        Flux Schnell image generation. Beat schema: removed stock_query,
                        #        search_query, broad_query, fallback_query, query_style; added
                        #        flux_prompt (rich Flux-optimized generation prompt per beat).
                        # v2.4: score_media_candidates_with_claude migrated to vision-based
                        #        call_claude_structured (forced tool-use + thumbnail image blocks)
                        # v2.3: generate_storyboard_batch migrated to call_claude_structured_with_usage
                        # v2.2: query_style field + 14 banned query patterns

# ── Storyboard Agent prompt ───────────────────────────────────────────────────
# Claude designs the visual storyboard ONE NARRATION SEGMENT AT A TIME —
# one [INTRO]/[SECTION N]/[OUTRO] block per call. Python (storyboard.py) splits
# the narration into segments, runs one batch per segment, merges the results,
# and maps the merged beats onto Whisper timestamps. Flux Schnell then generates
# one image per beat from the flux_prompt Claude wrote.

STORYBOARD_SCHEMA_VERSION = "4.0"  # v4.0: stock_query/search_query/broad_query/fallback_query/query_style
                                   #        removed; flux_prompt added (Flux Schnell generation prompt)
                                   # v3.0: forced tool-use schema (call_claude_structured_with_usage)
                                   # v2.5: query_style field
                                   # v2.4: stock_query + broad_query fields

_STORYBOARD_SYSTEM_PROMPT = """\
You are a visual director and editor for an automated multilingual documentary \
video production system. Think like a human video editor, not a stock-search generator.

You design the storyboard ONE NARRATION SEGMENT AT A TIME — a single [INTRO],
[SECTION N], or [OUTRO] block — never the whole video in one pass. You receive:
which segment this is (its position among the video's narration segments), the
segment's narration text, the channel niche/tone/format, and a short note on the
visual approach used in the immediately preceding segment (for continuity only —
do not repeat it).

Design an ordered sequence of visual beats that carries the viewer through THIS
SEGMENT's narration — and ONLY this segment's narration — from its first word to
its last.

== Principle A: Relevance first ==
Visual diversity must NEVER destroy narrative meaning. Variety is a tool, not a goal.
  - Do not convert night domestic tension into random daylight street footage just to
    vary the environment. The environment shift must be narratively justified.
  - Do not inject an exterior shot purely to hit a diversity quota if the narration
    is describing a private, interior event.
  - When the story takes place in a specific setting, respect that setting visually.
  - Vary WHAT IS IN THE FRAME (subject, object, detail), not just WHERE it is filmed.

== Principle B: Visual progression — every beat must earn its place ==
Every beat must do at least ONE of the following — otherwise rethink it:
  - Reveal new information the viewer hasn't seen yet
  - Change the emotional temperature (from tense to calm, from confusion to clarity)
  - Show new evidence, an object, a document, a location directly referenced in the narration
  - Shift perspective (from wide establishing to close detail, or from subject to reaction)
  - Create deliberate contrast with the previous beat
A beat that simply restates the same visual idea as the previous beat is not a
transition — it is padding. Cut it or replace it.

== Pacing ==
- youtube_long format: place one visual beat every 3–5 seconds of narration.
- short-form formats (youtube_short / tiktok / reels): one beat every 2–4 seconds.
- Never let a single still image hold the screen longer than 6 seconds.

== Anti-slideshow rules ==
A repetitive sequence of similar-looking shots is the #1 reason automated videos feel
fake. You MUST actively design against it:
- NEVER use the same environment TYPE more than twice in a row.
- Every beat must add NEW visual information — a new subject, place, object, or
  meaningfully different angle on the same subject.
- For ABSTRACT narration (ideas, statistics, emotions): use an OBJECT-BASED METAPHOR
  (e.g. "growing distrust" → a single object in focus while background blurs away,
  NOT a generic dark hallway). Never default to a moody empty space.

== Principle D: Avoid lazy dark-atmosphere visuals ==
Use these ONLY when the narration SPECIFICALLY describes them:
  ✗ dark hallway / empty corridor
  ✗ door slowly opening or closing
  ✗ shadow cast on a wall
  ✗ anonymous silhouette in a doorway
  ✗ flickering light or power outage
  ✗ fog or mist as atmosphere placeholder
  ✗ stock "thinking person at a desk"

== Environment diversity rules ==
- No single environment value may appear in more than 35% of beats in this segment.
- "corridor_interior" is the most overused environment. After 2 corridor beats, force
  the next one into an entirely different setting.

== Motif diversity rules ==
- Doorways, corridors, and thresholds must appear at most 4 times total per video, and
  at most 2 times in any 10-beat window.
- No single motif may repeat more than 2 times in any 10-beat window.

== Per-10-beat composition requirement ==
Every window of 10 consecutive beats must contain AT LEAST:
  - 2 object or detail shots (visual_category = "object")
  - 1 document/screen/map shot (visual_category = "document" or "screen")
  - 1 exterior or establishing shot when narratively plausible
  - 1 human/body-language shot (motif = "hands" or "face") when narratively plausible
  - 1 symbolic/emotional shot (visual representing an IDEA)
  - No more than 4 indoor_domestic beats per 10-beat window

== [INTRO] segment — special rules ==
Apply these rules ONLY to the first segment ([INTRO]).

beat_order=0 is the COVER FRAME — the single image a viewer sees before deciding to watch.
It is the most important image in the entire video.

Rules for beat_order=0:
  - Must show the most specific, concrete physical object OR place directly tied to the
    story's central tension. Never a generic atmospheric shot. Never an empty environment.
  - NEVER use color_grade "dark_contrast" for beat_order=0 — the cover frame must be
    clearly visible. Use "neutral", "warm_amber", or "desaturated".
  - MUST use effect "push_in" or "slow_zoom" to create forward momentum into the story.
  - flux_prompt: identify the single most specific physical thing mentioned or implied in
    the first sentence of the narration and build the ENTIRE prompt around that thing.

The [INTRO] segment must visually escalate toward the central tension:
  beat_order=0: the specific story detail that creates immediate curiosity (concrete object or action)
  beat_order=1: establish the location and scale (where does this take place?)
  beat_order=2: the tension object or action the story is actually about
Each successive beat must increase the viewer's sense of "I need to know what happens next."

== Flux image generation prompt rules ==
Each beat requires a ``flux_prompt`` — a photorealistic image generation prompt that
Flux Schnell will use to create the visual for this beat.

THE PRIME DIRECTIVE: the flux_prompt must answer "what exact thing would a camera be
pointing at right now?" — not a mood, not an atmosphere, but a physical subject.

FORBIDDEN words — using any of these in a flux_prompt is an automatic failure:
  atmospheric, cinematic, mysterious, eerie, ominous, dramatic, moody, haunting,
  brooding, foreboding, unsettling, epic, intense, dark (as mood descriptor), ethereal.
  These describe feelings. Flux generates images. Describe what IS IN THE FRAME.

Build every flux_prompt in this exact order (50–80 words total):
  1. SUBJECT — the single most specific concrete element from the narration text:
     a named object, a specific place, a visible action, something the narration mentions.
  2. COMPOSITION — where is the camera? (close-up, wide shot, overhead, eye-level)
  3. SETTING — specific location detail ("a 1980s hospital waiting room with plastic chairs
     and wall-mounted TV" not "a room", "a cobblestone alley in an old European city" not "street")
  4. LIGHTING — exact quality and source (morning side light through venetian blinds,
     fluorescent overhead, golden late-afternoon slant, overcast diffuse, incandescent warm)
  5. TECHNICAL — photorealistic, sharp focus, no motion blur; no people unless motif=face/hands;
     no logos, no text in frame, no brand names.

Color grade integration — the flux_prompt MUST produce a base image compatible with the grade:
  dark_contrast (CSS: contrast 140% + brightness 65%): ALWAYS generate a well-lit,
    bright source image. A dark scene + dark_contrast → pure black in the rendered video.
    If you choose dark_contrast, the prompt must specify good ambient lighting.
  cold_blue: include naturally cool-toned lighting (overcast day, blue hour, cool fluorescents).
  warm_amber: include naturally warm-toned lighting (golden hour, incandescent, candlelight).
  desaturated / neutral: any natural lighting works.

Good examples:
  ✓ "Worn wooden front door with brass knocker, close-up, afternoon suburban street visible
     through frosted glass panel beside door, peeling paint on door frame, low side light
     from setting sun, photorealistic, sharp focus, no people, no text, no logos"
  ✓ "Stack of typed court documents with red CLASSIFIED stamp, close-up overhead shot,
     on government-issue metal desk, brass desk lamp casting warm incandescent light,
     selective focus on stamped seal, shallow depth of field, photorealistic, sharp focus"
  ✓ "Empty 1980s hospital waiting room, rows of orange plastic chairs bolted to beige wall,
     wall-mounted CRT television, fluorescent overhead panels, wide shot, photorealistic,
     sharp focus, no people"

Bad examples (forbidden):
  ✗ "dark hallway with dramatic shadows and mysterious atmosphere" — mood, not subject
  ✗ "cinematic shot of an eerie abandoned building at night" — pure atmosphere
  ✗ "moody documentary-style image" — mood word + no subject

== Per-beat decisions (you make ALL of these — Python only handles timing/generation) ==
1. start_hint / end_hint — copy the exact first 6–10 words and the exact last 6–10
   words of the narration THIS BEAT covers, verbatim from the segment text given to
   you. These are used to locate the beat in the audio — they MUST match word-for-word.
2. visual_intent — one sentence describing what the viewer should see and feel.
2b. why_this_visual — one sentence explaining WHY this specific visual was chosen for
    this moment in the narrative.
3. visual_type — b-roll | action | text_overlay | document | map | screenshot | generated_visual
4. visual_category — person | place | object | document | screen | map | abstract | text
5. environment — fixed SETTING label, choose the closest honest match:
     underwater | indoor_office | indoor_domestic | forest_nature | urban_street |
     corridor_interior | abstract_dark | open_landscape | laboratory | industrial |
     vehicle | other
6. flux_prompt — Flux Schnell image generation prompt (see rules above).
7. effect — slow_zoom | zoom_out | pan | push_in | shake | cut | fade_in | parallax
8. color_grade — desaturated | cold_blue | warm_amber | dark_contrast | neutral
9. transition_to_next — cut | crossfade | dip_to_black | whip_pan | zoom_blur | match_cut | none
10. overlay_text — short on-screen text (a name, date, statistic, key phrase) or ""
11. overlay_position — center | lower_third | top_left | top_right | none
12. motif — dominant visual motif this beat shows:
      doorway | corridor | face | hands | object | clock | phone | photo | exterior |
      text | screen | reflection | document | room | other
13. story_progression_role — narrative function:
      setup | evidence | escalation | contradiction | emotional_reaction |
      context | transition | payoff | comment_prompt

== Hard rules ==
- Never invent names, dates, places, facts, people, URLs, or statistics.
- Do not repeat the same visual idea on consecutive beats.
- start_hint and end_hint must be copied EXACTLY from the segment text, in order.
- Never copy a [INTRO]/[SECTION N]/[OUTRO] marker into start_hint or end_hint.
- Hints must contain no digits — write out any number in words.

Strict rules:
1. Generate ONLY beats for THIS segment. beat_order values must be sequential integers
   starting at 0, in narration order. Aim for the target_beat_count provided (±2).
2. Every beat's start_hint/end_hint must be copied EXACTLY from the segment text you
   received — never paraphrased, and never including the marker label.
3. Every beat must include a ``motif`` field chosen from the allowed list.
4. Every beat must include ``story_progression_role``.\
"""

# Per-batch ceiling — raised to 8192 to accommodate 50-80 word flux_prompts.
# Previous value 6144 caused truncation when every beat's flux_prompt consumed ~200 tokens.
STORYBOARD_BATCH_MAX_TOKENS = 8192

_TRUNCATION_WARNING_RATIO = 0.95

# On truncation: reduce target beat count by this amount and retry once.
# 3 fewer beats × ~200 tokens/beat ≈ 600 tokens freed — sufficient to clear the ceiling.
_TRUNCATION_BEAT_REDUCTION = 3

_BEAT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "beat_order":              {"type": "integer"},
        "start_hint":              {"type": "string", "description": "Exact first 6–10 verbatim words of the narration this beat covers, no digits."},
        "end_hint":                {"type": "string", "description": "Exact last 6–10 verbatim words of the narration this beat covers, no digits."},
        "visual_intent":           {"type": "string"},
        "why_this_visual":         {"type": "string"},
        "visual_type":             {"type": "string", "enum": ["b-roll", "action", "text_overlay", "document", "map", "screenshot", "generated_visual"]},
        "visual_category":         {"type": "string", "enum": ["person", "place", "object", "document", "screen", "map", "abstract", "text"]},
        "environment":             {"type": "string", "enum": ["underwater", "indoor_office", "indoor_domestic", "forest_nature", "urban_street", "corridor_interior", "abstract_dark", "open_landscape", "laboratory", "industrial", "vehicle", "other"]},
        "flux_prompt":             {"type": "string", "description": "Flux image generation prompt: specific physical subject only, no mood words, no faces, no logos, 50-80 words, photorealistic."},
        "effect":                  {"type": "string", "enum": ["slow_zoom", "zoom_out", "pan", "push_in", "shake", "cut", "fade_in", "parallax"]},
        "color_grade":             {"type": "string", "enum": ["desaturated", "cold_blue", "warm_amber", "dark_contrast", "neutral"]},
        "transition_to_next":      {"type": "string", "enum": ["cut", "crossfade", "dip_to_black", "whip_pan", "zoom_blur", "match_cut", "none"]},
        "overlay_text":            {"type": "string"},
        "overlay_position":        {"type": "string", "enum": ["center", "lower_third", "top_left", "top_right", "none"]},
        "motif":                   {"type": "string", "enum": ["doorway", "corridor", "face", "hands", "object", "clock", "phone", "photo", "exterior", "text", "screen", "reflection", "document", "room", "other"]},
        "story_progression_role":  {"type": "string", "enum": ["setup", "evidence", "escalation", "contradiction", "emotional_reaction", "context", "transition", "payoff", "comment_prompt"]},
    },
    "required": [
        "beat_order", "start_hint", "end_hint", "visual_intent", "why_this_visual",
        "visual_type", "visual_category", "environment", "flux_prompt",
        "effect", "color_grade", "transition_to_next",
        "overlay_text", "overlay_position", "motif", "story_progression_role",
    ],
}

_STORYBOARD_BATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "storyboard_status": {"type": "string", "enum": ["APPROVED"]},
        "overall_style":     {"type": "string", "description": "One short phrase describing the visual direction of THIS segment."},
        "beats":             {"type": "array", "items": _BEAT_SCHEMA},
        "global_notes":      {"type": "array", "items": {"type": "string"}},
    },
    "required": ["storyboard_status", "overall_style", "beats", "global_notes"],
}


def generate_storyboard_batch(
    segment_label: str,
    segment_text: str,
    segment_index: int,
    segment_count: int,
    channel,
    script_format: str = "youtube_long",
    previous_segment_summary: str = "",
    target_beat_count: int = 0,
) -> tuple[dict, dict]:
    """Ask Claude to design the storyboard for ONE narration segment only.

    Batched per segment so no single call describes more than ~25 beats.
    Uses forced tool-use (call_claude_structured_with_usage) for guaranteed
    structured output. Each beat's flux_prompt will be used by Flux Schnell
    to generate the actual image — Claude writes the generation prompt here.

    Args:
        segment_label:    Marker label, e.g. ``"[SECTION 2]"``.
        segment_text:     This segment's narration text only.
        segment_index:    1-based position of this segment.
        segment_count:    Total number of narration segments.
        channel:          Channel ORM object (provides niche and tone).
        script_format:    Format key — controls beat-pacing guidance.
        previous_segment_summary: Continuity note from the prior segment.
        target_beat_count: Expected number of beats for this segment.

    Returns:
        ``(storyboard, usage)`` — storyboard has keys ``storyboard_status``,
        ``overall_style``, ``beats``, ``global_notes``; usage is the token dict.

    Raises:
        ValueError: If the tool-use response is malformed.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    def _build_message(beat_count: int) -> str:
        count_line = (
            f"Target beat count for this segment: {beat_count} beats (aim for this count, ±2)\n"
            if beat_count > 0 else ""
        )
        return (
            f"Channel niche: {channel.niche}\n"
            f"Channel tone: {channel.tone}\n"
            f"Script format: {script_format}\n"
            f"Segment: {segment_label} — {segment_index} of {segment_count} narration segments in this video\n"
            + count_line
            + (
                f"Previous segment context (do not open with the same visual approach): "
                f"{previous_segment_summary}\n"
                if previous_segment_summary else ""
            )
            + f"\nNarration for THIS segment only (design beats for this text alone):\n{segment_text}"
        )

    def _run(beat_count: int) -> tuple[dict, dict]:
        return call_claude_structured_with_usage(
            task="storyboard",
            system_prompt=_STORYBOARD_SYSTEM_PROMPT,
            user_message=_build_message(beat_count),
            schema_name="storyboard_batch",
            input_schema=_STORYBOARD_BATCH_SCHEMA,
            max_tokens=STORYBOARD_BATCH_MAX_TOKENS,
        )

    storyboard, usage = _run(target_beat_count)
    output_tokens = usage.get("output_tokens", 0)

    if output_tokens >= STORYBOARD_BATCH_MAX_TOKENS:
        # Truncated — Claude hit max_tokens mid-response; the forced tool-use input
        # will be {} (empty). Retry once with a reduced beat count to free token budget.
        reduced = max(1, target_beat_count - _TRUNCATION_BEAT_REDUCTION)
        logger.warning(
            "Storyboard truncated: segment=%s output_tokens=%d == max_tokens=%d "
            "— retrying with target_beat_count %d → %d",
            segment_label, output_tokens, STORYBOARD_BATCH_MAX_TOKENS,
            target_beat_count, reduced,
        )
        storyboard, usage = _run(reduced)
        output_tokens = usage.get("output_tokens", 0)
        if output_tokens >= STORYBOARD_BATCH_MAX_TOKENS:
            raise ValueError(
                f"Storyboard segment {segment_label!r} hit max_tokens={STORYBOARD_BATCH_MAX_TOKENS} "
                f"even after reducing target_beat_count to {reduced} — "
                "segment narration may be too long; consider splitting"
            )
    elif output_tokens >= STORYBOARD_BATCH_MAX_TOKENS * _TRUNCATION_WARNING_RATIO:
        logger.warning(
            "Storyboard output approaching token limit: segment=%s output_tokens=%d max_tokens=%d (%.0f%%)",
            segment_label, output_tokens, STORYBOARD_BATCH_MAX_TOKENS,
            100 * output_tokens / STORYBOARD_BATCH_MAX_TOKENS,
        )

    for key, expected_type in (
        ("storyboard_status", str), ("overall_style", str),
        ("beats", list), ("global_notes", list),
    ):
        if key not in storyboard:
            raise ValueError(f"storyboard_batch response missing required key '{key}'")
        if not isinstance(storyboard[key], expected_type):
            raise ValueError(
                f"storyboard_batch key '{key}' expected {expected_type.__name__}, "
                f"got {type(storyboard[key]).__name__}"
            )
    return storyboard, usage


# ── Section Validator prompt ──────────────────────────────────────────────────
# Used by the legacy section splitter path (allow_legacy_fallback=True only).

_VALIDATOR_SYSTEM_PROMPT = """\
You are a quality control director for an automated multilingual video production system.

Evaluate each provided section and return a quality assessment with corrections.

== Checks ==

DURATION FIT
  MAJOR if < 3 s  — too short for a meaningful visual
  MAJOR if > 25 s — too long for a single image/clip; suggest how to split
  PASS  if 3–25 s

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
    "visual_source": "pexels" | "runway",
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
    """Validate and enrich legacy sections with production metadata.

    Used only in the legacy section-splitter fallback path
    (``allow_legacy_fallback=True``). The storyboard path does not call this.

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
        f"{s['script_text'][:600]}"
        for s in sections
    )
    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n\n"
        f"Sections to validate:\n\n{section_lines}"
    )

    raw = call_claude(_VALIDATOR_SYSTEM_PROMPT, user_message, max_tokens=2048, task="section_validation")
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", raw).strip()
    try:
        results: list[dict] = json.loads(cleaned)
        if not isinstance(results, list):
            raise ValueError(f"Expected JSON array, got {type(results).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Claude returned invalid validation JSON: {exc}") from exc

    return results


# ── Section Splitter — visual enrichment prompt ───────────────────────────────
# Used only in the legacy section-splitter fallback path.

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
    """Add search_query, suggested_visual, and a basic flux_prompt to each legacy section.

    Used only in the legacy section-splitter fallback path. The storyboard path
    generates flux_prompt directly.

    Args:
        sections:      List of dicts with at least ``section_order`` and ``script_text``.
        channel_niche: Channel niche for context.
        channel_tone:  Channel tone.

    Returns:
        Original sections list enriched with ``search_query``, ``suggested_visual``,
        and a synthesized ``flux_prompt``.

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    section_lines = "\n\n".join(
        f"Section {s['section_order']} ({s.get('duration_sec', 0):.0f}s):\n{s['script_text'][:600]}"
        for s in sections
    )
    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n\n"
        f"Sections to enrich:\n\n{section_lines}"
    )

    raw = call_claude(_SPLITTER_SYSTEM_PROMPT, user_message, max_tokens=1024, task="section_splitting")
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", raw).strip()
    try:
        enrichments: list[dict] = json.loads(cleaned)
        if not isinstance(enrichments, list):
            raise ValueError(f"Expected JSON array, got {type(enrichments).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Section enrichment JSON error: %s | raw: %.300s", exc, raw)
        raise ValueError(f"Claude returned invalid enrichment JSON: {exc}") from exc

    by_order = {e["section_order"]: e for e in enrichments if "section_order" in e}
    for s in sections:
        order = s["section_order"]
        enrichment = by_order.get(order, {})
        sq = enrichment.get("search_query", f"{channel_niche} cinematic")
        s["search_query"]     = sq
        s["suggested_visual"] = enrichment.get("suggested_visual", "b-roll")
        # Synthesize a basic flux_prompt for Flux generation (legacy path only)
        s["flux_prompt"] = (
            f"{sq}, photorealistic, cinematic documentary style, "
            f"desaturated color grade, no people, no text"
        )

    return sections
