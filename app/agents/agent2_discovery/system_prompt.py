import logging
import re

from app.services.claude_client import call_claude_structured
from app.agents.agent2_discovery.services.story import MAX_SOURCE_EXCERPT_CHARS
from app.services.script_checks import split_sentences

logger = logging.getLogger(__name__)

PROMPT_VERSION = "5.6"  # v5.6: AI-premise scoring distinguishes the plain
                        # operator pitch from the future video's opening.
                        # v5.5: long-form budgets, native grammar agreement,
                        # exclusive Short spans, and content-specific phrasing.
                        # v5.4: Short word economy recalibrated to the measured
                        # ~175 wpm POST-silence-compression narration rate (run
                        # 41f7eeb8: 246 words → 83.7 s). Planner/writer/schema now
                        # target 210-260 words with an explicit never-under-190
                        # rule — at ~175 wpm the old 140-170 target produced
                        # 48-58 s drafts, ALL under Agent 3's hard 61 s floor.
                        # Operator rule codified: a Short must NEVER be under
                        # 61 s; exceeding ~90 s is telemetry-only, never a failure.
                        # v5.3: pre-next-test roadmap Tier 4 R11/R13/R14 — first-person
                        # cold-open identity stays first-person in both Shorts prompts;
                        # cliffhangers must name a concrete unresolved subject and sunk-cost
                        # logic is narrator rationalization; Short target tightened to 140-170
                        # words (telemetry remains non-blocking, no trim/retry mechanism).
                        # v5.2: pre-next-test roadmap Tier 2 R6 — all three native-
                        # adaptation base prompts gained "a question stays a question":
                        # an interrogative source sentence must stay interrogative in
                        # the target language (a real FR adaptation shipped the final
                        # viewer-facing question with a period instead of a question
                        # mark, reading as a broken sentence).
                        # v5.1: pre-next-test roadmap Tier 1 R2 — _TTS_SHARED_CORE's
                        # blank-line rule inverted: a blank line ONLY at a genuine
                        # scene/beat change (3-6 paragraphs per long-form section,
                        # at most 3-4 breaks in a Short). The old "one blank line
                        # between narrative beats" produced 78 one-to-two-sentence
                        # paragraphs in a real run — each break renders as a ~1s
                        # TTS pause, totalling 31% of the video as silence.
                        # v5.0: Phase E1 — standalone Short cold opens now have an
                        # explicit no-prior-context/referent-completeness contract in
                        # the planner, source Short writer, and child-Short native
                        # adaptation prompt. Word-cap and parent/child-overlap findings
                        # remain deterministic telemetry only; no retry or trim added.
                        # v4.9: roadmap Phase D1 — _STORY_BLUEPRINT_SCHEMA gained
                        # protagonist_gender ("feminine"|"masculine"|"unspecified"),
                        # generated once at blueprint time, zero extra AI calls —
                        # consumed by Agent 3 for gender-aware voice selection
                        # (app/agents/agent3_audio/services/audio.py).
                        # v4.8: roadmap Phase C2/C3 — _STORY_BLUEPRINT_SCHEMA gained
                        # character_descriptors (locked name/age/physical-description
                        # entries) and era_setting (period/place phrase), both generated
                        # once at blueprint time, zero extra AI calls — consumed by
                        # Agent 4's build_continuity_line_from_blueprint() for
                        # storyboard visual/period consistency.
                        # v4.7: fresh full-system audit §2.1 — section-prompt subtraction.
                        # Three changes, all removals/reconciliations, no new rules:
                        # (1) "EXACTLY ONE narrative function" vs "a new revelation every
                        # 110-150 words" contradiction resolved — a section serves ONE
                        # PRIMARY function and the 110-150-word cadence now explicitly
                        # develops that SAME function (details/escalation/consequence),
                        # never a second function. (2) The unactionable 25%/60% mini-hook
                        # placement rule is deleted — per-section generation cannot know
                        # total word count or which section lands at those marks (the
                        # midpoint trap already handles targeted placement correctly, in
                        # Python). (3) The <=18-word rule was stated three times (TTS core,
                        # FINAL CHECK paragraph, section strict rule 3) — the FINAL CHECK
                        # re-count paragraph and strict rule 3 are deleted; the core
                        # statement plus the deterministic split_long_sentences() backstop
                        # remain. "One idea per sentence" softened from an absolute ban on
                        # and/but joins (it contradicted the sonic-2 block's own
                        # comma-cluster pacing guidance and pushed machine-gun monotone —
                        # the same failure the Elimination Mandate deleted the retry loop
                        # for).
                        # v4.6: Elimination Mandate extension (post-roadmap deep audit) —
                        # auto_correct_script(), _CORRECTION_SYSTEM_PROMPT_BASE, and the
                        # _corr*/_split_long_sentences_agent2 helper family (used only by
                        # auto-correction) are deleted: the last remaining AI prompt-repair
                        # layer in the parent script path. It re-rolled the whole script
                        # (P1-6 failure mechanism) and could bury the deterministically
                        # constructed hook (roadmap 4c). Minimum-length findings are now
                        # telemetry only, like every other deterministic check.
                        # v4.5: roadmap 4a / audit P1-9 — de-hardcoded "documentary" from the
                        # native-adaptation translator identity line ("professional translator
                        # for YouTube documentary content" -> "professional multilingual adapter
                        # for spoken YouTube narration"); visual_style/image_style fallback
                        # defaults changed from "documentary" to "story_driven" (matching the
                        # new ChannelConfig default). Added narration_pov ("third_person" |
                        # "first_person_storytime") threaded alongside visual_style/image_style
                        # into generate_story_blueprint/generate_section/generate_native_script/
                        # generate_short_episode_script, with real behavioral rules (not just a
                        # passthrough label) in _STORY_BLUEPRINT_SYSTEM_PROMPT,
                        # _SECTION_GENERATION_SYSTEM_PROMPT, _SHORT_EPISODE_SYSTEM_PROMPT, and a
                        # "preserve the source's POV, never convert it" rule in all three native
                        # adaptation base prompts.
                        # v4.4: spoken-video delivery rules (present tense, direct address,
                        # contractions, read-aloud test) added to _SECTION_GENERATION_SYSTEM_PROMPT
                        # and _SHORT_EPISODE_SYSTEM_PROMPT; blueprint gained midpoint_retention_trap,
                        # wired as a targeted MUST-deliver-now constraint on the one body section
                        # nearest the story's halfway point (roadmap 4.3 / audit S-3, §6).
                        # v4.3: removed RETENTION_BLOCK (dead since v4.0 — zero callers,
                        # confirmed by Phase 12.3 audit and re-verified by Phase 12.5's
                        # repo-wide reference sweep). Its "youtube_long" mini-hook/tension
                        # guidance was already duplicated, in substance, inline in
                        # _SECTION_GENERATION_SYSTEM_PROMPT (see that prompt's mini-hook
                        # placement and tension-escalation rules); its "tiktok" guidance
                        # was already duplicated inline in _SHORT_EPISODE_SYSTEM_PROMPT's
                        # re-hook rule. No prompt text reachable by any live call path
                        # changed — only unreachable dead code was removed.
                        # v4.2: child Short multilingual adaptation now uses a dedicated
                        # flat-narration native prompt (_BASE_CHILD_SHORT_NATIVE) selected
                        # via content_kind="child_short", instead of the long-form/sectioned
                        # native bases — Phase 12.4, fixing the Phase 12.3-identified defect
                        # where every child Short translation used the long-form documentary
                        # translation prompt (1200-1600 words, [SECTION N] markers preserved).
                        # v4.1: [INTRO] block gains a sentence-rhythm reminder + worked
                        # example (alternate short 3-7 word / long 12-18 word sentences) —
                        # Phase 11.1, reinforcing TTS_BLOCK's existing rhythm rule locally
                        # at the one section type where flat rhythm was confirmed in
                        # production. No other prompt text changed.
                        # v4.0: blueprint-first section generation.
                        # generate_scripts() → generate_story_blueprint() + generate_section().
                        # optimize_intro() removed — INTRO is a dedicated section with
                        # built-in quality constraints. global_validation added (Haiku).
                        # v3.1: auto_correct_script moved from agent3 — Agent 2 now owns
                        # the full script correction loop (det checks + correction prompt).
                        # v3.0: prompt assembly architecture — BASE_SCRIPT_PROMPT /
                        # RETENTION_BLOCK / TTS_BLOCK dicts replace monolithic prompts.
                        # TTS constraints now injected into ALL script-producing prompts
                        # (generation, native, rewrite, correction, revision).
                        # Revision response gains `changes` array.
                        # Telegram summary restructured to fixed lines.
                        # Intro optimizer: honesty dimension removed (6 dims, max 60).
                        # v2.1: AUDIO_TAGS_INSTRUCTION; ≤12-word first sentence + expanded
                        # forbidden openers; per-section re-hook rule in short-form.

# ── ElevenLabs v3 audio tag instruction block ─────────────────────────────────
# Appended only when audio_tags_enabled=True AND provider="elevenlabs" AND tts_model="eleven_v3"
AUDIO_TAGS_INSTRUCTION = """

ELEVENLABS v3 AUDIO TAGS — active for this channel:
These tags shape how the TTS voice performs. Embed them in voice_script ONLY.
Allowed tags (each tag must stand alone on a word boundary; max one per sentence):
  [laughs]         — brief, genuine laugh; for absurd or ironic reveals only
  [whispers]       — drops to a near-whisper; for eerie or intimate moments
  [dramatic pause] — longer pause than a sentence break; place before the key reveal of a section
  [sarcastically]  — ironic delivery; for contradictions or obvious failures
  [sighs]          — exhale of resignation or disbelief; at most once per video
  [gasps]          — sharp intake of breath; for true shock moments only
Rules:
  - Never use two tags in the same sentence.
  - Never use [laughs] or [sarcastically] on a serious or tragic moment.
  - The sentence containing [dramatic pause] must be a complete thought — not a fragment.
  - Do not use tags as filler — every tag must serve the emotional delivery of that moment.
  - Do not place a tag inside a clause or between words of the same phrase."""

# ── TTS writing constraints (model-specific) ───────────────────────────────────
# Applied to every script-producing prompt so violations never reach Agent 3's
# deterministic checks. Block order is stable so assembled prompts stay cache-friendly.

