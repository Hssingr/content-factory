import json
import logging
import re

from app.services.claude_client import call_claude, parse_claude_json

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.3"  # v1.3: anti-slideshow storyboard rules + visual_category/avoid_reason,
                        # stricter media validation (reject generic dark corridors/repetition),
                        # new Viewer Experience Validator (final pre-render check)

# ── Storyboard Agent prompt ───────────────────────────────────────────────────
# Claude designs the full visual storyboard (creative decisions); Python maps the
# resulting beats onto Whisper timestamps deterministically (storyboard.py).

_STORYBOARD_SYSTEM_PROMPT = """\
You are a visual director and editor for an automated multilingual documentary \
video production system.

You receive the full narration (voice_script) with [INTRO]/[SECTION N]/[OUTRO] \
markers, plus the channel niche, tone, and target format. Design a complete \
storyboard: an ordered sequence of visual beats that carries the viewer through \
the narration from first word to last.

== Pacing ==
- youtube_long format: place one visual beat every 3–5 seconds of narration.
- short-form formats (youtube_short / tiktok / reels): one beat every 2–4 seconds.
- Never let a single still visual hold the screen longer than 6 seconds unless
  there is strong motion (action footage) or on-screen text driving attention.

== Anti-slideshow rules (CRITICAL — this is what separates a documentary from a slideshow) ==
A repetitive sequence of similar-looking shots is the #1 reason automated videos feel
fake. You MUST actively design against it:
- NEVER use the same location TYPE more than twice in a row (e.g. two corridor shots
  back-to-back is the limit — a third corridor/hallway/room beat in a row is forbidden).
- Treat these as OVERUSED defaults — use each at most once per ~6 beats, and never as
  a "safe fallback" when you can't think of something better: dark corridors, empty
  hallways, generic forests, generic offices, anonymous silhouettes, generic close-ups
  of hands typing, stock "thinking person at a desk" shots.
- Serious tone does NOT mean everything must look dark and empty. Vary brightness,
  setting, and subject even within a somber narrative — real documentaries cut between
  archive photos, locations, objects, maps, documents, and people.
- Every beat must add NEW visual information the viewer hasn't just seen — a new subject,
  a new place, a new object, or a meaningfully different angle on the same subject.
- Prefer variety across the whole storyboard: documents, hands, phones, maps, archive-style
  footage, symbolic close-ups (an object that represents the idea), specific locations,
  objects, on-screen text, screens/monitors, and real environments — not just "a person
  walking somewhere".
- For ABSTRACT narration (ideas, statistics, emotions, concepts with no concrete subject):
  use an OBJECT-BASED METAPHOR (e.g. narration about "growing distrust" → a single object
  in focus while the background blurs away, not a random dark hallway). Never default to
  a moody empty space just because the idea is abstract.
- search_query values must NOT repeat the same core subject as the immediately preceding
  beat's search_query — vary the noun, the setting, or the visual category entirely.
- If a beat genuinely cannot be represented well by real stock footage (a very specific
  named fact, a statistic, an internal feeling), use "text_overlay" or "generated_visual"
  instead of forcing a generic, loosely-related b-roll clip onto it.

== Per-beat decisions (you make ALL of these — Python only handles timing/fetching) ==
1. start_hint / end_hint — copy the exact first 6–10 words and the exact last 6–10
   words of the narration segment this beat covers, verbatim from voice_script.
   These are used to locate the beat in the audio — they MUST match word-for-word.
2. visual_intent — one sentence describing what the viewer should see and feel.
3. visual_type — b-roll | action | text_overlay | document | map | screenshot | generated_visual
   Use "generated_visual" only when no stock footage could plausibly exist
   (abstract concepts, specific named individuals, unphotographed private events).
4. visual_category — person | place | object | document | screen | map | abstract | text
   A coarse subject classification used to detect repetition programmatically. Pick the
   category that best matches what's actually ON SCREEN — be honest, not aspirational.
5. avoid_reason — one short phrase explaining why THIS beat is not redundant with the
   beats around it (e.g. "first interior shot after three exterior beats", "introduces
   the central object for the first time", "only document close-up in this section").
6. search_query / fallback_query — specific, cinematic, 4–8 word ENGLISH stock-media
   queries. The fallback must describe a genuinely different visual angle (different
   subject or setting), not a minor rewording of the primary query.
7. effect — slow_zoom | zoom_out | pan | push_in | shake | cut | fade_in | parallax
8. color_grade — desaturated | cold_blue | warm_amber | dark_contrast | neutral
9. transition_to_next — cut | crossfade | dip_to_black | whip_pan | zoom_blur | match_cut | none
10. overlay_text — short on-screen text (a name, date, statistic, key phrase) or ""
    when no overlay is needed.
11. overlay_position — center | lower_third | top_left | top_right | none
12. priority — "essential" (carries the narrative — never drop) or "optional"
    (atmosphere/filler — first candidate to trim if the assembly runs long).

== Hard rules ==
- Never invent names, dates, places, facts, people, URLs, documents, or statistics —
  overlay_text and search queries must be grounded strictly in the narration given.
- Do not repeat the same visual idea (same subject + same framing) on consecutive beats.
- Every beat must be visually concrete — no vague queries like "history" or "mystery".
- start_hint and end_hint must be copied EXACTLY from voice_script, in order, with no
  paraphrasing — they are matched programmatically against the spoken transcript.
- [INTRO]/[SECTION N]/[OUTRO] markers describe narration structure only — never copy
  the bracket markers themselves into start_hint or end_hint.
- Use generated_visual sparingly — prefer stock-searchable visuals whenever plausible.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "storyboard_status": "APPROVED",
  "overall_style": "one short phrase describing the visual direction of this video",
  "beats": [
    {
      "beat_order": 0,
      "section_marker": "[INTRO]",
      "start_hint": "exact first 6-10 words copied from voice_script",
      "end_hint": "exact last 6-10 words copied from voice_script",
      "duration_target_sec": 4,
      "visual_intent": "...",
      "visual_type": "b-roll",
      "visual_category": "place",
      "avoid_reason": "why this beat is not redundant with its neighbours",
      "search_query": "...",
      "fallback_query": "...",
      "effect": "slow_zoom",
      "color_grade": "desaturated",
      "transition_to_next": "crossfade",
      "overlay_text": "",
      "overlay_position": "none",
      "priority": "essential",
      "reason": "why this visual beat supports the narration"
    }
  ],
  "global_notes": ["one-sentence note about pacing or visual strategy, or an empty list"]
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. beat_order values must be sequential integers starting at 0, in narration order,
   covering the ENTIRE voice_script from the first word to the last.
3. Every beat's start_hint/end_hint must be copied from the SAME voice_script you received.\
"""


