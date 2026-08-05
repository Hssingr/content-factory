import json
import logging
import re
import time

from app.services.claude_client import (
    call_claude,
    call_claude_structured_with_usage,
    parse_claude_json,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "5.3"  # v5.3: Phase A' — preserve style through deterministic
                        #        reframes; leak-proof meta rules; contextual material
                        #        variety; era/locale-correct textual props.
                        # v5.2: Short Quality Roadmap Phase A3 — material-detail
                        #        reframing, denser Short openings, and action imagery.
                        # v5.1: independent review of real output (code_report/
                        #        8abd7fea_independent_video_output_review.md) — two
                        #        additive prompt-only rules, no schema change: (1) prefer
                        #        depicting a named, identity-locked character physically
                        #        over a symbolic object standing in for them; (2) avoid
                        #        extreme close-ups isolating a single facial feature
                        #        (mouth/lips/eyes), a reliable image-model distortion mode.
                        # v5.0: Tier 5 R18 era-aware diversity — locked-era stories use
                        #        motif/camera-framing diversity and never invent an anachronistic
                        #        environment merely to evade an environment-frequency cap.
                        # v4.9: Tier 3 alignment rules — never split one narration span into
                        #        consecutive beats with identical hint pairs; align sensory-reveal
                        #        imagery to the beat containing the reveal rather than a prior callback.
                        # v4.8: roadmap Phase C2/C3/C4 (operator video-output audit) — four
                        #        additive changes, no schema field changes: (1) style-negative-
                        #        constraints line per image_style (_IMAGE_STYLE_NEGATIVE_CONSTRAINTS)
                        #        so a beat's rendering can't drift into a neighboring style (a real
                        #        run mixed photoreal/cartoon/anime in one video); (2) "Character
                        #        identities:"/"Era/setting lock:" continuity-line sentences documented
                        #        (data comes from Agent 2's new blueprint fields, threaded via the
                        #        existing continuity_line parameter — see storyboard.py); (3) beat-count
                        #        adherence language strengthened from "aim for" to an explicit
                        #        requirement (real runs under-delivered ~65% of target); (4) hint-length
                        #        (6-10 verbatim words) language strengthened with an explicit floor
                        #        rationale (real runs logged 100%-invalid-hint segments from short hints).
                        # v4.7: roadmap Phase A2 (operator video-output audit) — "dip_to_black" removed
                        #        from the transition_to_next enum/prompt/schema entirely. A real
                        #        production run showed ~13 dip_to_black beats per parent video, each
                        #        rendering ~0.67s of full black while narration continued — indistinguishable
                        #        from a rendering failure to a viewer. Freshly generated beats can no
                        #        longer receive it (schema enum), and storyboard.py's
                        #        _VALID_TRANSITIONS/_safe_enum normalizes any stray value to "cut" at
                        #        beat-build time; a row already PERSISTED with the literal string from
                        #        before this fix is untouched by either (load_video_sections() merges
                        #        stored JSON verbatim, no re-validation), so MediaSection.tsx also maps
                        #        "dip_to_black" onto the same treatment as "crossfade" at render time.
                        # v4.6: added "cinematic_cartoon" to the image_style vocabulary (operator request) —
                        #        the "Recognized values" list now describes cinematic_cartoon rendering
                        #        (simplified illustration, bold outlines, flat color fills — distinct
                        #        from anime/digital_art), and "cinematic_cartoon" is added to the TECHNICAL
                        #        step's illustrated/painted-styles list so it is never suffixed with
                        #        "sharp focus, no motion blur" or the word "photorealistic".
                        # v4.5: fresh full-system audit §2.2 — storyboard prompt repair.
                        #        (1) The dead "Visual Continuity Bible (compact JSON)"
                        #        section is deleted — the bible layer was removed by the
                        #        Elimination Mandate (D2.1) and nothing ever sends that
                        #        block; the prompt now documents the real input, the
                        #        one-line "Visual continuity:" hint. (2) The TECHNICAL
                        #        flux-prompt step no longer hardcodes "photorealistic" —
                        #        it defers to the operator's Global image style, so
                        #        anime/watercolor/oil_painting channels stop getting
                        #        contradictory prompts. (3) The "Per-10-beat composition
                        #        requirement" quota block is deleted — it contradicted
                        #        Principle A ("do not inject a shot purely to hit a
                        #        diversity quota") and pushed toward the exact
                        #        document-saturation pattern validator check 21 flags as
                        #        MAJOR; the rotation guidance (Principle B2 + beat
                        #        category rotation) carries the same intent without
                        #        quotas. (4) low-intensity duration band corrected to
                        #        3.0-6.0s (the hard constraint and INTENSITY_FLOOR_MS
                        #        both use 3.0; the old 4.0 lower bound contradicted
                        #        them). (5) visual_type "text_overlay" and
                        #        visual_category "text" removed from the schema and
                        #        prompt — subtitles-only rendering left nothing those
                        #        values can legitimately describe. (6) The style
                        #        vocabulary now documents every UI preset
                        #        (constants.js VISUAL_STYLE_OPTIONS/IMAGE_STYLE_OPTIONS)
                        #        so operator choices are never silent unknowns.
                        #        storyboard_status/global_notes also dropped from the
                        #        schema (dead administrative weight — never read; the
                        #        auxiliary-field defaults in _check_shape() keep old
                        #        responses tolerated).
                        # v4.4: roadmap 4a / audit P1-9 — de-hardcoded "documentary" from the
                        #        storyboard prompt's identity line ("visual director ... for an
                        #        automated multilingual documentary video production system" ->
                        #        drops "documentary" entirely, since the actual style is always
                        #        channel-configured via Global Visual Direction, never assumed)
                        #        and from both runtime fallback defaults (the "no global
                        #        direction specified" instruction and the visual_style Python
                        #        fallback), both now "story_driven" — matching the new
                        #        ChannelConfig.visual_style default. "documentary"/
                        #        "realistic_documentary" remain as legitimate example values in
                        #        the style-vocabulary list — only the assumed-when-unconfigured
                        #        default changed.
                        # v4.3: Elimination Mandate (code_report/
                        #        forensic_output_audit_borrasca_run.md, D2.1/D2.2/D2.3)
                        #        — the entire visual-bible layer (Claude generation call,
                        #        compact_visual_bible_for_storyboard(), bible injection into
                        #        every storyboard batch, apply_cinematic_prompts_to_beats()
                        #        enrichment, first-15 enhancement/validation) is deleted: a
                        #        real production run showed it return 0 locations, 0 motifs,
                        #        0 first-15 rules — a paid no-op. generate_storyboard_batch()
                        #        now takes a plain `continuity_line` string (see
                        #        storyboard.build_continuity_line_from_blueprint()) — a
                        #        deterministic, no-AI-call line naming proper nouns that
                        #        recur across the blueprint, replacing the v4.1 compact-JSON
                        #        bible block below. flux_generator.derive_text_prop_prompt()
                        #        (the v4.2 entry below) no longer rewrites the subject at
                        #        all: it now returns Claude's own flux_prompt verbatim plus
                        #        ONE short no-readable-text clause — the previous template
                        #        substitution (environment scene hint + detected prop label +
                        #        selected framing clause + a 10-clause negative wall) produced
                        #        broken English, self-contradictions, and identical prompts
                        #        for distinct beats, directly causing duplicate images.
                        # v4.2: Phase 2.9 (audit G-3) — reworked "Every beat is a
                        #        generated image" into a named, generalizable
                        #        text-prop framing taxonomy (ANGLE / DISTANCE /
                        #        LIGHTING / DETAIL) so Claude can apply the
                        #        technique to any text-bearing prop, not only the
                        #        four worked examples v4.0 shipped with. The
                        #        deterministic Python-side sanitizer
                        #        (flux_generator.derive_text_prop_prompt(), Phase
                        #        14.7) now injects the same taxonomy as a
                        #        positive framing clause instead of a purely
                        #        negative "no readable text" instruction —
                        #        Flux prompting guidance is explicit that
                        #        positive description outperforms negative
                        #        instruction (Flux has no true negative-prompt
                        #        channel). No JSON schema change.
                        # v4.1: Phase 2.1 prompt inversion — visual_bible is now
                        #        passed into generate_storyboard_batch() as compact JSON
                        #        so Claude writes continuity into the validated storyboard
                        #        flux_prompt natively. Post-storyboard enrichment is
                        #        additive only and never replaces prompts with templates.
                        # v4.0: subtitles-only rendering (audit G-0/G-8) — removed the
                        #        "Media strategy selection" section (remotion_text_card is
                        #        gone; there are no text cards and no on-screen overlay text;
                        #        the subtitle track is the video's only text layer), removed
                        #        the overlay_text/overlay_position per-beat decisions, and
                        #        added "Every beat is a generated image": textual narration
                        #        moments are designed as physical scenes WITHOUT readable
                        #        text (phone face-down, closed folder, unreadable-distance
                        #        calendar) — the narration already says what the text said.
                        # v3.5: wire operator-configured visual_style and image_style into
                        #        every storyboard call via a "Global Visual Direction" block
                        #        injected into the user message. The system prompt now documents
                        #        how Claude should apply these constraints to flux_prompt,
                        #        color_grade, and effect choices while keeping Principles A/B
                        #        and the anti-slideshow rules intact. Defaults to
                        #        documentary/photorealistic when not configured.
                        # v3.4: Phase 14.8 — added "Principle B2: amplify, don't illustrate"
                        #        and a "beat category rotation" section to
                        #        _STORYBOARD_SYSTEM_PROMPT, instructing beats to add a
                        #        reaction/threat/consequence/evidence/tension/foreshadowing/
                        #        vulnerability/aftermath element rather than literally
                        #        restating the narration's noun/action, and to rotate
                        #        between six categories expressed via the EXISTING
                        #        visual_category/visual_type/motif fields (no schema change —
                        #        STORYBOARD_SCHEMA_VERSION stays 6.1). Also added one line to
                        #        _SPLITTER_SYSTEM_PROMPT's SEARCH QUERY rules nudging the
                        #        legacy fallback path toward the same reaction/consequence/
                        #        evidence framing (no JSON schema change there either).
                        # v3.3: _SPLITTER_SYSTEM_PROMPT (legacy section-splitter fallback,
                        #        reachable only via ChannelConfig.allow_legacy_fallback) had
                        #        "atmospheric", "cinematic", and "dramatic" wording that
                        #        directly contradicted FORBIDDEN_FLUX_WORDS — replaced with
                        #        concrete subject/place/action guidance. The synthesized
                        #        flux_prompt template in enrich_sections_with_visuals() also
                        #        hardcoded "cinematic" into every legacy beat unconditionally
                        #        — replaced with "documentary photography style". No other
                        #        prompt text changed; _STORYBOARD_SYSTEM_PROMPT (below) was
                        #        already compliant. Phase 12.6.
                        #        (Historical note, prompt engineering audit §2.7: the legacy
                        #        section-splitter fallback flow these two entries describe —
                        #        including _SPLITTER_SYSTEM_PROMPT itself — was fully removed
                        #        by roadmap 6.3/audit §7; it no longer exists anywhere in this
                        #        codebase. This entry is kept as accurate history of what v3.3/
                        #        v3.4 changed at the time, not a pointer to current code.)
                        # v3.2: Added hard rules forbidding literal quotation marks in any
                        #        field value and forbidding 'beats'/'global_notes' as a
                        #        JSON-encoded string — a real, recurring production failure
                        #        (json.JSONDecodeError "Expecting ',' delimiter" at a
                        #        consistent offset, across repeated calls on the same
                        #        segment) traced to the model embedding an unescaped literal
                        #        quote from quoted narration dialogue while manually
                        #        stringifying the beats array.
                        # v3.1: Removed why_this_visual and story_progression_role
                        #        per-beat instructions (Phase 6D-1: live A/B proof found
                        #        no measurable quality regression on visual_intent/flux_prompt
                        #        specificity, validator findings, or image subject fidelity;
                        #        see code_report/phase6d1_reasoning_scaffolding_ab_proof.md).
                        # v3.0: Replaced stock-fetcher / media-scoring infrastructure with
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

STORYBOARD_SCHEMA_VERSION = "7.2"  # v7.2: roadmap Phase A2 — removed "dip_to_black" from the
                                   #        transition_to_next enum (legacy persisted values
                                   #        normalize to "cut" via _safe_enum in storyboard.py).
                                   # v7.1: fresh full-system audit §2.2/§4 — removed
                                   #        visual_type "text_overlay" and visual_category
                                   #        "text" (nothing they describe exists under
                                   #        subtitles-only rendering; legacy stored values
                                   #        normalize to the defaults via _safe_enum), and
                                   #        removed storyboard_status/global_notes from the
                                   #        batch schema (dead administrative weight — never
                                   #        read downstream; _check_shape() still defaults
                                   #        them in place for tolerant parsing).
                                   # v7.0: subtitles-only rendering (audit G-0/G-8) — removed
                                   #        media_strategy, stock_queries, fallback_flux_prompt,
                                   #        text_card_style, overlay_text, overlay_position from
                                   #        the beat schema. Every beat is a Flux-generated
                                   #        image; Python hardcodes media_strategy=
                                   #        "flux_generated" downstream. Stock was never
                                   #        implemented (always overridden); text cards and
                                   #        overlay text are removed product-wide.
                                   # v6.1: removed why_this_visual and story_progression_role —
                                   #        confirmed zero downstream consumers (Phase 6B-1) and,
                                   #        per a live A/B proof (Phase 6D-1), zero measurable
                                   #        quality regression; ~17.6% lower storyboard output
                                   #        payload on the tested segment.
                                   # v6.0: media_strategy (flux_generated | remotion_text_card |
                                   #        stock_video | stock_image), stock_queries [],
                                   #        fallback_flux_prompt "", text_card_style per beat.
                                   #        stock_video / stock_image overridden to flux_generated
                                   #        by Python until a future release.
                                   # v5.0: beat_intensity + suggested_duration_sec per beat
                                   # v3.0: forced tool-use schema (call_claude_structured_with_usage)
                                   # v2.5: query_style field
                                   # v2.4: stock_query + broad_query fields

# Style-negative-constraints (roadmap Phase C2, operator video-output audit):
# a real production run shipped ONE video mixing photoreal-cinematic (cover
# frame), flat cartoon (several mid-video beats), and anime-style rendering
# (one beat) — the storyboard prompt's own image-style vocabulary already
# distinguishes these, but nothing told Claude a given style must NOT drift
# into a neighboring one. Deterministic Python lookup, keyed by the exact
# `image_style` values in the vocabulary above/app/ui/src/constants.js's
# IMAGE_STYLE_OPTIONS (CLAUDE.md §8.4 keeps the two lists in sync) — an
# unrecognized/custom image_style value simply gets no constraint line
# (fail-open, matches the existing "apply the closest reading for any other
# string" convention for visual_style).
_IMAGE_STYLE_NEGATIVE_CONSTRAINTS: dict[str, str] = {
    "photorealistic":    "no illustration, no painting, no cartoon, no anime, no visible brushstrokes or line art",
    "cinematic_realism":  "no illustration, no painting, no cartoon, no anime — real photographic detail only",
    "dark_realistic":     "no illustration, no painting, no cartoon, no anime, no bright/cheerful color grading",
    "vintage_film":       "no digital-clean modern look, no illustration, no cartoon, no anime",
    "digital_art":        "no photorealistic photography, no live-action imagery, no anime linework, no comic-panel framing",
    "cinematic_cartoon":  "no photorealism, no live-action photography, no anime linework, no comic-book panel borders, not a flat/childish kids-cartoon look",
    "oil_painting":       "no photorealistic photography, no clean digital/vector look, no anime, no flat cartoon shading",
    "watercolor":         "no photorealistic photography, no clean digital/vector look, no anime, no flat cartoon shading, no heavy black outlines",
    "anime":              "no photorealistic photography, no live-action imagery, no Western flat-cartoon look, no painterly oil/watercolor texture",
}


_STORYBOARD_SYSTEM_PROMPT = """\
You are a visual director and editor for an automated multilingual video \
production system. Think like a human video editor, not a stock-search generator.

You design the storyboard ONE NARRATION SEGMENT AT A TIME — a single [INTRO],
[SECTION N], or [OUTRO] block — never the whole video in one pass. You receive:
which segment this is (its position among the video's narration segments), the
segment's narration text, the channel niche/tone/format, an optional operator-configured
global visual direction and image style, an optional one-line visual-continuity hint
(names that recur across the story), and a short note on the visual approach used
in the immediately preceding segment (for continuity only — do not repeat it).

Design an ordered sequence of visual beats that carries the viewer through THIS
SEGMENT's narration — and ONLY this segment's narration — from its first word to
its last.

When the previous-segment context contains lines beginning
"FORBIDDEN environments for the next segment:" or
"FORBIDDEN motifs for the next segment:", treat those values as hard diversity
constraints for THIS response. Do not use those environment or motif values
unless the narration explicitly makes one unavoidable; if unavoidable, use it
only for the minimum number of beats and vary every other dimension.

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

== Principle B2: Visuals must amplify the narration, not illustrate it ==
A visual that simply shows the literal noun or action the narration just named
is illustration, not amplification — the single most common reason these
videos feel artificial. If the narration says "she entered the room," the beat
must NOT simply show a room. It must add something the words alone do not
already give the viewer.

Every beat must add at least ONE of:
  - emotional reaction — a human response to what is happening
  - hidden threat — something in the environment that signals danger before the
    narration names it
  - consequence — the result or fallout of an action already described
  - evidence — a concrete object, prop, or document that supports or
    complicates the story
  - spatial tension — framing/composition that makes the space itself feel
    unsafe or charged, not just a neutral establishing shot
  - symbolic/foreshadowing detail — a concrete object that hints at what is
    coming next
  - human vulnerability — a detail that makes a person feel exposed, small,
    or at risk
  - aftermath — what is left behind after an event, shown instead of the
    event itself

Before finalizing any beat, ask: "what does this shot ADD that the narration
sentence alone does not already tell the viewer?" If the honest answer is
"nothing — it's just the noun from the sentence," redesign the beat using one
of the eight additions above instead.

== Visual continuity ==
When a "Visual continuity:" line appears in the user message, it names the people,
places, or objects that recur across this story. Give each named entity ONE stable,
concrete physical identity (appearance, clothing, defining detail) and keep it
consistent in every beat that shows it — invent the identity once, then reuse it.
Never add an "Avoid:" suffix or a negative-prompt list to flux_prompt; describe
only what the image should contain.

When a "Character identities:" line appears, it gives you the story's own locked
name/age/physical-description for its recurring characters — generated once at
blueprint time, grounded in the story. Use these EXACT descriptions whenever a
beat depicts one of these people; do not invent a conflicting appearance, and do
not silently drop a described detail (hair, build, clothing) in a later beat
showing the same person. This is the deterministic replacement for keeping one
character looking like the same person across 100+ beats — a real production
run showed a story's protagonist rendered as 6+ visually different women with
nothing enforcing consistency.

When a beat's narration centers on a NAMED character who has a locked
"Character identities:" description, prefer depicting that person physically
(using their locked description) over a purely symbolic object standing in
for them. An independent review of real output found this failure mode
repeatedly — a musician represented only by his lute, a child represented
only by an empty bonnet or a toy, a brother represented only by an empty
coat, speech represented only by an extreme mouth close-up. Reserve an
object-only beat for when the OBJECT ITSELF, not the person, is the actual
subject of this exact sentence (e.g. the narration is specifically about the
lute being found, not about the musician). When in doubt and the person is
available to draw, draw the person.

When an "Era/setting lock:" line appears, it names this story's historical period
and physical setting. Every beat's props, clothing, architecture, technology, and
background details must be authentic to that period and place — no modern flags,
modern clothing silhouettes, contemporary street furniture, or anachronistic
technology unless the story is explicitly contemporary. A real production run
shipped modern anachronisms (a modern flag, a modern city waterfront, suit-like
contemporary clothing) inside a 6th-century Byzantine story; this line exists
specifically to stop that.
For an era-locked story, never choose a laboratory, industrial, vehicle, office,
or other environment merely to increase label diversity. If none of the fixed
environment values honestly fits the period/location shown, use other and
create diversity through motif, camera distance/angle, composition, and effect.

== Global Visual Direction (operator-configured) ==
When "Global visual direction:" and "Global image style:" lines appear in the user
message, apply them as consistent stylistic constraints across every beat:
  - Visual direction governs overall mood, color palette, and lighting feel.
    Recognized values (operator presets — apply the closest reading for any other string):
    "story_driven" → grounded, narrative-first, follows the story's emotional beats;
    "documentary" → neutral, factual, naturalistic; "true_crime" → dark suburban
    realism, evidence photography, institutional interiors; "investigative" →
    evidence-board framing, surveillance angles, document close-ups; "cinematic" →
    film-quality depth, deliberate angles, rich shadow play; "historical" →
    period-accurate settings, archival-photograph aesthetics; "noir" → high-contrast
    shadows, muted desaturated tones, deep blacks; "suspense_thriller" → tight
    close-ups, shallow depth of field, high-tension lighting; "nature" → lush
    environments, macro detail, golden-hour light; "educational" → clean, well-lit,
    clear compositions; "retro" → film grain, analog warmth, era-saturated palettes.
  - Image style governs the artistic rendering approach for Flux-generated images.
    Recognized values: "photorealistic" → realistic photography, accurate lighting and
    material; "cinematic_realism" → film-quality photography, graded color, shallow
    depth of field; "dark_realistic" → gritty desaturated realism, moody
    high-contrast lighting; "vintage_film" → 35mm grain, muted analog tones;
    "digital_art" → polished digital illustration, clean lines, vivid color;
    "cinematic_cartoon" → simplified cinematic_cartoon illustration, bold clean outlines, flat bright
    color fills, stylized (not anime, not comic-panel) — friendlier and less
    detailed than digital_art; "oil_painting" → textured classical brushwork;
    "watercolor" → soft translucent washes; "anime" → anime-influenced rendering,
    bold outlines.
  - Weave these as stylistic constraints into flux_prompt (append one short style
    clause, e.g. "documentary photography, naturalistic lighting"), color_grade
    (match the mood), and effect choices — NEVER at the cost of Principle A
    (narrative relevance) or the anti-slideshow rules.
  - When no global direction is specified in the user message, default to
    story_driven / photorealistic.
  - When a "Style constraints (this image style must NOT look like):" line
    appears, it names the neighboring rendering styles this beat's image must
    stay clearly distinct from (a real production run shipped ONE video mixing
    photoreal-cinematic, flat cartoon, and anime-style beats — exactly what
    this line prevents). Honor it by choosing flux_prompt's own descriptive
    words so the image cannot read as one of those neighboring styles — do NOT
    copy the negative phrase itself into flux_prompt (flux_prompt stays purely
    positive/descriptive, per the "no Avoid: suffix" rule above).

== Beat category rotation (sequence diversity) ==
Use the EXISTING visual_category / visual_type / motif fields below to express
six rotation categories — do not invent new fields or values:
  - human reaction       → visual_category="person", motif="face" or "hands"
  - threatening space     → visual_category="place", motif="doorway"/"corridor"/"room",
    composition/lighting chosen to make the space feel charged, not neutral
  - evidence / prop       → visual_category="object" or "document", motif="object"/
    "document"/"photo"
  - environmental clue    → visual_category="place" or "object", motif="room"/"object"/
    "reflection"
  - consequence/aftermath → visual_category="place" or "object"; visual_intent makes
    the aftermath explicit (what changed, what was left behind)
  - motion/action         → visual_type="action"

Do not repeat the same rotation category more than twice in a row. A run such
as object → object → room → object — all evidence/environmental-clue, with zero
human-reaction, threatening-space, or consequence/aftermath variety — is exactly
the pattern to avoid. Within every 3–4 beat stretch, include at least one
human-reaction, threatening-space, or consequence/aftermath beat.

== Pacing ==
- youtube_long format: place one visual beat every 3–5 seconds of narration.
- short-form formats (youtube_short / tiktok / reels): one beat every 2–4 seconds.
- Never let a single still image hold the screen longer than 6 seconds.
- For every short-form format, cover the first approximately 6 seconds with 2–3
  distinct beats, never one held image. The first beat depicts the specific
  subject of the hook — the named person, object, or event in the first sentence —
  never generic scenery or an unrelated establishing shot. No opening beat may
  span the entire first sentence: its start_hint/end_hint delimit only its own
  portion. Examples: show the trader making the purchase, the figure entering
  the threatened room, or the experiment visibly beginning.

== Dynamic beat duration ==

Every beat must carry a beat_intensity value based on what the narration is doing
at that moment. This is the single most important signal for visual pacing.

high (suggested_duration_sec: 1.0–2.5):
  Use for: reveals, contradictions, shocking facts, key questions, moments of
  realization, sudden reversals.
  The viewer is absorbing new information. Short beats create urgency.
  A reveal held for 4 seconds loses its impact.

medium (suggested_duration_sec: 2.5–4.0):
  Use for: normal story progression, introducing evidence, character actions,
  scene transitions, context that builds toward a reveal.

low (suggested_duration_sec: 3.0–6.0):
  Use for: location establishing, emotional pauses, deliberate slow buildup.
  NEVER use low intensity for more than 2 consecutive beats — this creates the
  "slideshow" effect. After 2 low beats, the next must be medium or high.

Hard constraints:
- suggested_duration_sec must fall within its intensity tier range.
- A reveal beat (high) must never have suggested_duration_sec > 2.5.
- A context beat (low) must never have suggested_duration_sec < 3.0.
- The final beat before a section boundary should be high or medium — never end
  a section on a low beat.

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

== Every beat is a generated image ==

There are no text cards and no on-screen overlay text — the video's only text
layer is the subtitle track, rendered separately. Every beat you design becomes
exactly one Flux-generated image. Image models cannot render legible text —
any prompt that asks for readable words always comes back as illegible
gibberish, no matter how the request is worded.

Never make a text-bearing object (document, sign, board, screen, newspaper,
label, or captioned map) the primary subject of a beat, and never compose an
establishing shot like a captioned or titled documentary card. Depict material
reality instead: texture, edges, seals, hands interacting with it, or the scene
around it. Examples: a signed document becomes its wax seal and torn paper edge
in close-up; a price board becomes chalk dust and a hand mid-stroke with the
board out of focus; a city establishing shot shows the period-correct street
itself at eye level, never a location title card.

Write the channel's Global Visual Direction image-style clause FIRST in every
flux_prompt, verbatim. The flux_prompt describes only what the model should
depict: never copy instructional vocabulary such as "title card", "no readable
text", "legible words", or other rule phrasing into it.

Vary the physical reframe with the story context. When the same object class
recurs, rotate among materially different treatments (for example edge-on
folds, hands sorting pages, binding thread, fasteners, reflected light) rather
than repeatedly defaulting to the same wax-seal close-up.

ERA AND LOCALE: every prop, currency item, document, sign, and storefront must
belong to the story's stated period and place. Describe currency in period
material terms and never introduce modern currency symbols or designs. For
example, an eighteenth-century trade story may show worn period paper notes,
fibers, and an exchange by hand—not a modern dollar symbol; a present-day
medical story in Seoul may show locally plausible hospital wayfinding and
architecture at an unreadable angle—not a generic American storefront sign.

When narration is textual, choose a physical framing where legible text is
naturally impossible or irrelevant, using ONE of these four techniques:
  - ANGLE — shoot the text-bearing surface from the side, from behind, or
    at an oblique angle so a reader's view is blocked or foreshortened (a
    closed folder seen from its spine, a phone face-down on a table, a
    note glimpsed over a shoulder).
  - DISTANCE — make the text-bearing surface a small element in a wider or
    more distant shot rather than the subject itself (a calendar on a far
    wall, a stack of newspapers seen across a room).
  - LIGHTING — use raking or grazing side light across the surface so any
    text is obscured by glare, shadow, or texture rather than lit for
    reading.
  - DETAIL — push in on a physical, non-textual detail of the object
    instead of the text (typewriter keys instead of the typed page, a worn
    corner or torn edge instead of a headline, a wax seal instead of a
    letter's contents).
The narration itself already tells the viewer what the text said — the image
only needs to establish the physical object, never repeat its content.

When narration describes an action or consequence, depict that action or its
immediate visible result — never a static portrait, building, or related noun.
Examples: buying becomes hands exchanging value; fleeing becomes a figure in
motion through the actual setting; collapsing becomes the visible fall or
aftermath; discovering becomes the character reacting to the evidence.

== Flux image generation prompt rules ==
Each beat requires a ``flux_prompt`` — an image generation prompt, written in the
channel's configured Global image style, that Flux will use to create the visual
for this beat.

THE PRIME DIRECTIVE: the flux_prompt must answer "what exact thing would a camera be
pointing at right now?" — not a mood, not an atmosphere, but a physical subject.

FORBIDDEN words — using any of these in a flux_prompt is an automatic failure:
  atmospheric, cinematic, mysterious, eerie, ominous, dramatic, moody, haunting,
  brooding, foreboding, unsettling, epic, intense, dark (as mood descriptor), ethereal.
  These describe feelings. Flux generates images. Describe what IS IN THE FRAME.

Build every flux_prompt in this exact order (50–80 words total):
  1. SUBJECT — the single most specific concrete element from the narration text:
     a named object, a specific place, a visible action, something the narration mentions.
  2. COMPOSITION — where is the camera? (close-up, wide shot, overhead, eye-level).
     Avoid an extreme close-up isolating a single facial feature (mouth, lips,
     or eyes only, filling the frame) — image models reliably distort anatomy
     at that framing (an independent review of real output flagged exactly
     this as the single worst image in a series). For speech or emotion,
     prefer a medium facial close-up that shows the whole face.
  3. SETTING — specific location detail ("a 1980s hospital waiting room with plastic chairs
     and wall-mounted TV" not "a room", "a cobblestone alley in an old European city" not "street")
  4. LIGHTING — exact quality and source (morning side light through venetian blinds,
     fluorescent overhead, golden late-afternoon slant, overcast diffuse, incandescent warm)
  5. TECHNICAL — render in the Global image style from the user message (default
     photorealistic). For photographic styles ("photorealistic", "cinematic_realism",
     "dark_realistic", "vintage_film") end with "sharp focus, no motion blur"; for
     illustrated/painted styles ("digital_art", "cinematic_cartoon", "oil_painting", "watercolor",
     "anime") name the medium instead and NEVER write "photorealistic". Always: no people unless
     motif=face/hands; no logos, no text in frame, no brand names.

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
   Use the FULL 6–10 word range whenever the beat's own narration span is at least
   that long — never stop at 2-3 words just because a natural phrase break appears
   earlier. A hint shorter than 6 words is frequently ambiguous (it can match more
   than one place in the narration) and is treated as lower-confidence, degrading
   this beat's timing accuracy — a real production run logged a 100% invalid-hint
   rate on some segments from exactly this under-length pattern. Only go below 6
   words when the beat's own narration span genuinely contains fewer than 6 words
   in total.
2. visual_intent — one sentence describing what the viewer should see and feel.
3. visual_type — b-roll | action | document | map | screenshot | generated_visual
4. visual_category — person | place | object | document | screen | map | abstract
5. environment — fixed SETTING label, choose the closest honest match:
     underwater | indoor_office | indoor_domestic | forest_nature | urban_street |
     corridor_interior | abstract_dark | open_landscape | laboratory | industrial |
     vehicle | other
   If an Era/setting lock is present and none of these is historically honest,
   use other. Never force an anachronistic label to satisfy diversity guidance.
6. flux_prompt — Flux Schnell image generation prompt (see rules above). Never
   include readable-text instructions — no quoted phrases, no "the text reads".
7. effect — slow_zoom | zoom_out | pan | push_in | shake | cut | fade_in | parallax
8. color_grade — desaturated | cold_blue | warm_amber | dark_contrast | neutral
9. transition_to_next — cut | crossfade | whip_pan | zoom_blur | match_cut | none
   (never dip_to_black — a full fade through black reads as a rendering failure
   while narration continues; use crossfade for a soft transition instead)
10. motif — dominant visual motif this beat shows:
      doorway | corridor | face | hands | object | clock | phone | photo | exterior |
      text | screen | reflection | document | room | other

== Hard rules ==
- Never invent names, dates, places, facts, people, URLs, or statistics.
- Do not repeat the same visual idea on consecutive beats.
- Consecutive beats must NEVER use the same start_hint/end_hint pair. A montage
  over one sentence or one narration span must be expressed as ONE beat using
  the strongest image for that span, not several beats sharing its anchors.
- When a beat's narration span contains a sensory reveal — what is newly seen,
  heard, felt, smelled, or otherwise discovered — visual_intent and flux_prompt
  must depict that reveal in THIS beat. Do not show an earlier callback instead
  or delay the reveal image into the following narration phrase.
- start_hint and end_hint must be copied EXACTLY from the segment text, in
  order, EXCEPT: omit any literal quotation mark character (") that appears
  in the segment text — copy the surrounding words exactly, just drop the
  quote mark itself. (Matching ignores punctuation already, so this does
  not affect timestamp alignment.)
- Never copy a [INTRO]/[SECTION N]/[OUTRO] marker into start_hint or end_hint.
- Hints must contain no digits — write out any number in words.
- The `beats` field of your tool call must be a native JSON array of beat
  objects — never a JSON-encoded string containing array text. No field
  value (start_hint, end_hint, visual_intent, flux_prompt, etc.)
  may itself contain a literal quotation mark character (") — if the
  segment narration contains quoted dialogue or a written note, describe or
  paraphrase it without the surrounding quote marks instead of copying them.

Strict rules:
1. Generate ONLY beats for THIS segment. beat_order values must be sequential integers
   starting at 0, in narration order. The target beat count provided is a
   REQUIREMENT, not a suggestion — generate that many beats, within ±2. Do not
   stop early or under-deliver to save effort: a segment that returns
   significantly fewer beats than its target forces each remaining beat to hold
   the screen far longer than intended, which reads as a slideshow, not a video.
   If you are unsure whether a stretch of narration deserves its own beat, split
   it into two beats rather than merging it into a neighboring one.
2. Every beat's start_hint/end_hint must be copied EXACTLY from the segment text you
   received — never paraphrased, and never including the marker label.
3. Every beat must include a ``motif`` field chosen from the allowed list.\
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
        "visual_type":             {"type": "string", "enum": ["b-roll", "action", "document", "map", "screenshot", "generated_visual"]},
        "visual_category":         {"type": "string", "enum": ["person", "place", "object", "document", "screen", "map", "abstract"]},
        "environment":             {"type": "string", "enum": ["underwater", "indoor_office", "indoor_domestic", "forest_nature", "urban_street", "corridor_interior", "abstract_dark", "open_landscape", "laboratory", "industrial", "vehicle", "other"]},
        "flux_prompt":             {"type": "string", "description": "Flux image generation prompt: specific physical subject only, no mood words, no faces, no logos, no readable text."},
        "effect":                  {"type": "string", "enum": ["slow_zoom", "zoom_out", "pan", "push_in", "shake", "cut", "fade_in", "parallax"]},
        "color_grade":             {"type": "string", "enum": ["desaturated", "cold_blue", "warm_amber", "dark_contrast", "neutral"]},
        "transition_to_next":      {"type": "string", "enum": ["cut", "crossfade", "whip_pan", "zoom_blur", "match_cut", "none"]},
        "motif":                   {"type": "string", "enum": ["doorway", "corridor", "face", "hands", "object", "clock", "phone", "photo", "exterior", "text", "screen", "reflection", "document", "room", "other"]},
        "beat_intensity":          {"type": "string", "enum": ["high", "medium", "low"], "description": "Narrative intensity at this moment: high=reveal/shock (1–2.5s), medium=progression (2.5–4s), low=establishing/pause (3–6s)."},
        "suggested_duration_sec":  {"type": "number", "description": "Suggested display duration in seconds. Must fall within the intensity tier range."},
    },
    "required": [
        "beat_order", "start_hint", "end_hint", "visual_intent",
        "visual_type", "visual_category", "environment", "flux_prompt",
        "effect", "color_grade", "transition_to_next", "motif",
        "beat_intensity", "suggested_duration_sec",
    ],
    "additionalProperties": False,
}

_STORYBOARD_BATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overall_style":     {"type": "string", "description": "One short phrase describing the visual direction of THIS segment."},
        "beats":             {"type": "array", "items": _BEAT_SCHEMA},
    },
    # Only `beats` is required (and only `beats` has real downstream consumers).
    # `storyboard_status`/`global_notes` were removed from the schema entirely
    # (v7.1 — dead administrative weight the model paid tokens to generate);
    # `overall_style` is kept as an optional one-phrase diagnostic that feeds a
    # single log line. _check_shape() still defaults all three in place so a
    # response carrying (or missing) any of them is tolerated — additionalProperties
    # below is guidance to steer the model away from re-generating the retired
    # fields (the exact token waste v7.1 removed them to stop), not a runtime
    # rejection: _check_shape() never errors on an unexpected extra key.
    "required": ["beats"],
    "additionalProperties": False,
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
    visual_style: str = "",
    image_style: str = "",
    continuity_line: str = "",
) -> tuple[dict, dict, dict]:
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
        continuity_line: Optional deterministic continuity hint — see
            ``storyboard.build_continuity_line_from_blueprint()`` — appended
            as a plain line in the user message. No AI call, no compaction;
            replaces the deleted visual-bible layer (Elimination Mandate,
            code_report/forensic_output_audit_borrasca_run.md, D2.1).

    Returns:
        ``(storyboard, usage, diag)`` — storyboard has keys ``storyboard_status``,
        ``overall_style``, ``beats``, ``global_notes``; usage is the token dict;
        diag has keys ``was_truncated`` (bool), ``attempt_count`` (int),
        ``input_tokens`` (int — total across all attempts), ``elapsed_ms`` (int — total ms).

    Raises:
        ValueError: If the tool-use response is malformed.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    def _build_message(beat_count: int) -> str:
        count_line = (
            f"Target beat count for this segment: {beat_count} beats — a requirement, "
            f"not a suggestion; generate this many, within ±2\n"
            if beat_count > 0 else ""
        )
        direction_lines = ""
        if visual_style or image_style:
            direction_lines = (
                f"Global visual direction: {visual_style or 'story_driven'}\n"
                f"Global image style: {image_style or 'photorealistic'}\n"
            )
            style_constraint = _IMAGE_STYLE_NEGATIVE_CONSTRAINTS.get(image_style)
            if style_constraint:
                direction_lines += f"Style constraints (this image style must NOT look like): {style_constraint}\n"
        continuity_lines = f"{continuity_line}\n" if continuity_line else ""
        return (
            f"Channel niche: {channel.niche}\n"
            f"Channel tone: {channel.tone}\n"
            f"Script format: {script_format}\n"
            + direction_lines
            + continuity_lines
            + f"Segment: {segment_label} — {segment_index} of {segment_count} narration segments in this video\n"
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

    def _coerce_string_to_expected_type(raw: str, expected_type: type) -> tuple[object, str]:
        """Try to recover ``expected_type`` from a string Claude should have returned natively.

        Handles three observed shapes, in order:
          1. The string IS the JSON value, verbatim (most common quirk).
          2. The string wraps the JSON value in a markdown code fence
             (```` ```json ... ``` ````/```` ``` ... ``` ````) — stripped before parsing.
          3. The string contains a valid JSON value followed by trailing
             non-JSON content (e.g. commentary after the array) — recovered
             with ``json.JSONDecoder.raw_decode``, which parses only the
             leading value and ignores what follows.

        Returns:
            ``(parsed_value_or_None, diagnostic)`` — ``diagnostic`` is ``""``
            on success, otherwise a short human-readable reason suitable for
            logging (the exact ``JSONDecodeError`` message/position, or which
            stage failed) so a failure is debuggable from the log alone.
        """
        candidate = raw.strip()
        fence_match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", candidate, re.DOTALL)
        if fence_match:
            candidate = fence_match.group(1).strip()

        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, expected_type):
                return parsed, ""
            return None, f"parsed but wrong type ({type(parsed).__name__})"
        except json.JSONDecodeError as exc:
            full_error = str(exc)

        # Fall back to raw_decode: tolerates trailing non-JSON content after
        # an otherwise-valid value (e.g. the model appended commentary).
        try:
            parsed, end_idx = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            return None, full_error

        if not isinstance(parsed, expected_type):
            return None, f"raw_decode parsed but wrong type ({type(parsed).__name__})"

        trailing = candidate[end_idx:].strip()
        if trailing:
            logger.warning(
                "STORYBOARD_SHAPE_COERCED_TRAILING_DATA — %d trailing char(s) after the "
                "parsed value were discarded: %r",
                len(trailing), trailing[:120],
            )
        return parsed, ""

    _AUXILIARY_FIELD_DEFAULTS: dict = {
        "storyboard_status": ("", str),
        "overall_style":     ("", str),
        "global_notes":      ([], list),
    }

    def _check_shape(storyboard: dict) -> None:
        """Raise ValueError only if ``beats`` is missing or wrong-typed.

        ``beats`` is the sole load-bearing field in this schema — every other
        field (``storyboard_status``, ``overall_style``, ``global_notes``) is
        administrative scaffolding with zero real downstream consumers
        (confirmed by grep: ``storyboard_status`` is never read past this
        shape check, ``global_notes`` is never read at all, ``overall_style``
        only feeds one cost/diagnostic log line). A real production response
        arrived with a complete, valid ``beats`` array but none of the other
        three keys — treating that as fatal discarded good beat data over
        fields nothing reads. Auxiliary fields are defaulted in place with a
        DEBUG diagnostic rather than aborting the segment.

        Before treating ``beats`` itself as wrong-typed, attempt one narrow
        coercion via ``_coerce_string_to_expected_type``: a real, observed
        model quirk returns ``beats`` as a JSON-encoded string instead of a
        native list, on an otherwise complete, non-truncated response (e.g.
        ``'[\\n  {"beat_order": 0, ...}\\n]'`` instead of the parsed array) —
        sometimes wrapped in a markdown code fence, or followed by trailing
        non-JSON commentary. If recovery succeeds, the parsed value is
        substituted in place and logged as ``STORYBOARD_SHAPE_COERCED`` — this
        is deterministic JSON re-parsing of a value Claude already produced,
        not blind trust of unvalidated content: the coerced value still has
        to pass every later beat-level enum/type check in
        ``_build_beat_section()``. A string that still fails to recover is
        logged with the specific parse failure reason (not just "wrong
        type") before falling through to the existing failure path unchanged.

        Logs the actual malformed value (truncated) before raising — earlier
        runs raised the same ValueError with no record of what Claude actually
        returned, making the failure unreproducible/undiagnosable after the fact.
        """
        for key, (default, expected_type) in _AUXILIARY_FIELD_DEFAULTS.items():
            if key not in storyboard:
                logger.debug(
                    "STORYBOARD_SHAPE_AUX_FIELD_MISSING segment=%s key=%s — "
                    "administrative field absent, defaulting to %r (never read "
                    "downstream beyond this check)",
                    segment_label, key, default,
                )
                storyboard[key] = default
            elif not isinstance(storyboard[key], expected_type):
                logger.warning(
                    "STORYBOARD_SHAPE_AUX_FIELD_WRONG_TYPE segment=%s key=%s "
                    "expected=%s got=%s — defaulting to %r",
                    segment_label, key, expected_type.__name__,
                    type(storyboard[key]).__name__, default,
                )
                storyboard[key] = default

        if "beats" not in storyboard:
            logger.error(
                "STORYBOARD_SHAPE_INVALID segment=%s key=beats issue=missing "
                "present_keys=%s",
                segment_label, sorted(storyboard.keys()),
            )
            raise ValueError("storyboard_batch response missing required key 'beats'")

        actual = storyboard["beats"]
        if not isinstance(actual, list):
            coercion_failure_reason = ""
            if isinstance(actual, str):
                parsed, coercion_failure_reason = _coerce_string_to_expected_type(actual, list)
                if parsed is not None:
                    logger.warning(
                        "STORYBOARD_SHAPE_COERCED segment=%s key=beats — value was a "
                        "JSON-encoded string instead of a native list; parsed and "
                        "substituted in place",
                        segment_label,
                    )
                    storyboard["beats"] = parsed
                    return
                logger.error(
                    "STORYBOARD_SHAPE_COERCION_FAILED segment=%s key=beats reason=%s "
                    "value_length=%d",
                    segment_label, coercion_failure_reason, len(actual),
                )

            logger.error(
                "STORYBOARD_SHAPE_INVALID segment=%s key=beats issue=wrong_type "
                "expected=list got=%s value=%r",
                segment_label, type(actual).__name__,
                actual if not isinstance(actual, str) else actual[:200],
            )
            raise ValueError(
                f"storyboard_batch key 'beats' expected list, got {type(actual).__name__}"
            )

    _t0 = time.monotonic()
    storyboard, usage = _run(target_beat_count)
    _elapsed_ms1 = int((time.monotonic() - _t0) * 1000)
    output_tokens = usage.get("output_tokens", 0)
    _output_tokens1 = output_tokens
    _input_tokens1 = usage.get("input_tokens", 0)

    _was_truncated = False
    _attempt_count = 1
    _total_input_tokens = _input_tokens1
    _total_elapsed_ms = _elapsed_ms1

    if output_tokens >= STORYBOARD_BATCH_MAX_TOKENS:
        # Truncated — Claude hit max_tokens mid-response; the forced tool-use input
        # will be {} (empty). Retry once with a reduced beat count to free token budget.
        _was_truncated = True
        reduced = max(1, target_beat_count - _TRUNCATION_BEAT_REDUCTION)
        logger.warning(
            "Storyboard truncated: segment=%s output_tokens=%d == max_tokens=%d "
            "— retrying with target_beat_count %d → %d",
            segment_label, output_tokens, STORYBOARD_BATCH_MAX_TOKENS,
            target_beat_count, reduced,
        )
        _t1 = time.monotonic()
        storyboard, usage = _run(reduced)
        _elapsed_ms2 = int((time.monotonic() - _t1) * 1000)
        output_tokens = usage.get("output_tokens", 0)
        _input_tokens2 = usage.get("input_tokens", 0)
        _attempt_count = 2
        _total_input_tokens += _input_tokens2
        _total_elapsed_ms += _elapsed_ms2
        _extra_cost_pct = (_input_tokens1 + _input_tokens2) / max(_input_tokens1, 1) * 100 - 100
        logger.warning(
            "STORYBOARD_RETRY_COST segment=%s attempt1_target_beats=%d "
            "attempt1_output_tokens=%d truncated=True retry_target_beats=%d "
            "retry_output_tokens=%d estimated_extra_cost_percent=%.0f "
            "elapsed_ms_attempt1=%d elapsed_ms_attempt2=%d",
            segment_label, target_beat_count,
            _output_tokens1, reduced, output_tokens,
            _extra_cost_pct, _elapsed_ms1, _elapsed_ms2,
        )
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

    try:
        _check_shape(storyboard)
    except ValueError:
        # One bounded retry for a structurally-malformed (not truncated) response —
        # e.g. Claude returning a required array field as a string on an otherwise
        # complete, non-truncated tool-use call. This is a different failure class
        # from truncation (above) and gets its own retry rather than falling back
        # to the truncation branch's reduced-beat-count logic, which would not help.
        logger.warning(
            "STORYBOARD_SHAPE_RETRY segment=%s — malformed response shape, "
            "retrying this segment once with the same target_beat_count=%d",
            segment_label, target_beat_count,
        )
        _t2 = time.monotonic()
        storyboard, usage = _run(target_beat_count)
        _elapsed_ms3 = int((time.monotonic() - _t2) * 1000)
        _attempt_count += 1
        _total_input_tokens += usage.get("input_tokens", 0)
        _total_elapsed_ms += _elapsed_ms3
        _check_shape(storyboard)  # raises (fail-loud) if the retry is also malformed

    diag: dict = {
        "was_truncated": _was_truncated,
        "attempt_count": _attempt_count,
        "input_tokens":  _total_input_tokens,
        "elapsed_ms":    _total_elapsed_ms,
    }
    return storyboard, usage, diag