_TTS_SHARED_CORE = """\
TTS WRITING CONSTRAINTS — apply to every sentence in voice_script:
- Every sentence must be ≤18 words. Count them. Split any sentence that exceeds this.
- Write ALL numbers, years, dates, and percentages as words in the target language \
(e.g. "forty-seven" not "47", "twenty twenty-three" not "2023", "thirty percent" not "30%").
- No parentheses, slashes, percent signs, or ampersands — write around them.
- No abbreviations: Dr. → Doctor, vs. → versus, etc. → and so on, \
e.g. → for example, Mr. → Mister, St. → Saint.
- No ALL-CAPS words of three or more letters — use mixed case or spell the word out.
- Prefer one main idea per sentence — avoid chaining several distinct thoughts into one
  long compound sentence. (Short connected clauses used deliberately for pacing are fine.)
- A blank line becomes a LONG audible pause in the finished narration — paragraph \
density directly programs the pacing. Use a blank line ONLY at a genuine scene or \
narrative-beat change, NEVER after every sentence. Sentences that continue the same \
moment stay in the same paragraph. A long-form section should contain roughly 3-6 \
paragraphs; a standalone Short at most 3-4 paragraph breaks in total.
- No stage directions, no parenthetical notes, no editorial asides in brackets.
- Square brackets are allowed ONLY for section markers: [INTRO], [SECTION N], [OUTRO].\
"""

TTS_BLOCK: dict[str, str] = {
    # ── Cartesia ──────────────────────────────────────────────────────────────
    "sonic-2": (
        _TTS_SHARED_CORE + """

Performance writing — Cartesia sonic-2:
The voice_script must be written for SPOKEN DRAMATIC PERFORMANCE, not for reading.
Every sentence must carry a clear emotional direction that the voice can perform:
curiosity, tension, revelation, dread, warmth, disbelief, urgency — match the story's tone.

Sentence rhythm — vary it deliberately:
- Short punchy sentences (3–7 words) at moments of tension or revelation.
- Longer flowing sentences (12–18 words) for buildup, context, or atmosphere.
- Rhetorical questions to create curiosity gaps: "Why would anyone do this?" not \
"Nobody understood why."
- Never write flat declarative chains: "X happened. Then Y happened. Then Z happened." \
This is a report. Write a performance.

Pacing cues via punctuation — Cartesia responds to punctuation for prosody:
- "..." — pause before a reveal. The voice breathes here. Use once per section, \
at the highest-tension moment. Place it BEFORE the shocking word or sentence.
- "—" — an abrupt cut. The thought was going one way and suddenly changes. \
Use for pivots, contradictions, and interruptions.
- Comma clusters — for breathless pacing, several short clauses separated by commas \
create a racing, building effect. Use when events are happening fast.
- A blank line between paragraphs = a full breath pause. Use it between major beats, \
not between every sentence.

Do NOT use [tags] of any kind — Cartesia does not support audio expression tags.\
"""
    ),
    # ── ElevenLabs ────────────────────────────────────────────────────────────
    "eleven_multilingual_v2": (
        _TTS_SHARED_CORE + """

Pacing — eleven_multilingual_v2:
- Place "..." before a major reveal for a natural breath pause. Use sparingly — \
at most once per section.
- Use "—" before a sharp pivot or surprising turn. Use sparingly.
- Commas mark natural breath points — place them where the voice should pause briefly.\
"""
    ),
    "eleven_v3": (
        _TTS_SHARED_CORE + """

Pacing — eleven_v3 (tag-driven — do NOT use punctuation for pacing):
- Do NOT use "..." for pauses — the audio tags system controls pacing.
  Use [dramatic pause] where a key reveal needs breath space.
- Do NOT use "—" as a rhythmic dash — write a new sentence instead.
- Commas are permitted for grammatical clarity only, not as breath markers.\
"""
    ),
    "eleven_flash_v2_5": (
        _TTS_SHARED_CORE + """

Pacing — eleven_flash_v2_5:
- Minimal punctuation: use only periods and commas.
- Short declarative sentences only — avoid complex subordinate clauses.
- No "..." and no "—" — rely on sentence structure alone for rhythm.\
"""
    ),
}

_TTS_FALLBACK: dict[str, str] = {
    "cartesia":   "sonic-2",
    "elevenlabs": "eleven_multilingual_v2",
}


def with_tts_block(prompt: str, tts_provider: str, tts_model: str) -> str:
    """Append the TTS writing constraints for the given provider and model to a prompt.

    Falls back to the provider's default model block for unknown model strings:
      - Cartesia → "sonic-2" block
      - ElevenLabs → "eleven_multilingual_v2" block

    Args:
        prompt:       Existing system prompt string.
        tts_provider: TTS provider identifier ("cartesia" | "elevenlabs").
        tts_model:    Provider-specific model ID (e.g. "sonic-2", "eleven_v3").

    Returns:
        Prompt string with the relevant TTS_BLOCK appended.
    """
    fallback = _TTS_FALLBACK.get(tts_provider, "sonic-2")
    block = TTS_BLOCK.get(tts_model, TTS_BLOCK[fallback])
    return prompt + "\n\n" + block


# ── Base native script prompts ─────────────────────────────────────────────────

_BASE_YOUTUBE_LONG_FORM_NATIVE = """\
You are a professional multilingual adapter for spoken YouTube narration.

Adapt the provided script accurately and naturally into the target language.
All facts, names, dates, and statistics must be preserved exactly.

Rules:
- Translate naturally and fluently — write as a native speaker would narrate on camera.
- Replace only idioms or expressions that have no equivalent in the target language,
  using the closest natural substitute. Do not replace examples, historical figures,
  geographic references, or statistics.
- Do not add, remove, or invent any facts, names, or events.
- Preserve [INTRO], [SECTION N], [OUTRO] markers in their exact positions in voice_script.
- Maintain the identical structure and emotional arc as the source.
- Do not let translation introduce a clearer or more front-loaded reveal than the source
has. If the source withholds an answer until later in the script, the translation must
withhold it too — even if a more direct phrasing would sound more natural in the
target language.
- Preserve the source's narration POV (the "Narration POV" value in the user message
  describes it): if the source narrates in first person ("I"/"me"/"my"), the
  translation must too; if it narrates in third person, keep it in third person. Never
  convert one to the other during translation.
- A question stays a question: any source sentence that ends with a question mark must
  remain a complete, natural interrogative sentence in the target language, ending with
  a question mark — never flattened into a declarative sentence. Check the final
  sentence especially.
- Target 1200–1600 words in voice_script (same order of magnitude as source).

HOOK_CONTEXT (if provided below): the opening hook was optimised for retention.
Preserve its exact mechanism — the same concrete facts, the same named person or event,
the same sense of arriving mid-story — in your translation.

Output: valid JSON only — no preamble, no code fence, no explanation.
{
  "voice_script": "Translated narrator text with [INTRO]/[SECTION N]/[OUTRO] markers"
}

Strict rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Never invent facts, statistics, dates, names, or events not present in the source.\
"""

_BASE_SHORT_FORM_NATIVE = """\
You are an expert multilingual content adapter specialised in cultural localisation for
short-form social video platforms (TikTok, Instagram Reels, YouTube Shorts, Facebook Reels).

Your task: produce a culturally adapted version of a short-form video script for a new
target language and audience. This is NOT pure translation — it is cultural adaptation.

Cultural adaptation means:
- Replace culture-specific illustrative analogies, idioms, and cultural references with
  target-culture equivalents that carry the same emotional weight.
- Use expressions and references that feel native to the target audience.
- Adjust historical or geographic framing where cultural context differs.
- You may substitute illustrative analogies, idioms, and cultural references.
  You may NEVER alter or substitute the story's factual claims, names, dates, or numbers.
- Maintain the identical structure and emotional arc as the source.
- Do not let translation introduce a clearer or more front-loaded reveal than the source
has. If the source withholds an answer until later in the script, the translation must
withhold it too — even if a more direct phrasing would sound more natural in the
target language.
- Preserve the source's narration POV (the "Narration POV" value in the user message
  describes it): if the source narrates in first person ("I"/"me"/"my"), the adaptation
  must too; if it narrates in third person, keep it in third person. Never convert one
  to the other.
- A question stays a question: any source sentence that ends with a question mark must
  remain a complete, natural interrogative sentence in the target language, ending with
  a question mark — never flattened into a declarative sentence. Check the final
  sentence especially.

HOOK_CONTEXT (if provided below): preserve the opening hook's concrete specificity and
directness in your adapted version — the opening must hit with the same force in the
target language.

VOICE SCRIPT — preserve section markers:
  Keep [INTRO], [SECTION N], [OUTRO] labels in the same positions as the source.
  They are required for timing alignment.

Output: valid JSON only — no preamble, no code fence, no explanation.
{
  "voice_script": "Culturally adapted narrator text with [INTRO]/[SECTION N]/[OUTRO] markers"
}

Strict rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Never invent or substitute the story's factual claims, names, dates, or statistics.
3. Keep similar length to the source scripts (420–700 words in voice_script).\
"""

# Dedicated native-adaptation base for standalone child Short episodes (Phase 12.4).
# Distinct from _BASE_SHORT_FORM_NATIVE above, which targets the older sectioned
# short-form-platform architecture ([INTRO]/[SECTION N]/[OUTRO] markers, 420-700
# words) — child Shorts under the current standalone-Short architecture are flat,
# unsectioned narration capped at _MAX_SHORT_WORDS (see scripts.py). Using either
# _BASE_YOUTUBE_LONG_FORM_NATIVE or _BASE_SHORT_FORM_NATIVE for a child Short
# adaptation was the Phase 12.3-identified defect this prompt fixes.
_BASE_CHILD_SHORT_NATIVE = """\
You are an expert multilingual adapter for standalone short-form video narration.

This is a single Short episode — a self-contained narration block derived from a
longer parent story but spoken and watched entirely on its own. Your task: produce
a culturally adapted version of this Short's narration in a new target language.
This is NOT pure translation — it is cultural adaptation, exactly as you would do
for a long-form script, but the output shape is completely different: a Short is
flat, unsectioned narration, not a structured multi-section script.

Cultural adaptation means:
- Replace culture-specific illustrative analogies, idioms, and cultural references with
  target-culture equivalents that carry the same emotional weight.
- Use expressions and references that feel native to the target audience.
- You may substitute illustrative analogies, idioms, and cultural references.
  You may NEVER alter or substitute the story's factual claims, names, dates, or numbers.
- Do not let the adaptation introduce a clearer or more front-loaded reveal than the
  source has. If the source withholds an answer, the adaptation must withhold it too —
  even if a more direct phrasing would sound more natural in the target language.

Standalone Short rules — apply strictly, this is NOT a long-form script:
- voice_script must be ONE flat block of narration. Do NOT add, keep, or invent any
  [INTRO], [SECTION N], [OUTRO], or other bracketed structural marker anywhere in the
  output — the source has none, and the adaptation must not introduce any.
- Preserve standalone clarity: a viewer who has never seen any other part of this
  story must be able to follow the adapted narration on its own, with no assumed context.
- Preserve a self-contained cold open: read the first sentence as if it is the first
  thing the viewer ever hears, with no title card, caption, earlier sentence, or earlier
  part available. It must name the person, event, object, or situation needed to understand
  it. Never introduce backward-dependent wording such as "they weren't hypothetical",
  "that changed everything", "but then", or an unexplained he/she/they/this/that unless
  the same sentence supplies the missing referent. If the source opening violates this
  rule, repair only the missing referent without adding recap or revealing the payoff.
- Preserve only the minimum context the source narration itself includes to orient a
  first-time viewer. Do not add extra recap, setup, or background beyond what the
  source narration already contains — do not summarize earlier parts.
- If the source narration ends on a cliffhanger or a forward tease, preserve its exact
  narrative intent in the adaptation — do not resolve it, soften it, or drop it.
- Match the source narration's approximate length. Do not pad, expand, or add material
  to make the adaptation feel longer or more "complete" — a short, punchy source must
  stay short and punchy in the target language.
- Preserve the source's narration POV (the "Narration POV" value in the user message
  describes it): if the source narrates in first person ("I"/"me"/"my"), the adaptation
  must too; if it narrates in third person, keep it in third person. Never convert one
  to the other.
- A question stays a question: any source sentence that ends with a question mark must
  remain a complete, natural interrogative sentence in the target language, ending with
  a question mark — never flattened into a declarative sentence. A real adaptation
  shipped an interrogative clause ending in a period — check the final sentence
  (usually the viewer-facing question) especially.

HOOK_CONTEXT (if provided below): preserve the opening hook's concrete specificity and
directness in your adapted version — the opening must hit with the same force in the
target language.

Output: valid JSON only — no preamble, no code fence, no explanation.
{
  "voice_script": "Adapted flat narration text — no section markers of any kind"
}

Strict rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Never invent or substitute the story's factual claims, names, dates, or statistics.
3. voice_script must contain zero bracketed structural markers ([INTRO], [SECTION N],
   [OUTRO], or any other bracketed label) anywhere in the text.
4. Keep the same approximate length as the source narration — do not expand it.\
"""