def generate_storyboard(voice_script: str, channel, script_format: str = "youtube_long") -> dict:
    """Ask Claude to design a complete visual storyboard for the narration.

    Claude makes every creative decision (visual intent, type, search queries,
    effects, color grades, transitions, overlays). Python only maps the resulting
    beats onto real audio timestamps (see ``storyboard.py``).

    Args:
        voice_script:  Full narrator text including [INTRO]/[SECTION N]/[OUTRO] markers.
        channel:       Channel ORM object (provides niche and tone for context).
        script_format: Format key — controls the beat-pacing guidance in the prompt.

    Returns:
        Dict with keys ``storyboard_status``, ``overall_style``, ``beats``, ``global_notes``.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Voice script:\n{voice_script}"
    )
    raw = call_claude(_STORYBOARD_SYSTEM_PROMPT, user_message, max_tokens=8192)
    return parse_claude_json(
        raw,
        required_keys=["storyboard_status", "overall_style", "beats", "global_notes"],
        type_checks={"storyboard_status": str, "overall_style": str, "beats": list, "global_notes": list},
    )


# ── Media Validation Agent prompt ─────────────────────────────────────────────

_MEDIA_VALIDATION_SYSTEM_PROMPT = """\
You are a media supervisor for an automated multilingual documentary video \
production system. Your job is to catch the exact failure mode that makes automated \
videos feel fake: generic, repetitive, mood-only stock footage that technically loosely \
relates to the topic but does not actually help the viewer understand what's being said.

You receive a list of storyboard beats IN ORDER. Each beat carries the visual intent the \
director planned, the narration it covers, the media that was actually fetched (URL, type, \
thumbnail, title/description/tags), and — for context — a short summary of the PREVIOUS \
and NEXT beat's intent, query, and media source. Decide, beat by beat, whether the fetched \
media actually serves the viewer.

