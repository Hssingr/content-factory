import json
import logging
import re

from app.services.claude_client import call_claude, call_claude_with_usage, parse_claude_json

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.5"  # v1.5: motif field + doorway diversity rules + batched media validation
                        # + high-level assembly validator + strict_quality_gate wiring
                        # in one call — fixes the structural max_tokens overflow (90-120 beats
                        # needed ~20-27k tokens, only 8192 were available); reduced beat schema
                        # (dropped reason/avoid_reason/section_marker/priority/duration_target_sec
                        # — all confirmed write-only by downstream grep) and added `environment`
                        # for repetition detection across lexically-different-but-visually-same
                        # queries (e.g. "underwater cavern" vs "bioluminescent cave")

# ── Storyboard Agent prompt ───────────────────────────────────────────────────
# Claude designs the visual storyboard (creative decisions) ONE NARRATION SEGMENT
# AT A TIME — one [INTRO]/[SECTION N]/[OUTRO] block per call — so no single response
# ever has to describe more beats than fit comfortably inside max_tokens. Python
# (storyboard.py) splits the narration into segments, runs one batch per segment,
# merges the results in order, and maps the merged beats onto Whisper timestamps.

STORYBOARD_SCHEMA_VERSION = "2.1"  # v2.1: added `motif` field + doorway/threshold diversity rules