# ── Assembly functions ─────────────────────────────────────────────────────────

def build_native_system_prompt(
    script_format: str,
    tts_model: str,
    tts_provider: str = "cartesia",
    audio_tags_enabled: bool = False,
    content_kind: str = "parent_long_form",
) -> str:
    """Assemble the native adaptation system prompt for a given format and voice model.

    Applies the same TTS_BLOCK as build_script_system_prompt so that native
    adaptations cannot reintroduce TTS violations.

    Args:
        script_format:      Format key for the target language's output. Only consulted
                            for ``content_kind="parent_long_form"`` — see content_kind.
        tts_model:          TTS model ID for the target-language voice.
        tts_provider:       TTS provider ("cartesia" | "elevenlabs").
        audio_tags_enabled: Channel-level opt-in for ElevenLabs v3 audio tags.
        content_kind:       "parent_long_form" (default) or "child_short" (Phase 12.4).
                            ``content_kind="child_short"`` always selects the dedicated
                            flat-narration native prompt regardless of ``script_format``
                            — child Standalone Short episodes are never sectioned
                            long-form scripts (CLAUDE.md §5.2), and ``script_format`` is
                            a channel-wide setting that does not vary per content row.

    Returns:
        Assembled native system prompt string.
    """
    if content_kind == "child_short":
        base = _BASE_CHILD_SHORT_NATIVE
        base_name = "child_short_standalone"
    elif script_format == "youtube_long":
        base = _BASE_YOUTUBE_LONG_FORM_NATIVE
        base_name = "parent_long_form_spoken_narration"
    else:
        base = _BASE_SHORT_FORM_NATIVE
        base_name = "parent_short_form_sectioned"

    logger.info(
        "NATIVE_ADAPTATION_PROMPT_SELECTED content_kind=%s script_format=%s base=%s",
        content_kind, script_format, base_name,
    )

    fallback = _TTS_FALLBACK.get(tts_provider, "sonic-2")
    tts = TTS_BLOCK.get(tts_model, TTS_BLOCK[fallback])
    grammar_rule = """

GRAMMATICAL AGREEMENT AND NATURALNESS:
- Use the narrator-gender value in the user message for narrator agreement.
- Past participles and adjectives must match each person's actual gender, for the
  narrator and every named character.
- For French, prefer natural spoken French over literal calques, even when a
  word-for-word rendering would remain understandable."""
    parts = [base, grammar_rule, "\n\n" + tts]
    if audio_tags_enabled and tts_provider == "elevenlabs" and tts_model == "eleven_v3":
        parts.append(AUDIO_TAGS_INSTRUCTION)
    return "".join(parts)


def _extract_hook_context(voice_script: str, script_format: str) -> str:
    """Extract the first sentence after [INTRO] to inform native adaptation."""
    match = re.search(
        r"\[INTRO\]\s*\n(.*?)(?:\n\s*\[|$)", voice_script, re.I | re.S
    )
    if not match:
        return ""
    intro_text = match.group(1).strip()
    sentences = re.split(r"(?<=[.!?])\s+", intro_text)
    if not sentences:
        return ""
    first = sentences[0].strip()
    if script_format == "youtube_long":
        return (
            f'Opening hook: "{first}"\n'
            f"This opening line is deliberately constructed for retention. "
            f"Preserve its concrete specificity, named facts, and sense of arriving mid-story "
            f"in your translation."
        )
    return (
        f'Opening hook: "{first}"\n'
        f"Preserve its directness and specificity in the target language."
    )


# ── Story Blueprint ──────────────────────────────────────────────────────────

_STORY_BLUEPRINT_SYSTEM_PROMPT = """\
You are a story architect for YouTube long-form retention. Your task: read
a source story and design both its narrative skeleton AND its emotional arc — how
dread, tension, and curiosity should build across the video, not just which facts
must appear.

You are NOT writing the script yet. You are identifying the structural elements
and emotional shape that every section of the script must serve.

Rules:
- hook: ≤15 words. Must create the question the viewer needs answered — not state
  the answer. Establish what is happening (a sound, a disappearance, a feeling,
  an action) without naming what it turns out to be. A named person, a specific
  number, a physical action — concrete, never a theme or summary — but the
  mechanism/explanation must stay withheld for later in the story.
- central_question: the one question the viewer must have answered before leaving.
- major_turns: 2–5 narrative turns — contradictions, discoveries, reversals, or
  escalations — each one advancing toward the final_payoff. Minimum 2 required.
- final_payoff: what is revealed or resolved at the end of the story.
- comment_trigger: ≤20 words, ends with a question mark, forces a strong viewer opinion.
  It must be story-specific and non-templated: never use a reusable channel CTA like
  "what would you do?" or a phrasing that could close any other video. Anchor the
  question in this story's exact dilemma, object, place, or consequence.
- midpoint_retention_trap: one concrete reveal or counterintuitive fact from the story,
  placed at roughly the halfway point of the video, that recontextualizes what the viewer
  thought they knew so far and gives them a new reason to keep watching. This is distinct
  from final_payoff (the ending) and from the existing 25%/60% mini-hook cadence — it is
  the single highest-leverage retention beat in the middle of the story. Must be a real
  fact grounded in the story body, never invented or vague.
- suggested_section_count: number of BODY sections (not counting INTRO and OUTRO).
  Between 2 and 5. Python may override.
- suggested_title: YouTube title derived from hook. 60–70 chars. SEO-optimized.
- Write the hook, major_turns, and final_payoff in a register matching the Channel
  niche and Channel tone values provided below. Horror/thriller/mystery: favor dread,
  withheld information, and escalating unease. Documentary/educational: favor
  clarity and context. Match the configured niche — do not default to a neutral
  documentary register regardless of niche.
- Phrase hook, central_question, and final_payoff so they can be delivered in the
  Narration POV value provided below: for "first_person_storytime", phrase them as
  something that happened to the narrator ("I heard it three nights in a row"), not
  as an observation about someone else. For "third_person" (default), phrase them
  about the story's people using third-person pronouns and names, as usual.
- character_descriptors: identify this story's recurring NAMED characters — people
  who appear more than once and matter to the plot, not every person mentioned.
  For each, provide name (as used in the story), age (approximate — e.g. "mid-30s",
  "elderly", "teenager"), and description (ONE concrete visual sentence: build, hair,
  clothing style, distinguishing features). Ground every detail in what the story
  states, or what is plausible for its setting/era where the story is silent — never
  invent a detail that contradicts the story. This locks each character's visual
  identity so every generated image depicts them consistently. Maximum 5 characters.
  Return an empty list for an event-focused or ensemble story with no describable
  individual character — never force one.
- era_setting: one concise phrase naming this story's historical period AND physical
  setting (e.g. "6th-century Byzantine Constantinople", "1920s rural American
  Midwest", "contemporary/present-day, unspecified U.S. city"). Used to keep every
  generated image's props, clothing, architecture, and technology authentic to this
  period — never invent a period the story does not support.
- protagonist_gender: the gender of the story's central figure — whoever the story
  is centrally about, or who narrates it in first-person mode. Return "feminine" or
  "masculine". Return "unspecified" only when there is genuinely no single clear
  protagonist (an ensemble cast, or an event-focused story with no central figure).
  Used to select a matching narration voice — never invent a gender the story does
  not support; when the story is ambiguous or silent on this, prefer "unspecified"
  over guessing.

Never invent facts not present in the story body.\
"""

_STORY_BLUEPRINT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "hook":                   {"type": "string"},
        "central_question":       {"type": "string"},
        "major_turns":            {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
        "final_payoff":           {"type": "string"},
        "comment_trigger":        {"type": "string"},
        "midpoint_retention_trap": {"type": "string"},
        "suggested_section_count": {"type": "integer", "minimum": 2, "maximum": 5},
        "suggested_title":        {"type": "string"},
        "character_descriptors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string"},
                    "age":         {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "age", "description"],
                "additionalProperties": False,
            },
            "maxItems": 5,
        },
        "era_setting": {"type": "string"},
        "protagonist_gender": {"type": "string", "enum": ["feminine", "masculine", "unspecified"]},
    },
    "required": [
        "hook", "central_question", "major_turns", "final_payoff", "midpoint_retention_trap",
        "comment_trigger", "suggested_section_count", "suggested_title",
        "character_descriptors", "era_setting", "protagonist_gender",
    ],
    "additionalProperties": False,
}