Decisions:
  KEEP    — media is specific, relevant to the MEANING (not just the mood) of this beat,
            and visually distinct from its near neighbours.
  REPLACE — media fails any rejection rule below, or is generic/repetitive/misleading.
            → provide replacement_search_query: a sharper, more specific, more concrete query.
  ADJUST  — media itself is genuinely usable but needs better presentation only.
            → revise effect / color_grade / transition_to_next / overlay_text only.

== Rejection rules — REPLACE the media if any of these are true ==
1. Generic dark/empty space: the media shows a dark corridor, empty hallway, tunnel,
   hospital, or generic unlit interior — UNLESS the narration for THIS beat specifically
   names or describes a corridor, tunnel, hospital, building interior, or darkness.
   A "serious" tone is not sufficient justification for a dark generic space.
2. Repetition: the media's subject is the same as (or near-identical to) the subject
   shown in the previous beat or the next beat — even if each one is technically
   "relevant" on its own, showing the same kind of shot back-to-back reads as a slideshow.
   Mark REPLACE on the LATER of the two beats.
3. Mood-only match: the media matches the emotional tone (e.g. "looks somber") but does
   NOT depict anything related to what the narration is actually describing — the viewer
   gains no understanding from looking at it.
4. Non-comprehension: an average viewer glancing at the screen would not connect this
   image to what the narrator is saying at that moment.
5. Generic stock cliché: anonymous silhouettes, generic "person typing on laptop",
   generic "thinking person at desk", stock handshake/meeting footage used as filler
   with no specific connection to the narration.

When in doubt: a beat with NO media (left for text_overlay or a generated_visual instead)
is better than one with confidently-wrong stock media. Prefer recommending REPLACE with a
more specific query — or, if nothing could plausibly match, note that in "issue" so the
orchestrator can fall back to text_overlay.

Rules:
- Do not approve weak media just because something was fetched.
- Replacement queries must be concrete and specific (name the actual subject/object/place
  implied by the narration, not a mood word), 4–8 ENGLISH words, realistically searchable
  on stock platforms (Pexels/Unsplash) — never invent URLs.
- Favour a professional documentary rhythm: flag chaotic over-editing AND monotonous
  repetition equally — both break immersion.