_STORYBOARD_SYSTEM_PROMPT = """\
You are a visual director and editor for an automated multilingual documentary \
video production system.

You design the storyboard ONE NARRATION SEGMENT AT A TIME — a single [INTRO],
[SECTION N], or [OUTRO] block — never the whole video in one pass. You receive:
which segment this is (its position among the video's narration segments), the
segment's narration text, the channel niche/tone/format, and a short note on the
visual approach used in the immediately preceding segment (for continuity only —
do not repeat it).

Design an ordered sequence of visual beats that carries the viewer through THIS
SEGMENT's narration — and ONLY this segment's narration — from its first word to
its last.

== Pacing ==
- youtube_long format: place one visual beat every 3–5 seconds of narration.
- short-form formats (youtube_short / tiktok / reels): one beat every 2–4 seconds.
- Never let a single still visual hold the screen longer than 6 seconds unless
  there is strong motion (action footage) or on-screen text driving attention.

== Anti-slideshow rules (CRITICAL — this is what separates a documentary from a slideshow) ==
A repetitive sequence of similar-looking shots is the #1 reason automated videos feel
fake. You MUST actively design against it — including across the segment boundary:
- NEVER use the same location TYPE more than twice in a row (e.g. two corridor shots
  back-to-back is the limit — a third corridor/hallway/room beat in a row is forbidden).
- If the previous segment's note mentions an environment or visual_type, do NOT open
  this segment with the same one — start on something visually different, then vary further.
- Treat these as OVERUSED defaults — use each at most once per ~6 beats, and never as
  a "safe fallback" when you can't think of something better: dark corridors, empty
  hallways, generic forests, generic offices, anonymous silhouettes, generic close-ups
  of hands typing, stock "thinking person at a desk" shots.
- Serious tone does NOT mean everything must look dark and empty. Vary brightness,
  setting, and subject even within a somber narrative — real documentaries cut between
  archive photos, locations, objects, maps, documents, and people.
- Every beat must add NEW visual information the viewer hasn't just seen — a new subject,
  a new place, a new object, or a meaningfully different angle on the same subject.
- Prefer variety across the segment: documents, hands, phones, maps, archive-style
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

== Motif diversity rules (enforced automatically — Python will force-replace excess motifs) ==
- Doorways, corridors, and thresholds ("passage" motifs) are the most overused single
  visual trope in automated video. Combined, they must appear at most 4 times total per
  video, and at most 2 times in any 10-beat window. A dark hallway is not a substitute
  for a visual idea.
- No single motif may repeat more than 2 times in any 10-beat window. For example:
  showing 3 different "face" shots or 3 "clock" shots within 10 beats will be flagged.
- Horror or suspense can use passage motifs sparingly, but the genre does NOT justify a
  majority of beats being doors/corridors — vary with objects, faces, exteriors, documents,
  environments, screens, and reactions.
- Use the ``motif`` field (see below) to self-regulate: if you notice your segment is heavy
  on one motif, force the next beat onto a different one before the previous segment's
  continuity note forces you to.

== Per-beat decisions (you make ALL of these — Python only handles timing/fetching) ==
1. start_hint / end_hint — copy the exact first 6–10 words and the exact last 6–10
   words of the narration THIS BEAT covers, verbatim from the segment text given to
   you. These are used to locate the beat in the audio — they MUST match word-for-word.
2. visual_intent — one sentence describing what the viewer should see and feel.
3. visual_type — b-roll | action | text_overlay | document | map | screenshot | generated_visual
   Use "generated_visual" only when no stock footage could plausibly exist
   (abstract concepts, specific named individuals, unphotographed private events).
4. visual_category — person | place | object | document | screen | map | abstract | text
   A coarse subject classification used to detect repetition programmatically. Pick the
   category that best matches what's actually ON SCREEN — be honest, not aspirational.
5. environment — a fixed SETTING label used to detect repetition across the whole video,
   even when search queries use different words for the same kind of place. Choose the
   closest honest match:
     underwater | indoor_office | indoor_domestic | forest_nature | urban_street |
     corridor_interior | abstract_dark | open_landscape | laboratory | industrial |
     vehicle | other
   IMPORTANT: judge by what the shot would actually LOOK like on screen, not by the
   words in your search query — "deep ocean abyss", "underwater cavern", "submarine
   darkness", and "bioluminescent cave" must ALL receive "underwater" because a viewer
   would perceive them as the same environment, even though the queries differ.
6. search_query / fallback_query — specific, cinematic, 4–8 word ENGLISH stock-media
   queries. The fallback must describe a genuinely different visual angle (different
   subject or setting), not a minor rewording of the primary query.
7. effect — slow_zoom | zoom_out | pan | push_in | shake | cut | fade_in | parallax
8. color_grade — desaturated | cold_blue | warm_amber | dark_contrast | neutral
9. transition_to_next — cut | crossfade | dip_to_black | whip_pan | zoom_blur | match_cut | none
10. overlay_text — short on-screen text (a name, date, statistic, key phrase) or ""
    when no overlay is needed.
11. overlay_position — center | lower_third | top_left | top_right | none
12. motif — the single dominant visual motif this beat would show to a viewer. Choose
    the most honest match from:
      doorway | corridor | face | hands | object | clock | phone | photo | exterior |
      text | screen | reflection | document | room | other
    Use "other" when no listed motif applies. Be honest — this is used by the repetition
    detector: if you label everything "other" to avoid the passage cap, Python will still
    catch overuse via the ``environment`` and ``search_query`` checks.

== Hard rules ==
- Never invent names, dates, places, facts, people, URLs, documents, or statistics —
  overlay_text and search queries must be grounded strictly in the narration given.
- Do not repeat the same visual idea (same subject + same framing) on consecutive beats.
- Every beat must be visually concrete — no vague queries like "history" or "mystery".
- start_hint and end_hint must be copied EXACTLY from the segment text, in order, with
  no paraphrasing — they are matched programmatically against the spoken transcript.
- Never copy a [INTRO]/[SECTION N]/[OUTRO] marker itself into start_hint or end_hint.
- Use generated_visual sparingly — prefer stock-searchable visuals whenever plausible.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "storyboard_status": "APPROVED",
  "overall_style": "one short phrase describing the visual direction of THIS segment",
  "beats": [
    {
      "beat_order": 0,
      "start_hint": "exact first 6-10 words copied from this segment's narration",
      "end_hint": "exact last 6-10 words copied from this segment's narration",
      "visual_intent": "...",
      "visual_type": "b-roll",
      "visual_category": "place",
      "environment": "underwater",
      "search_query": "...",
      "fallback_query": "...",
      "effect": "slow_zoom",
      "color_grade": "desaturated",
      "transition_to_next": "crossfade",
      "overlay_text": "",
      "overlay_position": "none",
      "motif": "object"
    }
  ],
  "global_notes": ["one-sentence note about this segment's pacing or visual strategy, or an empty list"]
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. beat_order values must be sequential integers starting at 0, in narration order,
   covering THIS SEGMENT's narration ONLY — from its first word to its last word.
3. Every beat's start_hint/end_hint must be copied from the SAME segment text you received
   — never from another segment, and never including the [INTRO]/[SECTION N]/[OUTRO] label.
4. Every beat must include a ``motif`` field chosen from the allowed list.
5. In any 10-beat run in this segment, do not use the same non-"other" motif more than
   twice, and never use doorway+corridor+threshold combined more than 4 times total.\
"""