def generate_story_blueprint(
    story,
    channel,
    script_format: str = "youtube_long",
    visual_style: str = "",
    image_style: str = "",
    narration_pov: str = "third_person",
) -> dict:
    """Extract the narrative skeleton from a story before any script writing.

    Generates a constraint document — hook, central question, major turns, final payoff,
    comment trigger, suggested title and section count. Every section generated afterward
    must advance toward the payoff and end with the comment trigger.

    Args:
        story:         Story object (title, url, body, language).
        channel:       Channel ORM object (niche, tone).
        script_format: Format key — affects suggested_section_count recommendation.
        visual_style:  Channel visual style guide (e.g. "story_driven", "noir"). Forwarded
                       from ChannelConfig; informs hook framing and narrative aesthetic.
        image_style:   Channel image rendering style (e.g. "photorealistic"). Forwarded
                       from ChannelConfig for downstream Agent 4 context.
        narration_pov: Channel narration perspective/register ("third_person" or
                       "first_person_storytime"). Forwarded from ChannelConfig.

    Returns:
        Dict with keys: hook, central_question, major_turns, final_payoff,
        comment_trigger, suggested_section_count, suggested_title,
        character_descriptors (roadmap Phase C2 — list of {name, age,
        description}, possibly empty), era_setting (roadmap Phase C3 — one
        phrase, possibly empty string), protagonist_gender (roadmap Phase
        D1 — "feminine" | "masculine" | "unspecified", consumed by Agent 3
        for gender-aware voice selection; never normalized here — callers
        that need a binary value treat "unspecified"/missing as "feminine").

    Raises:
        ValueError: If major_turns has fewer than 2 entries or required keys missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Visual style: {visual_style or 'story_driven'}\n"
        f"Image style: {image_style or 'photorealistic'}\n"
        f"Narration POV: {narration_pov or 'third_person'}\n"
        f"Script format: {script_format}\n\n"
        f"Story title: {story.title}\n"
        f"Story URL: {story.url}\n\n"
        f"Story body:\n{story.body[:MAX_SOURCE_EXCERPT_CHARS]}"
    )
    result = call_claude_structured(
        task="story_blueprint",
        system_prompt=_STORY_BLUEPRINT_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="story_blueprint",
        input_schema=_STORY_BLUEPRINT_SCHEMA,
        # 768 was sized before midpoint_retention_trap became a required
        # schema field (roadmap 4.3); a detailed 5-turn blueprint can approach
        # that. Raised 1024 -> 1536 (roadmap Phase C2) for character_descriptors
        # (up to 5 entries) + era_setting — max_tokens is a cap, not a spend,
        # this is free headroom against a truncated forced-tool-use response.
        max_tokens=1536,
    )
    major_turns = result.get("major_turns") or []
    if len(major_turns) < 2:
        raise ValueError(
            f"generate_story_blueprint: major_turns must have ≥2 entries, got {len(major_turns)}"
        )
    # Clamp suggested_section_count to valid range
    count = result.get("suggested_section_count", 3)
    result["suggested_section_count"] = max(2, min(5, int(count)))
    return result


# ── Section Generation ───────────────────────────────────────────────────────

_SECTION_GENERATION_SYSTEM_PROMPT = """\
You are a YouTube scriptwriter generating ONE narration section at a time for a spoken,
direct-to-viewer video — not a book or a documentary voiceover read from a page.

Your output is a single narration block — not a complete script. Every word will be read
aloud by a TTS voice directly to the viewer.