- Only choose ADJUST when the media is genuinely specific and relevant — never patch a
  wrong or generic clip with effects or overlays instead of replacing it.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "validation_status": "APPROVED" | "NEEDS_CHANGES",
  "beat_reviews": [
    {
      "beat_order": 0,
      "decision": "KEEP" | "REPLACE" | "ADJUST",
      "media_ok": true,
      "issue": "short explanation naming the specific rejection rule, or empty string if none",
      "replacement_search_query": "new 4-8 word English query — only if REPLACE",
      "effect": "slow_zoom | zoom_out | pan | push_in | shake | cut | fade_in | parallax",
      "color_grade": "desaturated | cold_blue | warm_amber | dark_contrast | neutral",
      "transition_to_next": "cut | crossfade | dip_to_black | whip_pan | zoom_blur | match_cut | none",
      "overlay_text": "revised overlay text, or empty string"
    }
  ],
  "overall_comment": "one-sentence summary"
}\
"""


def validate_media_with_claude(
    beats_with_media: list[dict],
    channel_niche: str,
    channel_tone: str,
    script_format: str,
) -> dict:
    """Ask Claude to review fetched media against each storyboard beat's intent.

    Args:
        beats_with_media: Beats enriched with fetched media (media_url, media_type,
                          media_thumb, media_source, ...).
        channel_niche:    Channel niche for context.
        channel_tone:     Channel tone for context.
        script_format:    Format key — informs pacing/rhythm expectations.

    Returns:
        Dict with keys ``validation_status``, ``beat_reviews``, ``overall_comment``.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    max_tokens = 8192 if len(beats_with_media) > 25 else 4096

    def _neighbour_summary(b: dict | None) -> str:
        if b is None:
            return "(none — edge of storyboard)"
        return (
            f"intent={b.get('visual_intent', '')[:100]!r} "
            f"query={b.get('search_query', '')!r} "
            f"source={b.get('media_source', '?')}"
        )

    beat_lines_parts = []
    for i, b in enumerate(beats_with_media):
        prev_b = beats_with_media[i - 1] if i > 0 else None
        next_b = beats_with_media[i + 1] if i + 1 < len(beats_with_media) else None
        beat_lines_parts.append(
            f"Beat {b.get('beat_order', b.get('section_order', 0))} "
            f"({b.get('duration_sec', 0):.1f}s) "
            f"[{b.get('visual_type', 'b-roll')} / {b.get('effect', '?')} / {b.get('color_grade', '?')}]:\n"
            f"  Intent: {b.get('visual_intent', '')[:200]}\n"
            f"  Query:  {b.get('search_query', '')}\n"
            f"  Media:  {b.get('media_source', '?')} {b.get('media_type', '?')} — {b.get('media_url', '')[:80]}\n"
            f"  Thumb:  {b.get('media_thumb', '')[:80]}\n"
            f"  Overlay: {b.get('overlay_text', '') or '(none)'} [{b.get('overlay_position', 'none')}]\n"
            f"  Previous beat: {_neighbour_summary(prev_b)}\n"
            f"  Next beat:     {_neighbour_summary(next_b)}"
        )
    beat_lines = "\n\n".join(beat_lines_parts)

    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n"
        f"Script format: {script_format}\n\n"
        f"Beats:\n\n{beat_lines}"
    )

    raw = call_claude(_MEDIA_VALIDATION_SYSTEM_PROMPT, user_message, max_tokens=max_tokens)
    return parse_claude_json(
        raw,
        required_keys=["validation_status", "beat_reviews", "overall_comment"],
        type_checks={"validation_status": str, "beat_reviews": list, "overall_comment": str},
    )


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
  - Storyboard coverage: does the sequence of beats carry the narration from
    start to finish with no visual gaps or orphaned stretches?
  - Flow & transitions: do the chosen transition_to_next values feel natural
    between consecutive sections, or is the editing chaotic / monotonous?
  - Pacing: are section durations varied (monotonous = flag it)?
  - Duration drift: does sum of section durations match expected total within ±2%?
  - Effect coherence: do the chosen camera effects feel intentional together,
    or is there a jarring mix (e.g. constant shake next to slow_zoom)?
  - Color coherence: do color grades feel cohesive across the video, or do they
    clash from one section to the next without narrative reason?
  - Overlay usefulness: are overlay_text/overlay_position choices adding value,
    or cluttering / repeating information already in the narration?
  - Repeated visuals: flag consecutive or near-consecutive sections that show
    the same subject + framing (visually redundant).
  - Static-feeling sections: flag sections that will feel static on screen
    (long still image, no motion, no overlay, no strong effect).

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
    return parse_claude_json(
        raw,
        required_keys=["assembly_status", "section_reviews", "assembly_issues", "overall_comment"],
        type_checks={
            "assembly_status": str,
            "section_reviews": list,
            "assembly_issues": list,
            "overall_comment": str,
        },
    )


# ── Viewer Experience Validator prompt ────────────────────────────────────────
# Final pre-render check, run once the full plan (sections + shorts + captions)
# is assembled. Reviews the plan the way a viewer would experience it — distinct
# from the Assembly Validator (media relevance / technical flow) and the Media
# Validator (per-beat fetched-media relevance).