# Per-batch ceiling. A single segment of a youtube_long video is at most ~250 words
# (~100s of narration) → ~25 beats at the densest pacing (1 per 4s). At the reduced
# 13-field schema (~617 chars/beat ≈ 154 tokens/beat) that's ~3,850 tokens of beats
# plus wrapper overhead — comfortably inside this ceiling with ~35% headroom, while
# staying far below the 8192 limit that the old whole-video call structurally exceeded.
STORYBOARD_BATCH_MAX_TOKENS = 6144

# Truncation-detection thresholds (Task 4/5 of the BLOCKER fix set) — applied to
# every batch call so a future pacing/schema change that re-introduces overflow is
# caught immediately in logs instead of silently degrading into parse failures.
_TRUNCATION_WARNING_RATIO = 0.95


def generate_storyboard_batch(
    segment_label: str,
    segment_text: str,
    segment_index: int,
    segment_count: int,
    channel,
    script_format: str = "youtube_long",
    previous_segment_summary: str = "",
) -> tuple[dict, dict]:
    """Ask Claude to design the storyboard for ONE narration segment only.

    This is the batched replacement for the old whole-video ``generate_storyboard``
    call — Python (``storyboard.split_into_beats``) splits the narration into
    [INTRO]/[SECTION N]/[OUTRO] segments and calls this once per segment, so no
    single response ever needs to describe more than ~25 beats.

    Args:
        segment_label:    Marker label for this segment, e.g. ``"[SECTION 2]"``.
        segment_text:     This segment's narration text only (verbatim substring
                          of the full voice_script, markers already stripped).
        segment_index:    1-based position of this segment among all segments.
        segment_count:    Total number of narration segments in this video.
        channel:          Channel ORM object (provides niche and tone for context).
        script_format:    Format key — controls the beat-pacing guidance in the prompt.
        previous_segment_summary: Short note on the prior segment's closing visual
                          choices, for continuity (empty string for the first segment).

    Returns:
        ``(storyboard, usage)`` — ``storyboard`` has keys ``storyboard_status``,
        ``overall_style``, ``beats``, ``global_notes``; ``usage`` is the token-usage
        dict from ``call_claude_with_usage`` (``input_tokens``, ``output_tokens``,
        ``cache_read_input_tokens``).

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n"
        f"Segment: {segment_label} — {segment_index} of {segment_count} narration segments in this video\n"
        + (
            f"Previous segment ended with this visual approach (do not repeat it — "
            f"open differently): {previous_segment_summary}\n"
            if previous_segment_summary else ""
        )
        + f"\nNarration for THIS segment only (design beats for this text alone):\n{segment_text}"
    )
    raw, usage = call_claude_with_usage(
        _STORYBOARD_SYSTEM_PROMPT, user_message, max_tokens=STORYBOARD_BATCH_MAX_TOKENS
    )

    output_tokens = usage.get("output_tokens", 0)
    if output_tokens >= STORYBOARD_BATCH_MAX_TOKENS:
        logger.warning(
            "Storyboard likely truncated: segment=%s output_tokens=%d max_tokens=%d",
            segment_label, output_tokens, STORYBOARD_BATCH_MAX_TOKENS,
        )
    elif output_tokens >= STORYBOARD_BATCH_MAX_TOKENS * _TRUNCATION_WARNING_RATIO:
        logger.warning(
            "Storyboard output approaching token limit: segment=%s output_tokens=%d max_tokens=%d (%.0f%%)",
            segment_label, output_tokens, STORYBOARD_BATCH_MAX_TOKENS,
            100 * output_tokens / STORYBOARD_BATCH_MAX_TOKENS,
        )

    storyboard = parse_claude_json(
        raw,
        required_keys=["storyboard_status", "overall_style", "beats", "global_notes"],
        type_checks={"storyboard_status": str, "overall_style": str, "beats": list, "global_notes": list},
    )
    return storyboard, usage


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


def validate_media_with_claude_batched(
    beats_with_media: list[dict],
    channel_niche: str,
    channel_tone: str,
    script_format: str,
    batch_size: int = 8,
    context_size: int = 2,
    target_indices: list[int] | None = None,
) -> dict:
    """Validate fetched media in small batches to avoid truncation on long videos.

    Splits the target beat indices into batches of ``batch_size`` beats. Each
    batch also receives ``context_size`` flanking beats from the FULL beat list
    (not reviewed, used only for the neighbour-repetition check).

    When ``target_indices`` is ``None`` (default), all beats are validated —
    same behaviour as before. When ``target_indices`` is provided, only those
    positions are sent for review; surrounding beats in ``beats_with_media``
    still appear as context so Claude can check visual repetition with neighbours.

    Args:
        beats_with_media: Full beat list enriched with fetched media.
        channel_niche:    Channel niche for context.
        channel_tone:     Channel tone for context.
        script_format:    Format key — informs pacing/rhythm expectations.
        batch_size:       Beats per Claude call.
        context_size:     Flanking beats included as context (not reviewed).
        target_indices:   Positions within ``beats_with_media`` to validate.
                          ``None`` means validate all.

    Returns:
        Merged dict: ``{validation_status, beat_reviews, overall_comment}``.
        ``beat_reviews`` covers only the target beats.

    Raises:
        ValueError: If all batches fail (logged individually — last exception re-raised).
    """
    n = len(beats_with_media)
    if n == 0:
        return {"validation_status": "APPROVED", "beat_reviews": [], "overall_comment": "No beats"}

    # Determine which list positions to validate
    if target_indices is None:
        sorted_targets = list(range(n))
    else:
        sorted_targets = sorted(set(i for i in target_indices if 0 <= i < n))

    if not sorted_targets:
        return {"validation_status": "APPROVED", "beat_reviews": [], "overall_comment": "No target beats"}

    # Group targets into batches
    batches = [
        sorted_targets[i:i + batch_size]
        for i in range(0, len(sorted_targets), batch_size)
    ]
    total_batches  = len(batches)
    all_reviews: dict[int, dict] = {}
    batch_statuses: list[str] = []
    comments: list[str] = []
    last_exc: Exception | None = None

    for batch_num, batch_indices in enumerate(batches, start=1):
        batch_beats = [beats_with_media[i] for i in batch_indices]
        ctx_before  = beats_with_media[max(0, batch_indices[0] - context_size):batch_indices[0]]
        ctx_after   = beats_with_media[batch_indices[-1] + 1 : batch_indices[-1] + 1 + context_size]
        label       = f"{batch_num}/{total_batches}"

        try:
            result = _validate_media_batch(
                batch_beats, ctx_before, ctx_after,
                channel_niche, channel_tone, script_format, label,
            )
        except Exception as exc:
            logger.error(
                "Media validation batch %s failed: %s — keeping current media for indices %s",
                label, exc, batch_indices,
            )
            last_exc = exc
            continue

        for review in result.get("beat_reviews", []):
            order = review.get("beat_order")
            if isinstance(order, int) and order not in all_reviews:
                all_reviews[order] = review

        batch_statuses.append(result.get("validation_status", "APPROVED"))
        comment = result.get("overall_comment", "")
        if comment:
            comments.append(f"batch {batch_num}: {comment}")

    if not all_reviews and last_exc is not None:
        raise last_exc

    merged_status  = "NEEDS_CHANGES" if "NEEDS_CHANGES" in batch_statuses else "APPROVED"
    merged_reviews = [all_reviews[k] for k in sorted(all_reviews)]
    logger.info(
        "Media validation batched: %d target beats / %d batches (pool=%d) — status=%s reviews=%d",
        len(sorted_targets), total_batches, n, merged_status, len(merged_reviews),
    )
    return {
        "validation_status": merged_status,
        "beat_reviews":      merged_reviews,
        "overall_comment":   "; ".join(comments) if comments else "All batches approved",
    }


def _validate_media_batch(
    beats: list[dict],
    ctx_before: list[dict],
    ctx_after: list[dict],
    channel_niche: str,
    channel_tone: str,
    script_format: str,
    batch_label: str,
) -> dict:
    """Run one media validation Claude call for a slice of beats.

    Reduces per-beat payload: full URLs are replaced by their last 40 characters
    so the context stays compact while still providing enough identity for
    Claude's repetition check.
    """
    def _url_tail(url: str) -> str:
        return url[-40:] if isinstance(url, str) and url else "(none)"

    def _beat_line(b: dict, is_context: bool = False) -> str:
        prefix = "[context] " if is_context else ""
        return (
            f"{prefix}Beat {b.get('beat_order', b.get('section_order', 0))} "
            f"({b.get('duration_sec', 0):.1f}s) "
            f"[{b.get('visual_type', 'b-roll')}/{b.get('visual_category', '?')}"
            f"/{b.get('environment', '?')}]:\n"
            f"  Intent: {b.get('visual_intent', '')[:150]}\n"
            f"  Query:  {b.get('search_query', '')}\n"
            f"  Media:  {b.get('media_source', '?')} {b.get('media_type', '?')} "
            f"…{_url_tail(b.get('media_url', ''))}\n"
            f"  Overlay: {b.get('overlay_text', '') or '(none)'}"
        )

    lines: list[str] = []
    if ctx_before:
        lines.append("Context beats (preceding — neighbour check only, do not review):")
        lines.extend(_beat_line(b, is_context=True) for b in ctx_before)
        lines.append("")

    lines.append(f"Beats to review (batch {batch_label}):")
    lines.extend(_beat_line(b) for b in beats)

    if ctx_after:
        lines.append("")
        lines.append("Context beats (following — neighbour check only, do not review):")
        lines.extend(_beat_line(b, is_context=True) for b in ctx_after)

    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel tone: {channel_tone}\n"
        f"Script format: {script_format}\n\n"
        + "\n".join(lines)
    )

    est_tokens = len(user_message) // 4
    logger.debug(
        "Media validation batch %s: %d beats, ~%d estimated prompt tokens",
        batch_label, len(beats), est_tokens,
    )

    for attempt in range(1, 3):   # one retry on JSON parse failure
        try:
            raw    = call_claude(_MEDIA_VALIDATION_SYSTEM_PROMPT, user_message, max_tokens=2048)
            result = parse_claude_json(
                raw,
                required_keys=["validation_status", "beat_reviews", "overall_comment"],
                type_checks={"validation_status": str, "beat_reviews": list, "overall_comment": str},
            )
            out_est = len(raw) // 4
            if out_est >= int(2048 * 0.95):
                logger.warning(
                    "Media validation batch %s output near token limit (~%d/2048 tokens)",
                    batch_label, out_est,
                )
            logger.info(
                "Media validation batch %s: status=%s reviews=%d",
                batch_label, result.get("validation_status"), len(result.get("beat_reviews", [])),
            )
            return result
        except (ValueError, Exception) as exc:
            if attempt < 2:
                logger.warning(
                    "Media validation batch %s attempt %d failed: %s — retrying",
                    batch_label, attempt, exc,
                )
                continue
            raise


# ── Assembly Validator prompt ─────────────────────────────────────────────────
# Receives high-level STATISTICS (not a full beat list) so token usage is bounded
# regardless of video length. Per-beat media replacement was removed from this
# validator because: (a) the Media Validation Agent already handles per-beat
# replacement earlier in the pipeline, (b) sending the full beat list caused
# truncation on long videos.

_ASSEMBLY_SYSTEM_PROMPT = """\
You are a post-production supervisor reviewing a finished video assembly plan.