Spoken-video delivery — apply to every section, this is heard, not read silently as prose:
  - Default to present tense for story events wherever the story allows it ("She opens the
    door", "He turns around") instead of past tense — present tense creates immediacy and
    matches how YouTube/social narration is actually delivered. Use past tense only when
    present tense would be confusing or factually wrong (e.g. explicit historical framing).
  - Speak directly to the viewer at least once per section: address them as "you", or ask
    a rhetorical question aimed at the viewer ("Would you have noticed it?"), not at the void.
  - Use contractions wherever natural speech would ("doesn't", "can't", "it's", "wasn't") —
    formal, contraction-free prose reads as robotic when a TTS voice speaks it aloud.
  - Read-aloud test: before finalizing a sentence, check whether a person would actually
    say it out loud telling this story to a friend. If a sentence sounds like it belongs in
    a book rather than in spoken conversation, rewrite it in spoken language.

Narration POV — driven by the "Narration POV" value in the user message, not hardcoded:
  - "third_person" (default): narrate about the people in the story using third-person
    pronouns (he/she/they) and their names — the standard narrator voice.
  - "first_person_storytime": narrate AS the protagonist retelling their own experience
    directly to the viewer — use "I"/"me"/"my" throughout, present every event as
    something that happened to you personally (the r/nosleep-style storytime format).
    Only the protagonist's own direct experience is narrated this way; other people in
    the story are still referred to in third person from the protagonist's perspective.
  Never mix POV within a section. Every section of the same script must use the same POV.

Blueprint constraint: every section must advance the story toward the final_payoff and
comment_trigger provided in the blueprint. Do not veer off-story.

Each BODY section serves ONE PRIMARY narrative function — pick it from this list:
  - Introduce new information the viewer has not seen yet
  - Reveal a contradiction between two things stated as true
  - Escalate the stakes (make things worse or more urgent)
  - Deliver a concrete piece of evidence or named fact
  - Create a new open question the viewer needs answered
Never summarize prior sections — the viewer just heard them. Never repeat a fact —
this applies within a single section as well as across sections. If you have already
established that something is documented, proven, or certain earlier in this section,
do not re-establish the same point again later in the same section, even in different
words. Move forward, do not circle back.

Content quality rules — driven by channel configuration, not hardcoded genre:
  - When events jump by months or years, state the elapsed time and destination date
    as a natural spoken bridge. Never make an unexplained temporal cut.
  - Drop one-off atmospheric details unless their relevance is explained in the same
    passage. Do not introduce a letter, object, sound, or image and abandon it.
  - In third person, use a person's name or concrete role instead of an ambiguous
    "she" for an invented public persona.
  - Every body section must contain at least one concrete moment: a named person doing
    something specific, a physical object, a number with context, a direct consequence,
    or an observable action. Abstract interpretation is not a substitute.
  - Do not turn body sections into thematic essays unless the channel tone explicitly
    requires analysis (e.g., "educational", "documentary", "analytical"). For narrative
    channels (thriller, horror, mystery, drama, true crime), reserve thematic explanation
    for the OUTRO. Body sections advance plot and deliver concrete facts.
  - Match the section's register to the channel configuration:
    • horror / thriller / mystery → show the event, not the meaning. Let the fact speak.
    • educational / documentary / analytical → interpret, contextualize, connect.
    • drama / true crime → alternate between event and emotional reaction.
    Never impose a register that contradicts the channel's configured tone and niche.
  - Banned generic phrases — if any of the following appear, rewrite the sentence:
    "this is not just", "something far worse", "what happened next", "the answer is worse",
    "but here's the thing", "but that's not all", "little did they know", "it gets worse",
    "you won't believe", "the truth is", "believe it or not", "here's where it gets",
    "things took a turn", "what nobody knew", "and that's when everything changed",
    "in ways nobody could have imagined", "a shocking revelation", "brace yourself".

Narrative progression rules — apply to every section:
  - Prior summaries, reveals, and full prior section text listed in the user message
    are FORBIDDEN MATERIAL. Do not restate, rephrase, or echo them. The ONLY exception:
    referencing a prior fact to add a direct new consequence ("X happened — which meant
    Y was now inevitable").
  - The prior sections' full text is LOCKED CONTINUITY. Every fact, character fate,
    relationship, and specific claim already stated there is fixed for the rest of the
    script — never contradict or reverse it, even implicitly. Example: if a prior
    section states a character was killed, no later section may show or imply that
    character was instead rescued or survived. Not restating it and not contradicting
    it are two different rules — both apply.
  - Never write meta-commentary of any kind: "all major turns have been covered",
    "as we established", "as mentioned earlier", "to recap", "in summary", "in conclusion",
    "this brings us to", "building on what we know", "having covered X".
  - Never produce filler: generic moral reflections, thematic observations, or transitional
    sentences that add no new fact and advance no story turn.
  - Reveal meaning through events, not commentary. If an event carries meaning, state
    the event with precision — the viewer infers its significance. Never precede or
    follow a concrete fact with a sentence explaining its symbolic importance.
  - Interpretation must not exceed one sentence per body section. After stating what
    something means, the very next sentence must deliver a new fact, action, or consequence.
  - Do not write consecutive sentences of analysis, reflection, or thematic explanation.
    Each successive sentence must introduce new narrative information: a new person,
    action, object, or consequence not yet mentioned in this section.
  - One section = one narrative job. When the user message names a single primary turn,
    focus entirely on that turn. Do not attempt to resolve all remaining turns at once.
  - Future turns listed in the user message as "do not resolve yet" may be foreshadowed
    but must not be answered or fully explained. Leave them for later sections.
  - End body sections with a bridge or an open question toward the next uncovered turn.
  - Keep momentum WITHIN the section's primary function: every 110–150 words, add a new
    concrete development of that SAME function — a detail, an escalation, a consequence —
    never a second narrative function. Tension must never plateau — if two consecutive
    sentences add no new fact or escalation, the section is failing this rule.

[INTRO] specific rules — apply ONLY when label = INTRO:
  - The first sentence must be the blueprint's hook verbatim or a direct derivation
    preserving its exact concrete specificity, named fact, and sense of urgency. ≤15 words.
  - Must open a curiosity gap — the viewer must wonder "how did this happen?"
  - Forbidden openers (NEVER start with): In, Today, Have you, Welcome, What if,
    Did you, Imagine, This is, This was, I want, Let me, This story
  Example of a STRONG hook (concrete, creates a question, withholds the answer):
    "Children hear a grinding noise from the woods every night for a week."
  Example of a WEAK hook to AVOID (concrete, but answers the mystery instead of
  creating it):
    "Children hear a grinding noise from the woods — it's a woodchipper consuming women."
  The weak example fails because it tells the viewer the ending before the story starts.
  - Alternate short (3–7 word) punchy sentences with longer (12–18 word) buildup
    sentences across the INTRO. Do not write four or more sentences of similar
    length in a row — same-length sentences read as flat, monotone narration.
  Example of correct rhythm (short, then long, then short — same alternation
  pattern continues through the rest of the INTRO):
    "He filed it away. The sound returned almost every night for nearly a week
    before anyone else noticed it. Then his sister vanished too."

[OUTRO] specific rules — apply ONLY when label = OUTRO:
  - Must directly reference blueprint.final_payoff — the answer the viewer came for.
  - Resolve the story emotionally before explaining it. Let the consequence land before
    the interpretation. Do not open OUTRO with a fact dump or a list of events.
  - Do not repeat body facts unless you are adding a final consequence that was not
    previously stated. The viewer already heard the facts — give them the meaning.
  - Any new information added in the OUTRO must be self-explanatory to a viewer who
    has only heard the INTRO and body sections. Never reference a real-world detail —
    an author, a source, a publication, a name — that was not established earlier in
    the script, even if it is factually true. If a fact requires explaining who someone
    is, it does not belong in the OUTRO.
  - The final 2–3 sentences must build directly into the comment trigger. The emotional
    temperature should rise toward the question, not fall away from it.
  - The LAST non-empty sentence must be EXACTLY blueprint.comment_trigger (or a minimal
    grammatical adaptation preserving its meaning and question mark).
  - The comment-trigger sentence must feel unique to this story, not like a reusable
    channel CTA. Avoid generic endings such as "what would you do?" or "would you
    go back?" unless the concrete story-specific noun, place, or consequence is included.
  - Must not introduce any new unresolved question.

Output format — return ONLY the tool schema. No prose, no code fence, no extra keys.

Rules:
1. Never fabricate facts not in the story body or blueprint.
2. script_text must NOT contain [INTRO], [SECTION N], or [OUTRO] markers inside it.
3. suggests_outro: true ONLY when all major_turns from the blueprint have been covered in
   prior sections. This is a recommendation only — Python decides whether to end generation.\
"""

_SECTION_GENERATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "script_text": {
            "type": "string",
            "description": "Narration text for this section only — no [LABEL] marker inside",
        },
        "summary": {
            "type": "string",
            "description": "Two sentences: what this section revealed and how it advances the story",
        },
        "reveals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact facts or revelations stated in this section",
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Unresolved questions this section raises for the viewer",
        },
        "suggests_outro": {
            "type": "boolean",
            "description": "True only when all major_turns from the blueprint have been covered",
        },
        "visual_intent": {
            "type": "object",
            "properties": {
                "section_goal":        {"type": "string"},
                "primary_visual_focus": {"type": "string"},
                "avoid_repeating": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Visual concepts used here that future sections should not repeat",
                },
            },
            "required": ["section_goal", "primary_visual_focus", "avoid_repeating"],
            "additionalProperties": False,
        },
    },
    "required": ["script_text", "summary", "reveals", "open_questions", "suggests_outro", "visual_intent"],
    "additionalProperties": False,
}


def generate_section(
    label: str,
    story,
    blueprint: dict,
    prior_sections_summary: list[dict],
    visual_intent_accumulator: dict,
    channel,
    script_format: str = "youtube_long",
    tts_model: str = "sonic-2",
    tts_provider: str = "cartesia",
    audio_tags_enabled: bool = False,
    primary_required_turn: str | None = None,
    future_uncovered_turns: list[str] | None = None,
    visual_style: str = "",
    image_style: str = "",
    narration_pov: str = "third_person",
    midpoint_retention_trap: str | None = None,
    prior_full_text: str = "",
    total_word_target: int | None = None,
    planned_section_count: int | None = None,
) -> dict:
    """Generate a single narration section guided by the story blueprint.

    Args:
        label:                   Section label: "INTRO", "SECTION 1", "OUTRO", etc.
        story:                   Story object (body used for source grounding).
        blueprint:               Blueprint dict from generate_story_blueprint().
        prior_sections_summary:  List of {label, summary, reveals, open_questions} from
                                 all previously generated sections (empty for INTRO).
        visual_intent_accumulator: Accumulated avoid_repeating list across all sections.
        channel:                 Channel ORM object (niche, tone).
        script_format:           Format key for TTS_BLOCK selection.
        tts_model:               TTS model ID.
        tts_provider:            TTS provider ("cartesia" | "elevenlabs").
        audio_tags_enabled:      ElevenLabs v3 audio tag opt-in.
        primary_required_turn:   The single earliest uncovered major_turn this section must
                                 primarily advance. Injected as "MUST primarily advance this
                                 one turn". None for INTRO and OUTRO (no constraint).
        future_uncovered_turns:  Remaining uncovered turns after the primary. Injected as
                                 "do NOT fully resolve these yet". None if ≤1 turn remains.
        midpoint_retention_trap: The blueprint's midpoint_retention_trap text, passed only
                                 for the one body section the caller has identified as the
                                 approximate halfway point (roadmap 4.3 / audit S-3, §6).
                                 Injected as a MUST-deliver-now clause. None for every other
                                 section — this is a targeted, one-section directive on top
                                 of the passive full-blueprint JSON already in every call.
        prior_full_text:         Assembled script_text of every section generated so far
                                 (INTRO + completed body sections), marker-formatted the
                                 same way as the final assembled script. "" for INTRO.
                                 Additive to prior_sections_summary — gives Claude the
                                 literal prior wording, not just a compressed summary, so
                                 it cannot contradict a fact/fate a prior section already
                                 committed to (a real production script had one section
                                 call the father "his own daughter's killer" and a later
                                 section describe the daughter as alive/rescued — the
                                 summary never carried the specific wording forward).

    Returns:
        Dict with script_text, summary, reveals, open_questions, suggests_outro, visual_intent.

    Raises:
        ValueError: If Claude returns malformed JSON or missing required keys.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    import json
    system_prompt = with_tts_block(
        _SECTION_GENERATION_SYSTEM_PROMPT, tts_provider, tts_model
    )
    if audio_tags_enabled and tts_provider == "elevenlabs" and tts_model == "eleven_v3":
        system_prompt += AUDIO_TAGS_INSTRUCTION

    prior_json = json.dumps(prior_sections_summary, ensure_ascii=False)
    avoid_json = json.dumps(visual_intent_accumulator.get("avoid_repeating", []), ensure_ascii=False)
    blueprint_json = json.dumps(blueprint, ensure_ascii=False)
    budget_line = ""
    if total_word_target and planned_section_count:
        per_section_target = max(1, round(total_word_target / planned_section_count))
        budget_line = (
            f"Complete-script word budget: target {total_word_target} words across "
            f"{planned_section_count} planned sections; target about "
            f"{per_section_target} words for this section. Treat this as a writing "
            f"target only: do not mention it in the narration and do not pad with "
            f"repetition.\n\n"
        )

    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Visual style: {visual_style or 'story_driven'}\n"
        f"Image style: {image_style or 'photorealistic'}\n"
        f"Narration POV: {narration_pov or 'third_person'}\n"
        f"Script format: {script_format}\n\n"
        f"{budget_line}"
        f"Blueprint:\n{blueprint_json}\n\n"
        f"Prior sections summary:\n{prior_json}\n\n"
        f"Prior sections (full text — established continuity; do not contradict any "
        f"fact, character state, or fate stated here):\n{prior_full_text}\n\n"
        f"Visual concepts already used (do not repeat):\n{avoid_json}\n\n"
        f"Story source (for fact-grounding):\n{story.body[:MAX_SOURCE_EXCERPT_CHARS]}\n\n"
        f"Now generate: {label}"
    )
    if primary_required_turn:
        user_message += (
            f"\n\nThis section MUST primarily advance this one story turn:\n{primary_required_turn}"
        )
    if future_uncovered_turns:
        future_json = json.dumps(future_uncovered_turns, ensure_ascii=False)
        user_message += (
            f"\n\nFuture turns (do NOT fully resolve these yet — they belong in later sections):\n{future_json}"
        )
    if midpoint_retention_trap:
        user_message += (
            f"\n\nThis section is the story's midpoint — it MUST deliver the blueprint's "
            f"midpoint_retention_trap now, as a reveal or counterintuitive fact that "
            f"recontextualizes what the viewer thought they knew so far:\n{midpoint_retention_trap}"
        )
    result = call_claude_structured(
        task="section_generation",
        system_prompt=system_prompt,
        user_message=user_message,
        schema_name="section_output",
        input_schema=_SECTION_GENERATION_SCHEMA,
        max_tokens=3072,
    )
    # Forced tool-use's schema `required` list is advisory to the API, not a
    # Python-side guarantee — a real production run returned a tool_use input
    # missing `script_text` entirely (a required field), which crashed
    # _append_generated_section()'s direct `section["script_text"]` index
    # several call frames later with an unhelpful KeyError. This is the
    # validation this function's own docstring already documented
    # ("Raises: ValueError: If Claude returns... missing required keys") but
    # never actually implemented. Caught by _call_section_generation()'s
    # existing try/except (returns None, logged) — a transport/malformed-
    # response failure, not a quality judgment, so no retry is added here,
    # consistent with the Elimination Mandate.
    if not str(result.get("script_text") or "").strip():
        raise ValueError(
            f"generate_section: {label} response missing required 'script_text' "
            f"field (returned keys: {sorted(result.keys())})"
        )
    _coerce_section_array_fields(result, label)
    return result


def _coerce_section_array_fields(result: dict, label: str) -> None:
    """Coerce declared-array fields that Claude returned as a bare string.

    Forced tool-use is not a hard type guarantee — a real run returned
    "reveals" as a plain string instead of the schema's declared array,
    which crashed several functions downstream (_payoff_reached() tried to
    concatenate it with a list). Coerce rather than drop: a bare string is
    a degenerate single-item array, not garbage data.
    """
    for key in ("reveals", "open_questions"):
        value = result.get(key)
        if isinstance(value, str):
            logger.warning(
                "generate_section: %s field '%s' returned as str, not array — "
                "coercing to a single-item list",
                label, key,
            )
            result[key] = [value] if value else []
        elif value is None:
            result[key] = []

    vi = result.get("visual_intent")
    if isinstance(vi, dict):
        avoid = vi.get("avoid_repeating")
        if isinstance(avoid, str):
            logger.warning(
                "generate_section: %s field 'visual_intent.avoid_repeating' returned "
                "as str, not array — coercing to a single-item list",
                label,
            )
            vi["avoid_repeating"] = [avoid] if avoid else []
        elif avoid is None:
            vi["avoid_repeating"] = []


# ── Global Validation — REMOVED (Elimination Mandate, D1.2) ─────────────────
# The global narrative-coherence Claude call (validate_script_globally(),
# _GLOBAL_VALIDATION_SYSTEM_PROMPT, _GLOBAL_VALIDATION_SCHEMA, task=
# "global_validation") was deleted per
# code_report/forensic_output_audit_borrasca_run.md, section D1.2: a real
# production run found 4 real narrative issues (repeated reveals, an
# unresolved open loop, a continuity contradiction) and every single one
# shipped unfixed anyway — pure cost, zero effect on the final script.


# ── Telegram message builder (deterministic — no Claude call) ─────────────────

_TELEGRAM_TEMPLATES: dict[str, dict[str, str]] = {
    "fr": {
        "header":      "📺 Nouveau contenu trouvé",
        "title_lbl":   "Titre",
        "source_lbl":  "Source",
        "preview_lbl": "Aperçu",
        "signals_lbl": "Signaux principaux",
        "langs_lbl":   "Langues",
        "rights_lbl":  "Revue droits/IP",
        "action":      "Répondez *APPROVE* pour valider, ou décrivez ce que vous souhaitez changer.",
    },
    "en": {
        "header":      "📺 New story found",
        "title_lbl":   "Title",
        "source_lbl":  "Source",
        "preview_lbl": "Preview",
        "signals_lbl": "Top signals",
        "langs_lbl":   "Languages",
        "rights_lbl":  "Rights/IP review",
        "action":      "Reply *APPROVE* to proceed, or describe what you would like to change.",
    },
    "es": {
        "header":      "📺 Nuevo contenido encontrado",
        "title_lbl":   "Título",
        "source_lbl":  "Fuente",
        "preview_lbl": "Vista previa",
        "signals_lbl": "Señales principales",
        "langs_lbl":   "Idiomas",
        "rights_lbl":  "Revisión derechos/IP",
        "action":      "Responde *APPROVE* para continuar, o describe lo que quieres cambiar.",
    },
    "it": {
        "header":      "📺 Nuovo contenuto trovato",
        "title_lbl":   "Titolo",
        "source_lbl":  "Fonte",
        "preview_lbl": "Anteprima",
        "signals_lbl": "Segnali principali",
        "langs_lbl":   "Lingue",
        "rights_lbl":  "Revisione diritti/IP",
        "action":      "Rispondi *APPROVE* per procedere, o descrivi cosa vorresti cambiare.",
    },
}

# Deterministic story-preview sizing for build_telegram_message() — the
# operator must be able to judge APPROVE/CHANGE from the message alone,
# without following a link (and for script_source="ai_generated" there is
# no link to follow at all — see the source_display branch below). No Claude
# call: the preview is always the story's own opening sentences, never a
# generated summary.
_TELEGRAM_PREVIEW_MAX_SENTENCES = 3
_TELEGRAM_PREVIEW_MAX_CHARS = 400


# ── Script revision — REMOVED (fresh full-system audit §1.3) ─────────────────
# generate_revised_scripts(), _REVISION_SYSTEM_PROMPT, and _REVISION_SCHEMA
# were deleted: scripts are generated only AFTER Telegram approval, so no
# reachable state ever had a script to revise while its validation was still
# PENDING — the whole chain was dead code, and the operator's CHANGE reply
# was silently swallowed. A CHANGE reply is now story-level feedback
# (validation.py _handle_change): reject the story and re-dispatch discovery
# with the feedback threaded into the exclusion context. The "revision" task
# key was removed from MODEL_ROUTING with it.


# ── Public functions ───────────────────────────────────────────────────────────

def _build_telegram_preview(source_excerpt: str) -> str:
    """First 2-3 sentences of the story's own text — deterministic, no Claude call.

    The operator must be able to judge APPROVE/CHANGE from the Telegram
    message alone: title + source label say nothing about what the story is
    actually about, and for ``script_source="ai_generated"`` there is no URL
    to follow at all (``source_display`` above is a static label, not a
    link). Reuses the story's own opening sentences rather than generating a
    fresh summary — cheap, deterministic, and for the ai_generated premise
    path the "premise" already *is* a hand-shaped 3-6 sentence pitch, so its
    own opening sentences are already the right preview.
    """
    if not source_excerpt:
        return ""
    sentences = split_sentences(source_excerpt)[:_TELEGRAM_PREVIEW_MAX_SENTENCES]
    preview = " ".join(s.strip() for s in sentences if s.strip()).strip()
    if len(preview) > _TELEGRAM_PREVIEW_MAX_CHARS:
        preview = preview[:_TELEGRAM_PREVIEW_MAX_CHARS].rstrip() + "…"
    return preview


def build_telegram_message(
    title: str,
    url: str,
    assessment: dict | None,
    target_languages: list[str] | None,
    user_language: str,
    source_excerpt: str = "",
) -> str:
    """Build a Telegram validation message without any Claude call.

    Deterministic: same inputs always produce the same output. Uses
    ``_TELEGRAM_TEMPLATES`` for per-language fixed phrases; falls back to English
    for any language not in the template dict.

    Args:
        title:            Story/content title.
        url:              Source URL of the story.
        assessment:       Optional scoring dict (``{"scores": {dim: int}}``) — used
                          to surface top-2 dimensions. Omitted from message if None.
        target_languages: Optional list of BCP-47 language codes. Omitted if None.
        user_language:    BCP-47 code of the channel owner (determines template language).
        source_excerpt:   Optional story/premise body text (``Content.source_excerpt``).
                          When non-empty, its first 2-3 sentences are shown as a
                          preview so the operator can judge the story without
                          following a link (see ``_build_telegram_preview()``).
                          Omitted from the message if empty.

    Returns:
        Formatted Telegram Markdown string ready to send.
    """
    lang_key = (user_language or "en").lower()[:2]
    t = _TELEGRAM_TEMPLATES.get(lang_key, _TELEGRAM_TEMPLATES["en"])

    # AI-Generated Story Discovery (code_report/ai_generated_story_discovery_design.md):
    # the synthetic discovery://ai_generated/... URL is an internal identifier,
    # not a real source — showing it raw to the operator would be misleading.
    source_display = (
        "AI-generated original story premise (no external source)"
        if url.startswith("discovery://ai_generated/") else url
    )

    lines: list[str] = [
        t["header"],
        "",
        f"*{t['title_lbl']}:* {title}",
        f"*{t['source_lbl']}:* {source_display}",
    ]

    preview = _build_telegram_preview(source_excerpt)
    if preview:
        lines.append(f"*{t['preview_lbl']}:* {preview}")

    if assessment and isinstance(assessment.get("scores"), dict):
        dims: list[tuple[str, int]] = []
        rights_ip_risk: int | None = None
        for name, val in assessment["scores"].items():
            if isinstance(val, (int, float)):
                score = int(val)
            elif isinstance(val, dict):
                score = int(val.get("score", 0))
            else:
                continue
            if name == "rights_ip_risk":
                rights_ip_risk = score
                continue
            dims.append((name, score))
        dims.sort(key=lambda x: x[1], reverse=True)
        top2 = " · ".join(
            f"{name.replace('_', ' ').title()} ({score}/100)"
            for name, score in dims[:2]
        )
        lines.append(f"*{t['signals_lbl']}:* {top2}")
        if rights_ip_risk is not None and rights_ip_risk >= 70:
            lines.append(
                f"*{t['rights_lbl']}:* rights_ip_risk {rights_ip_risk}/100 — operator decision required"
            )

    if target_languages:
        lines.append(f"*{t['langs_lbl']}:* {' · '.join(lang.upper() for lang in target_languages)}")

    lines.append("")
    lines.append(t["action"])

    return "\n".join(lines)


_NATIVE_ADAPTATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "voice_script": {
            "type": "string",
            "description": "The fully adapted narration text in the target language.",
        },
    },
    "required": ["voice_script"],
    "additionalProperties": False,
}