_VIEWER_EXPERIENCE_SYSTEM_PROMPT = """\
You are a YouTube viewer experience auditor for an automated multilingual documentary \
video production system. You review the FINAL video plan — right before it is rendered — \
the way a real viewer would experience it on screen, start to finish.

You receive a structured summary of the entire plan: the script's opening, the full
sequence of visual beats (intent, category, query, media source), the Shorts split,
caption statistics, and audio settings. Judge it holistically — not beat by beat.

Answer these questions, in order, as if you were about to publish this video to a real
audience and your name was on it:
  1. intro    — Is the opening strong? Would a real viewer keep watching past 10 seconds,
                or does it feel weak, vague, or like an AI-generated summary?
  2. script   — Does the narration feel human and compelling, or robotic and generic?
  3. visuals  — Are the visuals varied and meaningful, or repetitive / slideshow-like /
                generic (same location types, same subjects, mood-only matches)?
  4. captions — Based on the caption stats (count, average words per caption, target
                word range), would captions read as clean phrases or broken fragments?
  5. audio    — Based on the audio/pacing summary, does narration pacing sound natural
                for a documentary, or flat/monotonous/robotic?
  6. overall  — Would this feel like a professional YouTube documentary, or like an
                automated slideshow with narration over it?

Use FIXED, repeatable criteria — the same plan must always receive the same verdict.
Be specific and harsh where it matters (intro and visual repetition are the most common
failure points) — do not rubber-stamp a mediocre plan as APPROVED.

Decision rule:
  - status = "APPROVED" only if the plan would plausibly feel like a genuine, professional
    YouTube documentary to a real viewer — not flawless, just genuinely good.
  - status = "NEEDS_FIXES" if there is at least one blocking issue that would make a
    viewer feel the video is automated, repetitive, or not worth watching.

For each blocking issue, report:
  - category: one of "intro", "script", "visuals", "captions", "audio", "pacing"
  - issue: the specific problem, concrete and actionable
  - fix: what should change to fix it (deterministic where possible — e.g. "replace beats
    7-9 which all show dark interiors with varied subjects"; "shorten captions exceeding
    12 words"; "rewrite the first two sentences of the intro to lead with a concrete fact")

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "status": "APPROVED" | "NEEDS_FIXES",
  "blocking_issues": [
    {"category": "intro" | "script" | "visuals" | "captions" | "audio" | "pacing", "issue": "...", "fix": "..."}
  ],
  "overall_comment": "one or two sentence summary of the viewer experience"
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. If the plan genuinely feels professional, return an empty blocking_issues array.
3. Do not invent facts about the content beyond what the summary describes.\
"""


def assess_viewer_experience(
    sections: list[dict],
    shorts_count: int,
    caption_count: int,
    avg_caption_words: float,
    total_duration_ms: int,
    channel_niche: str,
    channel_tone: str,
    channel_style: str,
    script_hook: str,
) -> dict:
    """Ask Claude to review the final video plan from a viewer's perspective.

    Runs once the full plan (sections, Shorts, captions) is assembled — the last
    check before Remotion rendering. Distinct from the Assembly Validator (media
    relevance / technical flow) and Media Validator (per-beat fetched-media fit):
    this looks at the plan holistically, the way a real viewer would experience it.

    Args:
        sections:          Fully enriched, validated sections (post assembly validation).
        shorts_count:      Number of Shorts produced by the Shorts Cutter.
        caption_count:     Number of standard captions generated.
        avg_caption_words: Average word count per standard caption.
        total_duration_ms: Expected total audio duration.
        channel_niche:     Channel niche for context.
        channel_tone:      Channel tone for context.
        channel_style:     Video style (e.g. "documentary").
        script_hook:       First ~300 characters of the narration (the intro).

    Returns:
        Dict with ``status`` ("APPROVED" | "NEEDS_FIXES"), ``blocking_issues``
        (list of ``{"category", "issue", "fix"}``), and ``overall_comment``.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    beat_lines = "\n".join(
        f"  Beat {s.get('beat_order', s.get('section_order', 0))} "
        f"({s.get('duration_sec', 0):.1f}s) "
        f"[{s.get('visual_category', s.get('visual_type', '?'))}]: "
        f"{s.get('visual_intent', '')[:120]} — query={s.get('search_query', '')!r} "
        f"source={s.get('media_source', '?')}"
        for s in sections
    )
    expected_sec = total_duration_ms / 1000

    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n"
        f"Channel style: {channel_style}\n\n"
        f"Script opening (intro/hook):\n{script_hook}\n\n"
        f"Total duration: {expected_sec:.1f}s | Beats: {len(sections)} | Shorts: {shorts_count}\n"
        f"Captions: {caption_count} (avg {avg_caption_words:.1f} words/caption — "
        f"target ~6-10 for clean readable phrases)\n\n"
        f"Visual beat sequence:\n{beat_lines}"
    )

    raw = call_claude(_VIEWER_EXPERIENCE_SYSTEM_PROMPT, user_message, max_tokens=2048)
    return parse_claude_json(
        raw,
        required_keys=["status", "blocking_issues", "overall_comment"],
        type_checks={"status": str, "blocking_issues": list, "overall_comment": str},
    )


# ── Section Validator prompt ──────────────────────────────────────────────────

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
        f"{s['script_text'][:600]}"
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
        f"Section {s['section_order']} ({s.get('duration_sec', 0):.0f}s):\n{s['script_text'][:600]}"
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