You receive summary statistics for the assembled video — NOT a full beat list.
Your job: identify ASSEMBLY-LEVEL problems (pacing, flow, coherence) that would make
the video feel automated or low-quality to a real viewer. Judge at a macro level —
do not flag individual beat choices.

Common assembly failures to flag:
  - Duration drift: total section durations deviate from audio duration by more than ±2%
  - Monotonous pacing: almost all sections have identical duration (stddev < 0.5 s)
  - Visual environment overuse: one environment type exceeds 40% of all beats
  - Effect chaos: more than 4 different effects used with no discernible pattern
  - Color incoherence: sharp color_grade changes with no narrative reason
  - Transition monotony: more than 80% of transitions are the same type
  - Overlay overuse: more than 30% of beats carry overlay_text (clutters narration)
  - Long static stretches: 5+ consecutive beats all using slow_zoom or pan only

Report ONLY genuine assembly-level issues — do not flag individual beat choices.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "assembly_status": "APPROVED" | "NEEDS_ADJUSTMENT",
  "assembly_issues": [
    {
      "severity": "HIGH" | "MEDIUM",
      "category": "pacing" | "flow" | "drift" | "repetition" | "effects" | "color" | "overlay",
      "issue": "specific description of the assembly problem",
      "suggestion": "concrete suggestion for fixing it"
    }
  ],
  "overall_comment": "one-sentence summary"
}