def generate_native_script(
    voice_script: str,
    target_language: str,
    niche: str,
    tone: str,
    script_format: str = "youtube_long",
    audio_tags_enabled: bool = False,
    tts_model: str = "sonic-2",
    tts_provider: str = "cartesia",
    hook_context: str | None = None,
    content_kind: str = "parent_long_form",
    narration_pov: str = "third_person",
    protagonist_gender: str = "unspecified",
) -> dict:
    """Adapt a source-language script for a target language and audience.

    Assembles the native prompt via ``build_native_system_prompt()`` and injects
    HOOK_CONTEXT so the adapted opening preserves the optimised hook's mechanism.

    Args:
        voice_script:       Source-language narrator text (may include section markers
                            for ``content_kind="parent_long_form"``, or none at all for
                            ``content_kind="child_short"``).
        target_language:    BCP-47 language code for the output (e.g. "fr", "de", "es").
        niche:              Channel niche.
        tone:               Channel tone.
        script_format:      Format key from ``channel_config.script_format``. Only
                            consulted when ``content_kind="parent_long_form"``.
        audio_tags_enabled: Channel-level opt-in for ElevenLabs v3 audio tags.
        tts_model:          TTS model ID for the target-language voice.
        tts_provider:       TTS provider ("cartesia" | "elevenlabs").
        hook_context:       Optional pre-built HOOK_CONTEXT string (from optimize_intro or
                            extracted inline). If None, extracted from voice_script.
        content_kind:       "parent_long_form" (default) or "child_short" (Phase 12.4).
                            Selects the dedicated flat-narration native prompt for
                            standalone child Short episodes — see
                            ``build_native_system_prompt()``.
        narration_pov:     Channel narration perspective/register, threaded from
                            ChannelConfig alongside visual_style/image_style.

    Returns:
        Dict with key ``voice_script`` in ``target_language``.

    Raises:
        ValueError: If Claude returns malformed JSON or a key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    prompt = build_native_system_prompt(
        script_format, tts_model, tts_provider, audio_tags_enabled, content_kind=content_kind,
    )

    # Resolve hook context from source voice_script when not provided by caller
    ctx = hook_context if hook_context is not None else _extract_hook_context(voice_script, script_format)

    user_message = (
        f"Target language: {target_language}\n"
        f"Channel niche: {niche}\n"
        f"Channel tone: {tone}\n"
        f"Narration POV: {narration_pov or 'third_person'}\n"
        f"Narrator gender (for grammatical agreement): "
        f"{protagonist_gender if protagonist_gender in {'feminine', 'masculine'} else 'unspecified'}\n"
    )
    if ctx:
        user_message += f"\nHOOK_CONTEXT:\n{ctx}\n"
    user_message += f"\nSource voice script:\n{voice_script}"
    return call_claude_structured(
        task="native_adaptation",
        system_prompt=prompt,
        user_message=user_message,
        schema_name="native_adaptation_output",
        input_schema=_NATIVE_ADAPTATION_SCHEMA,
        max_tokens=8192,
    )


# ── Script Quality Gate assess/rewrite — REMOVED (Elimination Mandate, D1.1) ─
# assess_script_quality() and rewrite_script_for_quality() (plus their schemas
# and the _SCRIPT_QUALITY_SYSTEM_PROMPT/_SCRIPT_QUALITY_REWRITE_BASE prompts
# above) were deleted per code_report/forensic_output_audit_borrasca_run.md,
# section D1.1: a real production run spent two paid rewrites and the script
# still came back NEEDS_REWRITE, with the flagged repetitions shipped unfixed
# regardless — the rewrite persona was also hardcoded "documentary
# scriptwriter", actively pulling every channel's register toward documentary
# style irrespective of configured tone (see CLAUDE.md's register-fix note,
# P1-9). Deterministic structural checks (TTS compliance, hook quality,
# maximum length, retention structure) still run in
# run_script_quality_gate() (scripts.py) as telemetry only.


# ── Story Scoring Gate (single story) ─────────────────────────────────────────

# Slimmed 19 → 8 dimensions (fresh full-system audit §2.5): four pairs were
# near-duplicates (clickability/thumbnail/title_thumbnail/scroll_stopper;
# central_mystery/curiosity_gap; viral_clip_count/short_form_clip_potential;
# series/episode_two), five carried weights of 0.01-0.02 (≤7 combined points),
# and 12 independent floors gave a noisy LLM score 12 chances to reject a
# usable story. Each surviving dimension's prompt description absorbs what its
# merged siblings measured.
_SCORING_DIMENSIONS: list[str] = [
    "visual_storytelling_potential",
    "scroll_stopper_potential",
    "emotional_stakes",
    "central_mystery",
    "conflict_or_contradiction",
    "social_media_clickability",
    "image_generation_feasibility",
    "rights_ip_risk",
]

_SINGLE_STORY_SCORING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "description": "Integer score 0–100 for each story-gate dimension.",
            "properties": {
                dim: {"type": "integer", "minimum": 0, "maximum": 100}
                for dim in _SCORING_DIMENSIONS
            },
            "required": _SCORING_DIMENSIONS,
            "additionalProperties": False,
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}

_SINGLE_STORY_SCORING_SYSTEM_PROMPT = """\
Score this story's potential to perform on YouTube, TikTok, Instagram Reels, and YouTube Shorts.
You are not deciding whether to accept or reject the story — another system makes that decision.
Score strictly using fixed anchors so the same story always receives the same scores.
Output ONLY the tool schema. No prose, no explanations.