Strict rules:
1. JSON only.
2. Report at most 5 issues — prioritize by impact on viewer experience.
3. Never invent beat-level details not provided in the summary.
4. Empty assembly_issues array means no genuine assembly problems found.\
"""


def validate_assembly_with_claude(
    sections: list[dict],
    total_duration_ms: int,
    channel_niche: str,
    channel_tone: str,
    channel_style: str,
) -> dict:
    """Ask Claude to validate overall assembly quality from high-level statistics.

    Sends a structured summary (distribution counts, drift, stddev) instead of the
    full beat list — this bounds token usage regardless of video length and avoids
    truncation on 60+ beat videos. Per-beat media replacement was removed from this
    validator (the Media Validation Agent handles that earlier in the pipeline).

    Args:
        sections:          Fully enriched, validated sections (post stock_fetcher).
        total_duration_ms: Expected total audio duration.
        channel_niche:     Channel niche for context.
        channel_tone:      Channel tone for context.
        channel_style:     Video style (e.g. "documentary").

    Returns:
        Dict with keys ``assembly_status``, ``assembly_issues``, ``overall_comment``.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    from collections import Counter as _Counter
    expected_sec = total_duration_ms / 1000
    sum_sec      = sum(s.get("duration_sec", 0) for s in sections)
    drift_pct    = abs(sum_sec - expected_sec) / max(expected_sec, 1) * 100

    durations  = [s.get("duration_sec", 0) for s in sections]
    avg_dur    = sum(durations) / len(durations) if durations else 0
    stddev_dur = (sum((d - avg_dur) ** 2 for d in durations) / len(durations)) ** 0.5 if durations else 0

    env_counts    = _Counter(s.get("environment", "other") for s in sections)
    effect_counts = _Counter(s.get("effect", "?") for s in sections)
    grade_counts  = _Counter(s.get("color_grade", "?") for s in sections)
    trans_counts  = _Counter(s.get("transition_to_next", "cut") for s in sections)
    overlay_count = sum(1 for s in sections if s.get("overlay_text", "").strip())
    overlay_pct   = overlay_count / max(len(sections), 1) * 100

    user_message = (
        f"Channel niche: {channel_niche}\n"
        f"Channel style: {channel_style}\n\n"
        f"Video stats:\n"
        f"  Beats: {len(sections)}  |  Expected: {expected_sec:.1f}s  |  "
        f"Section sum: {sum_sec:.1f}s  |  Drift: {drift_pct:.1f}%\n"
        f"  Duration avg: {avg_dur:.1f}s  |  Duration stddev: {stddev_dur:.1f}s\n"
        f"  Overlay beats: {overlay_count}/{len(sections)} ({overlay_pct:.0f}%)\n\n"
        f"Environment distribution: "
        + ", ".join(f"{e}={c}" for e, c in env_counts.most_common(6))
        + "\nEffect distribution: "
        + ", ".join(f"{e}={c}" for e, c in effect_counts.most_common())
        + "\nColor grade distribution: "
        + ", ".join(f"{g}={c}" for g, c in grade_counts.most_common())
        + "\nTransition distribution: "
        + ", ".join(f"{t}={c}" for t, c in trans_counts.most_common())
    )

    raw = call_claude(_ASSEMBLY_SYSTEM_PROMPT, user_message, max_tokens=1024)
    return parse_claude_json(
        raw,
        required_keys=["assembly_status", "assembly_issues", "overall_comment"],
        type_checks={
            "assembly_status": str,
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