Anchors for performance dimensions:
  0–30   = weak / absent (actively hurts the video or makes it unclickable)
  31–65  = moderate (present but needs heavy compensation)
  66–100 = strong (clear asset that makes the video noticeably better)

Special inverted operator-review dimension:
  rights_ip_risk: 0–30 = low apparent risk/public-domain/original personal account;
  31–65 = uncertain authorship, reposted fiction, or adaptation ambiguity;
  66–100 = famous authored fiction, named franchise/character/world, rights-managed
  creepypasta, or source/title that appears commercially claimable. This dimension
  is not a performance score and does not decide acceptance; it flags operator review.

Dimensions:
  visual_storytelling_potential  Can be SHOWN on screen with several distinct visual
                                 categories AND genuinely different contexts/environments?
  scroll_stopper_potential       Does the opening moment stop a mid-scroll viewer —
                                 concrete, high-stakes, dropping them into
                                 action/danger/contradiction?
  emotional_stakes               Named person in real human drama with personal
                                 consequence — emotion tied to a specific person in a
                                 specific moment?
  central_mystery                Clear factual mystery or unexplained phenomenon whose
                                 opening creates an open question the story credibly answers?
  conflict_or_contradiction      Real conflict or factual contradiction (not bland)?
  social_media_clickability      Compelling title AND one powerful, nameable thumbnail
                                 image together — would a user click on those alone?
  image_generation_feasibility   Key moments can be depicted as distinct concrete generated images?
  rights_ip_risk                 Operator-review risk for monetized adaptation rights/IP claims?

Rules: score strictly; do NOT invent facts; judge only what is in the story body provided.\
"""


def score_story_for_gate(
    story,
    channel,
    script_format: str = "youtube_long",
) -> dict:
    """Score a single candidate story's documentary and visual performance potential.

    Uses ``call_claude_structured`` with a forced tool-use schema so the response is
    always a flat ``{scores: {dim: int}}`` dict — no prose, no extra keys.

    Args:
        story:         Story object (title, url, body, upvotes, comments, published_at).
        channel:       Channel ORM object (provides niche and tone as context).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        Dict with ``scores`` mapping each story-gate dimension to an integer 0–100.

    Raises:
        ValueError: If Claude's response is malformed or missing required dimensions.
    """
    if getattr(story, "source_type", None) == "ai_generated":
        # AI-Generated Story Discovery (code_report/ai_generated_story_discovery_design.md):
        # a synthesized premise has no real-world engagement signal — upvotes/
        # comments/URL are meaningless placeholders here, not honest zeros.
        # Presenting them as metadata would push scroll_stopper_potential/
        # social_media_clickability to penalize an absence that isn't real.
        metadata_line = (
            "Metadata: this is an AI-generated original premise — no real-world engagement "
            "signal exists for it. Do not penalize scroll_stopper_potential or "
            "social_media_clickability for the natural absence of upvotes/comments/URL. "
            "The body is an operator-facing plain-language pitch, not the finished script's "
            "literal opening. For scroll_stopper_potential, judge whether the concrete "
            "moments explicitly described in the pitch contain a high-stakes action, danger, "
            "or contradiction that the script could open on; do not penalize the pitch for "
            "stating its subject plainly, and do not invent an unmentioned moment.\n"
        )
    else:
        metadata_line = (
            f"Metadata: upvotes={story.upvotes}, comments={story.comments}, "
            f"published_at={story.published_at.isoformat()}\n"
        )
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Story title: {story.title}\n"
        f"Story URL: {story.url}\n"
        f"{metadata_line}\n"
        f"Story body:\n{story.body[:6000]}"
    )

    result = call_claude_structured(
        task="story_gate_scoring",
        system_prompt=_SINGLE_STORY_SCORING_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="story_gate_scoring",
        input_schema=_SINGLE_STORY_SCORING_SCHEMA,
        max_tokens=512,
    )

    raw_scores = result.get("scores")
    if not isinstance(raw_scores, dict):
        raise ValueError("score_story_for_gate: 'scores' missing or not a dict in response")

    missing = [d for d in _SCORING_DIMENSIONS if d not in raw_scores]
    if missing:
        raise ValueError(f"score_story_for_gate: missing dimensions: {missing}")

    return result


# ── Standalone short planning: Shorts Planner ──────────────────────────────────────────────────

_SHORTS_PLANNER_SYSTEM_PROMPT = """\
You are a Short-form content strategist planning how to split a long-form story into
3–5 standalone TikTok episodes.

Your task: read the source story (voice script + blueprint) and produce a part plan.

Rules:
- total_parts must be between 3 and 5 (inclusive). Never fewer than 3 or more than 5.
- Split at narrative boundaries: reveals, discoveries, reversals, or escalations.
  Never split primarily by time — narrative logic is paramount.
- Each part targets 72–88 seconds of spoken narration (≈210–260 words at the measured
  ~175 wpm compressed Short narration rate). A Short must NEVER run under 61 seconds —
  a part plan too thin to sustain at least 210 words of narration is a planning error.
  Keep the plan scoped tightly enough for the writer to stay within that range without
  omitting the main reveal.
- Every part must be independently watchable: a viewer who starts on Part 3 must
  understand the situation from the first 5 seconds without having seen prior parts.
- Every part owns an exclusive narrative span. A part must never re-narrate another
  part's main_reveal, and its ending must stop inside its own span instead of
  continuing into a sibling part's events.
- opening_hook: 1–2 sentences, each ≤15 words, starts at a high-tension story moment. No recap.
  Apply the cold-open deletion test: read it with the title, part number, previous part,
  and every earlier sentence removed. It must still identify the person, event, object,
  or situation needed to understand the hook. Never open with backward-dependent wording
  such as "they weren't hypothetical", "that changed everything", "but then", or an
  unexplained he/she/they/this/that. Must reference something SPECIFIC from the story —
  not a generic "wait for it" tease — while withholding the part's payoff.
- Narration POV is provided below. In "first_person_storytime" mode, satisfy the
  cold-open identity requirement IN the narrator's own first-person voice (for example,
  "I am Hannibal Barca" or "I led Carthage's army"). Never add a third-person naming
  sentence and then switch to I/me/my. Name the narrator within the first five seconds.
  In "third_person" mode, name the subject normally; never use an ambiguous "she" for
  an invented public persona when the person's name or role is available.
- Part N's cliffhanger must be directly answered by Part N+1's main_reveal.
  Every cliffhanger must name the concrete unresolved threat, person, event, place, or
  object — never tease only "something", "what came next", or another unnamed abstraction.
  The final part's cliffhanger is replaced by a comment trigger question (ends with "?").
  That final question must be unique to this story and must not copy blueprint.comment_trigger
  verbatim or near-verbatim; avoid reusable CTAs such as "what would you do?".
- Never invent facts not present in the voice script or blueprint.
- If a part uses sunk-cost reasoning ("we came this far", "turning back would waste it"),
  frame it explicitly as the narrator's or character's rationalization under pressure,
  never as objective wisdom or advice endorsed by the script.
- goal, main_content_summary, and main_reveal: one concise sentence each.

Output ONLY the tool schema. No prose, no extra keys.\
"""

_SHORTS_PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "total_parts": {
            "type": "integer",
            "minimum": 3,
            "maximum": 5,
            "description": "Total number of Short episodes. Must be 3, 4, or 5.",
        },
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "part":                 {"type": "integer"},
                    "goal":                 {"type": "string"},
                    "opening_hook":         {"type": "string"},
                    "main_content_summary": {"type": "string"},
                    "main_reveal":          {"type": "string"},
                    "cliffhanger":          {"type": "string"},
                },
                "required": [
                    "part", "goal", "opening_hook",
                    "main_content_summary", "main_reveal", "cliffhanger",
                ],
                "additionalProperties": False,
            },
            "minItems": 3,
            "maxItems": 5,
        },
    },
    "required": ["total_parts", "parts"],
    "additionalProperties": False,
}


def generate_shorts_plan(
    voice_script: str,
    blueprint: dict,
    channel,
    narration_pov: str = "third_person",
) -> dict:
    """Plan 3–5 standalone TikTok episodes from a long-form voice script.

    Uses a SECONDARY_MODEL structured call — the output is validated by Python
    for the total_parts range constraint (3 ≤ n ≤ 5). Callers should retry once
    if the constraint fails before giving up.

    Args:
        voice_script: Fully assembled long-form voice script (with markers).
        blueprint:    Blueprint dict from generate_story_blueprint().
        channel:      Channel ORM object (provides niche and tone).
        narration_pov: Canonical channel narration perspective. The planner uses
                       it to phrase cold-open identity in the correct person.

    Returns:
        Dict with ``total_parts`` (int) and ``parts`` (list of part plan dicts).

    Raises:
        ValueError: If Claude returns malformed JSON, missing keys, or total_parts
                    outside [3, 5].
        anthropic.APIError: On non-retryable Claude API errors.
    """
    import json
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n\n"
        f"Narration POV: {narration_pov or 'third_person'}\n\n"
        f"Blueprint:\n{json.dumps(blueprint, ensure_ascii=False)}\n\n"
        f"Long-form voice script:\n{voice_script}"
    )
    result = call_claude_structured(
        task="shorts_planner",
        system_prompt=_SHORTS_PLANNER_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="shorts_plan",
        input_schema=_SHORTS_PLAN_SCHEMA,
        # 1024 was observed truncating in production: a real run hit
        # output_tokens=1024 exactly on both attempts, and the truncated tool
        # JSON came back with parts=[] both times — a 5-part plan (max
        # schema size) with 6 text fields per part plus tool-call overhead
        # does not reliably fit in 1024. The prior retry-on-ValueError loop
        # was retrying the identical request into the identical ceiling, so
        # it failed deterministically twice, not just unluckily. 4096 gives
        # real headroom (same order of magnitude as generate_section()'s
        # 3072 for a single section, here covering up to 5 short sections'
        # worth of small fields).
        max_tokens=4096,
    )
    total = result.get("total_parts")
    if not isinstance(total, int) or not (3 <= total <= 5):
        raise ValueError(
            f"generate_shorts_plan: total_parts must be 3–5, got {total!r}"
        )
    parts = result.get("parts") or []
    if len(parts) != total:
        raise ValueError(
            f"generate_shorts_plan: parts list length {len(parts)} != total_parts {total}"
        )
    required_part_keys = {"part", "goal", "opening_hook", "main_content_summary", "main_reveal", "cliffhanger"}
    for i, part in enumerate(parts):
        missing = required_part_keys - set(part.keys())
        if missing:
            raise ValueError(f"generate_shorts_plan: part[{i}] missing keys: {missing}")
    return result


# ── Standalone short planning: Short Episode Script ────────────────────────────────────────────

_SHORT_EPISODE_SYSTEM_PROMPT = """\
You are writing a TikTok episode script — one standalone part of a multi-part story.

This is NOT a cut of a longer video. It is purpose-built for TikTok.

Rules:
- Target 210–260 words. Count every word in voice_script before returning.
  NEVER return fewer than 190 words: the finished Short must run at least 61 seconds
  of narration, and below ~190 words that is physically impossible. If voice_script
  exceeds 260 words, remove the least essential sentences — but when in doubt, keep
  them: a slightly long Short ships; a too-short one cannot.
  (260 words ≈ 89 seconds at the measured ~175 wpm compressed Short narration rate —
  calibrated from production audio after interior-silence compression, not the raw
  "words per second" of the TTS voice.)
- First sentence uses the opening_hook from the plan, ≤15 words, and starts at a
  high-tension story moment. It must also pass the cold-open deletion test: read it as
  the first thing a viewer ever hears, with no title, part number, previous part, or
  earlier sentence. It must name the person, event, object, or situation needed to
  understand it; never begin with backward-dependent wording such as "they weren't
  hypothetical", "that changed everything", "but then", or an unexplained
  he/she/they/this/that. If the planned hook lacks its own referent, repair that missing
  context in the first sentence instead of copying the fragment. If opening_hook or
  main_reveal already states the story's final answer or mechanism, do not restate it
  that directly here — open on the situation or the unresolved question instead, and
  let the reveal land later in this part's narration.
- Re-hook every 7–10 seconds of narration: a new curiosity gap, question, or micro-reveal
  that prevents the viewer from scrolling away. These are not summaries — they are new angles.
- Spoken-video delivery — this is heard, not read silently as prose: default to present
  tense for story events wherever the story allows it ("She opens the door" not "She
  opened"); speak directly to the viewer at least once as "you" or with a rhetorical
  question aimed at them; use contractions wherever natural speech would ("doesn't",
  "can't", "it's") — formal, contraction-free prose reads as robotic when spoken by TTS;
  read-aloud test — if a sentence wouldn't be said out loud telling this story to a
  friend, rewrite it in spoken language.
- Provide only the minimum context needed for a first-time viewer to immediately understand the current situation.
  Do not summarize earlier events unless they are essential to understand the current reveal.
- One clear main_reveal per part — this is the payoff for watching this part
- The part plan includes other_parts_main_reveals. Treat every item there as forbidden
  sibling territory: do not narrate, recap, or append those reveals. End within this
  part's own span; do not continue into the next part's events.
- When the story jumps by months or years, state the elapsed time and destination date
  as a natural spoken bridge. Do not make an unexplained temporal cut.
- Drop one-off atmospheric details unless their relevance is explained in the same
  passage. Never introduce a letter, object, sound, or image and simply abandon it.
- Do not state the same fact or implication twice in this script, even in different
  words. Once something is established, move forward — do not circle back to it.
- End by delivering the planned cliffhanger while preserving its narrative intent — this is what drives the viewer to Part N+1
- The cliffhanger must name the concrete unresolved threat, person, event, place,
  or object. Never end only on an unnamed "something", "what came next", or a
  similarly abstract tease.
- CTA diversity: the final question/CTA must be specific to THIS Short's unresolved
  moment and must not copy blueprint.comment_trigger verbatim or near-verbatim. Do
  not reuse generic channel endings such as "what would you do?", "would you go
  back?", or "what do you think happened?" unless the sentence is anchored in
  this Short's exact person, object, place, or consequence.
- Sentence rhythm: short sentences (3–7 words) for tension, longer (8–15 words) for buildup.
  Never 3+ consecutive sentences of the same length.
- No filler, no recap, no "as I mentioned", no "in Part 1"
- No [SECTION N] markers — Short scripts are flat narration only
- Narration POV — driven by the "Narration POV" value in the user message, not
  hardcoded: "third_person" (default) narrates about the story's people using
  third-person pronouns and names; "first_person_storytime" narrates AS the
  protagonist retelling their own experience directly to the viewer, using
  "I"/"me"/"my" throughout (the r/nosleep-style storytime format). Never mix POV
  within this part's narration.
- In "first_person_storytime" mode, satisfy the cold-open identity requirement in
  first person (for example, "I am Hannibal Barca" or "I led Carthage's army").
  Never insert a third-person naming sentence and then switch to I/me/my. Name the
  narrator within the first five seconds.
- In third person, never use an ambiguous "she" for an invented public persona when
  the person's name or concrete role is available.
- Prefer a precise final question anchored to this part's person, choice, object, or
  consequence; never ask a scope-less question about "all of history".
- Any sunk-cost reasoning must be framed as my/the character's rationalization
  under pressure, never as objective wisdom or advice endorsed by the narration.
- ORIGINALITY — this is the most strictly enforced rule in this prompt: you will be
  given the long-form voice script for story grounding only. You must NEVER lift a
  run of 6 or more consecutive words directly from it, even when the long-form
  phrasing is already tight and factual. If a passage in the source is hard to
  paraphrase, that is a signal to find a different angle into the same fact — not
  a reason to copy it. Write this part's narration as if you had never read the
  long-form script word-for-word, only learned the underlying facts from it.
- Avoid generic clickbait filler and canned suspense phrases.
  Do not use stock lines such as "you won't believe", "but here's the thing",
  "little did they know", or similar formulaic hooks.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{"title": "Part N title (≤60 chars, TikTok-optimized)", "voice_script": "Full flat narration text"}\
"""


_SHORT_EPISODE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title":        {"type": "string", "description": "Part N title (≤60 chars, TikTok-optimized)."},
        "voice_script": {"type": "string", "description": "Full flat narration text, target 210-260 words (never under 190)."},
    },
    "required": ["title", "voice_script"],
    "additionalProperties": False,
}


def generate_short_episode_script(
    part_plan: dict,
    long_voice_script: str,
    blueprint: dict,
    channel,
    channel_voice,
    visual_style: str = "",
    image_style: str = "",
    narration_pov: str = "third_person",
    word_floor_note: str = "",
) -> dict:
    """Generate a single TikTok episode script from a part plan.

    The user message includes the part plan, the relevant excerpt of the long
    voice_script, and the blueprint — Claude writes purpose-built TikTok narration,
    NOT a cut of the long video.

    Args:
        part_plan:          Single part dict from generate_shorts_plan().
        long_voice_script:  Full long-form voice script (for story grounding).
        blueprint:          Blueprint dict from generate_story_blueprint().
        channel:            Channel ORM object (provides niche and tone).
        channel_voice:      ChannelVoice ORM object (provides tts_model for TTS_BLOCK).
        word_floor_note:    Optional prepended constraint line — used ONLY by the
                            single deterministic word-floor regeneration
                            (operator-approved Elimination Mandate exception,
                            2026-07-16): an objective word-count trigger, never
                            an AI quality judgment.

    Returns:
        Dict with keys ``title`` (str) and ``voice_script`` (str).

    Raises:
        ValueError: If Claude returns malformed JSON or missing required keys.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    import json
    tts_model    = channel_voice.tts_model if channel_voice else "sonic-2"
    tts_provider = channel_voice.provider  if channel_voice else "cartesia"
    system_prompt = with_tts_block(_SHORT_EPISODE_SYSTEM_PROMPT, tts_provider, tts_model)

    part_n     = part_plan.get("part", "?")
    total_parts = part_plan.get("_total_parts", "?")   # injected by caller
    part_json  = json.dumps(part_plan, ensure_ascii=False)
    bp_json    = json.dumps(blueprint, ensure_ascii=False)

    floor_line = f"{word_floor_note}\n\n" if word_floor_note else ""
    user_message = (
        f"{floor_line}"
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Visual style: {visual_style or 'story_driven'}\n"
        f"Image style: {image_style or 'photorealistic'}\n"
        f"Narration POV: {narration_pov or 'third_person'}\n"
        f"Part: {part_n} of {total_parts}\n\n"
        f"Part plan:\n{part_json}\n\n"
        f"Blueprint:\n{bp_json}\n\n"
        f"CTA guardrail: do not copy blueprint.comment_trigger verbatim or near-verbatim; "
        f"write a story-specific final question for this part only.\n\n"
        # Full script, deliberately untruncated (fresh full-system audit §2.4):
        # a 1,600-word long script is ~10k chars, so the old [:6000] slice cut
        # off the story's ending — parts 4-5 of a 5-part plan were grounded on
        # one-sentence plan summaries alone, reintroducing the thin-grounding
        # fabrication risk the source-material floor exists to prevent. The
        # long script is capped at ~1,750 words by check_maximum_length, so the
        # full text always fits comfortably in context.
        f"Long-form voice script (for FACT GROUNDING ONLY — see ORIGINALITY rule above. "
        f"Do not reuse its exact phrasing):\n"
        f"{long_voice_script}"
        )

    return call_claude_structured(
        task="short_script",
        system_prompt=system_prompt,
        user_message=user_message,
        schema_name="short_episode_script_output",
        input_schema=_SHORT_EPISODE_SCHEMA,
        max_tokens=1024,
    )

# ── Short Quality Gate — REMOVED (Elimination Mandate, D1.3) ────────────────────
# The AI Short Quality Gate (assess_short_script_quality(), _SHORT_QUALITY_SYSTEM_PROMPT,
# _SHORT_QUALITY_SCHEMA, task="short_quality_check") was deleted per
# code_report/forensic_output_audit_borrasca_run.md, section D1.3: a real
# production run showed its PASSED-with-issues contract mismatch burned a retry
# into a worse draft, and the correction loop it fed made Short scripts worse
# across attempts, not better. Deterministic structural checks
# (_collect_short_script_major_issues in scripts.py) and the parent/child
# overlap detector (detect_parent_child_overlap) remain as telemetry-only checks
# on the single generated draft — see _generate_short_script() in scripts.py.
