import logging
import re

from app.services.claude_client import call_claude, call_claude_structured, parse_claude_json

logger = logging.getLogger(__name__)

PROMPT_VERSION = "4.3"  # v4.3: removed RETENTION_BLOCK (dead since v4.0 — zero callers,
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
- One idea per sentence — do not join two distinct thoughts with "and" or "but".
- One blank line between narrative beats (breathing room for the voice).
- No stage directions, no parenthetical notes, no editorial asides in brackets.
- Square brackets are allowed ONLY for section markers: [INTRO], [SECTION N], [OUTRO].

FINAL CHECK — before returning your JSON:
  Re-read every sentence in voice_script one by one and count its words.
  If any sentence contains 19 or more words, STOP and split it into two shorter
  sentences before returning. Do not return until every sentence is ≤18 words.
  No exceptions — a 19-word sentence is a hard failure.\
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
You are a professional translator for YouTube documentary content.

Translate the provided script accurately and naturally into the target language.
This is a factual YouTube video — all facts, names, dates, and statistics must be
preserved exactly.

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
- Preserve only the minimum context the source narration itself includes to orient a
  first-time viewer. Do not add extra recap, setup, or background beyond what the
  source narration already contains — do not summarize earlier parts.
- If the source narration ends on a cliffhanger or a forward tease, preserve its exact
  narrative intent in the adaptation — do not resolve it, soften it, or drop it.
- Match the source narration's approximate length. Do not pad, expand, or add material
  to make the adaptation feel longer or more "complete" — a short, punchy source must
  stay short and punchy in the target language.

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
        base_name = "parent_long_form_documentary"
    else:
        base = _BASE_SHORT_FORM_NATIVE
        base_name = "parent_short_form_sectioned"

    logger.info(
        "NATIVE_ADAPTATION_PROMPT_SELECTED content_kind=%s script_format=%s base=%s",
        content_kind, script_format, base_name,
    )

    fallback = _TTS_FALLBACK.get(tts_provider, "sonic-2")
    tts = TTS_BLOCK.get(tts_model, TTS_BLOCK[fallback])
    parts = [base, "\n\n" + tts]
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
            f"This was selected by a retention optimizer as the strongest hook for this story. "
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
a news story and design both its narrative skeleton AND its emotional arc — how
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
- suggested_section_count: number of BODY sections (not counting INTRO and OUTRO).
  Between 2 and 5. Python may override.
- suggested_title: YouTube title derived from hook. 60–70 chars. SEO-optimized.
- Write the hook, major_turns, and final_payoff in a register matching the Channel
  niche and Channel tone values provided below (provided below). Horror/thriller/mystery: favor dread,
  withheld information, and escalating unease. Documentary/educational: favor
  clarity and context. Match the configured niche — do not default to a neutral
  documentary register regardless of niche.

Never invent facts not present in the story body.\
"""

_STORY_BLUEPRINT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "hook":                   {"type": "string"},
        "central_question":       {"type": "string"},
        "major_turns":            {"type": "array", "items": {"type": "string"}, "minItems": 2},
        "final_payoff":           {"type": "string"},
        "comment_trigger":        {"type": "string"},
        "suggested_section_count": {"type": "integer", "minimum": 2, "maximum": 5},
        "suggested_title":        {"type": "string"},
    },
    "required": [
        "hook", "central_question", "major_turns", "final_payoff",
        "comment_trigger", "suggested_section_count", "suggested_title",
    ],
}


def generate_story_blueprint(story, channel, script_format: str = "youtube_long") -> dict:
    """Extract the narrative skeleton from a story before any script writing.

    Generates a constraint document — hook, central question, major turns, final payoff,
    comment trigger, suggested title and section count. Every section generated afterward
    must advance toward the payoff and end with the comment trigger.

    Args:
        story:         Story object (title, url, body, language).
        channel:       Channel ORM object (niche, tone).
        script_format: Format key — affects suggested_section_count recommendation.

    Returns:
        Dict with keys: hook, central_question, major_turns, final_payoff,
        comment_trigger, suggested_section_count, suggested_title.

    Raises:
        ValueError: If major_turns has fewer than 2 entries or required keys missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Story title: {story.title}\n"
        f"Story URL: {story.url}\n\n"
        f"Story body:\n{story.body[:8000]}"
    )
    result = call_claude_structured(
        task="story_blueprint",
        system_prompt=_STORY_BLUEPRINT_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="story_blueprint",
        input_schema=_STORY_BLUEPRINT_SCHEMA,
        max_tokens=768,
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
You are a YouTube documentary scriptwriter generating ONE narration section at a time.

Your output is a single narration block — not a complete script. Every word will be read
aloud by a TTS voice directly to the viewer.

Blueprint constraint: every section must advance the story toward the final_payoff and
comment_trigger provided in the blueprint. Do not veer off-story.

Each BODY section must do EXACTLY ONE of these narrative functions:
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
  - Prior summaries and reveals listed in the user message are FORBIDDEN MATERIAL.
    Do not restate, rephrase, or echo them. The ONLY exception: referencing a prior fact
    to add a direct new consequence ("X happened — which meant Y was now inevitable").
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
  - The two strongest mini-hooks across the whole script must land at the body sections
    nearest the 25% and 60% marks of total word count — these are the highest
    audience drop-off risk points.
  - Every 110–150 words of narration, introduce a new revelation, complication, or
    emotional beat. Tension must never plateau — if two consecutive sentences add no
    new fact or escalation, the section is failing this rule.

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
  - Must not introduce any new unresolved question.

Output format — return ONLY the tool schema. No prose, no code fence, no extra keys.

Rules:
1. Never fabricate facts not in the story body or blueprint.
2. script_text must NOT contain [INTRO], [SECTION N], or [OUTRO] markers inside it.
3. Every sentence in script_text must be ≤18 words. Count them.
4. suggests_outro: true ONLY when all major_turns from the blueprint have been covered in
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
        },
    },
    "required": ["script_text", "summary", "reveals", "open_questions", "suggests_outro", "visual_intent"],
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
    override_instruction: str = "",
    primary_required_turn: str | None = None,
    future_uncovered_turns: list[str] | None = None,
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
        override_instruction:    Optional extra constraint appended to user message
                                 (used for targeted retry after completeness check failure).
        primary_required_turn:   The single earliest uncovered major_turn this section must
                                 primarily advance. Injected as "MUST primarily advance this
                                 one turn". None for INTRO and OUTRO (no constraint).
        future_uncovered_turns:  Remaining uncovered turns after the primary. Injected as
                                 "do NOT fully resolve these yet". None if ≤1 turn remains.

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

    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Blueprint:\n{blueprint_json}\n\n"
        f"Prior sections summary:\n{prior_json}\n\n"
        f"Visual concepts already used (do not repeat):\n{avoid_json}\n\n"
        f"Story source (for fact-grounding):\n{story.body[:4000]}\n\n"
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
    if override_instruction:
        user_message += f"\n\nIMPORTANT: {override_instruction}"

    return call_claude_structured(
        task="section_generation",
        system_prompt=system_prompt,
        user_message=user_message,
        schema_name="section_output",
        input_schema=_SECTION_GENERATION_SCHEMA,
        max_tokens=3072,
    )


# ── Global Validation ────────────────────────────────────────────────────────

_GLOBAL_VALIDATION_SYSTEM_PROMPT = """\
You check ONLY narrative coherence of a fully assembled video script.

Do NOT re-check:
  - Sentence length or TTS compliance  (already checked per section)
  - Hook quality or forbidden openers  (already checked per section)
  - Word count or minimum length       (already checked per section)

Check ONLY:
  1. Section flow: does each section transition naturally from the previous?
     Flag abrupt topic jumps where the connection is unclear.
  2. Fact repetition: is any specific fact, name, or statistic stated more than once?
  3. Outro resolution: does the outro answer the question the intro raised?
  4. Open loops: are there questions raised mid-script that the outro never closes?

Use FIXED criteria — identical script must always return identical result.
Output ONLY the tool schema. No prose, no extra keys.\
"""

_GLOBAL_VALIDATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "NEEDS_FIX"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section":     {"type": "string"},
                    "description": {"type": "string"},
                    "suggestion":  {"type": "string"},
                },
                "required": ["section", "description", "suggestion"],
            },
        },
    },
    "required": ["status", "issues"],
}


def validate_script_globally(voice_script: str, blueprint: dict) -> dict:
    """Run a Haiku narrative coherence check on the fully assembled voice script.

    Checks transitions, repetition, intro/outro resolution, and open loops.
    Does NOT re-check TTS compliance, hook quality, or word count.

    Args:
        voice_script: Fully assembled script with [INTRO]/[SECTION N]/[OUTRO] markers.
        blueprint:    Blueprint dict — used to contextualise intro/outro resolution check.

    Returns:
        Dict with status ("PASS" | "NEEDS_FIX") and issues list.

    Raises:
        ValueError: If Claude returns malformed JSON.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    import json
    user_message = (
        f"Blueprint (for context):\n{json.dumps(blueprint, ensure_ascii=False)}\n\n"
        f"Voice script:\n{voice_script}"
    )
    result = call_claude_structured(
        task="global_validation",
        system_prompt=_GLOBAL_VALIDATION_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="global_validation",
        input_schema=_GLOBAL_VALIDATION_SCHEMA,
        max_tokens=1024,
    )
    if result.get("status") not in {"PASS", "NEEDS_FIX"}:
        raise ValueError(f"validate_script_globally: unexpected status {result.get('status')!r}")
    return result


# ── Telegram message builder (deterministic — no Claude call) ─────────────────

_TELEGRAM_TEMPLATES: dict[str, dict[str, str]] = {
    "fr": {
        "header":      "📺 Nouveau contenu trouvé",
        "title_lbl":   "Titre",
        "source_lbl":  "Source",
        "signals_lbl": "Signaux principaux",
        "langs_lbl":   "Langues",
        "action":      "Répondez *APPROVE* pour valider, ou décrivez ce que vous souhaitez changer.",
    },
    "en": {
        "header":      "📺 New story found",
        "title_lbl":   "Title",
        "source_lbl":  "Source",
        "signals_lbl": "Top signals",
        "langs_lbl":   "Languages",
        "action":      "Reply *APPROVE* to proceed, or describe what you would like to change.",
    },
    "es": {
        "header":      "📺 Nuevo contenido encontrado",
        "title_lbl":   "Título",
        "source_lbl":  "Fuente",
        "signals_lbl": "Señales principales",
        "langs_lbl":   "Idiomas",
        "action":      "Responde *APPROVE* para continuar, o describe lo que quieres cambiar.",
    },
    "it": {
        "header":      "📺 Nuovo contenuto trovato",
        "title_lbl":   "Titolo",
        "source_lbl":  "Fonte",
        "signals_lbl": "Segnali principali",
        "langs_lbl":   "Lingue",
        "action":      "Rispondi *APPROVE* per procedere, o descrivi cosa vorresti cambiare.",
    },
}


# ── Revision prompt ────────────────────────────────────────────────────────────

_REVISION_SYSTEM_PROMPT = """\
You revise an existing video script based on user feedback.

Rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Preserve the source language, tone, and factual content unless the feedback explicitly
   asks to change them.
3. Apply changes accurately and minimally — do not rewrite what the feedback does not address.
4. Never invent facts, URLs, statistics, or events not present in the script you received.
5. Never send a partial script — always return the full voice_script.
6. Preserve [INTRO], [SECTION N], [OUTRO] markers in voice_script.
7. Output schema:
   {"title": "...", "voice_script": "...",
    "changes": [{"section": "INTRO|SECTION 1|...|OUTRO", "before_summary": "...", "after_summary": "..."}]}
   Include an entry in "changes" for every section that was meaningfully modified.
   "before_summary" and "after_summary": one sentence each describing the substance of the change.\
"""


# ── Script Quality Gate ────────────────────────────────────────────────────────

_SCRIPT_QUALITY_SYSTEM_PROMPT = """\
You are a YouTube retention editor reviewing a documentary narration script BEFORE production.
Your only job: decide whether this script would make a normal viewer keep watching, or whether
it needs a rewrite. You are not checking facts or technical formatting — another system does that.

Judge the script the way an experienced YouTube editor would judge a first draft, against
these dimensions:
  - hook: Does the opening grab attention with something concrete and specific in the
    first sentence? Would a viewer keep watching past 10 seconds? Does the opening
    reveal the story's actual ending, answer, or mechanism — rather than creating a
    question the rest of the video must answer? If it gives away the ending, this is
    a HIGH severity hook issue regardless of how concrete or well-written it is.
  - clarity: Is it always clear what is happening, who is involved, and why it matters?
  - emotional_pull: Does the viewer have a reason to care about the people/events?
  - narrative_arc: Does tension build toward a payoff, or does it stay flat / meander?
    Specifically: does any section re-explain a fact or idea already established
    elsewhere in the script, even in different words? Does any section compress two
    distinct significant facts (a motive AND a method, a cause AND a consequence)
    into one or two rushed sentences instead of giving the more important one room
    to land? Flag both as narrative_arc issues, HIGH severity.
  - specificity: Are claims grounded in concrete facts, names, numbers, dates — or vague?
  - generic_language: Does it contain stock AI-documentary phrasing ("This is a story
    about…", "What happened next…", "Everything changed…", "Little did they know") used
    as a crutch instead of a grounded specific?
  - tts_readability: Will this sound natural and human when read aloud by a TTS voice?

Use FIXED, repeatable criteria — do not be lenient or harsh based on mood.

Decision rule:
  - status = "PASSED" only if the script would plausibly hold a YouTube viewer's
    attention through the intro, feel like a professionally written documentary,
    AND contains no HIGH severity issue in any dimension.
  - status = "NEEDS_REWRITE" if there is at least one HIGH severity issue, or three
    or more issues of any severity.

For each issue found, report:
  - severity: "HIGH" (would cause viewers to leave), "MEDIUM", "LOW"
  - category: "hook" | "clarity" | "pacing" | "generic_language" | "emotional_pull" |
    "tts_readability" | "narrative_arc"
  - description: the specific problem, quoting the offending text where useful
  - fix: a concrete, actionable instruction for how to fix it

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "status": "PASSED" | "NEEDS_REWRITE",
  "issues": [
    {"severity": "HIGH" | "MEDIUM" | "LOW", "category": "...", "description": "...", "fix": "..."}
  ]
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. If the script genuinely passes, return an empty issues array.
3. Be specific — quote the actual sentence and say why it fails.\
"""

_QUALITY_REWRITE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title":        {"type": "string"},
        "voice_script": {"type": "string"},
    },
    "required": ["title", "voice_script"],
    "additionalProperties": False,
}

_SCRIPT_QUALITY_REWRITE_BASE = """\
You are a YouTube documentary scriptwriter rewriting a script to fix specific retention
problems identified by an editorial review — WITHOUT losing any facts, structure, or language.

You will receive the current title/voice_script and a list of issues with
concrete fixes. Apply EVERY fix precisely. Do not introduce new problems while fixing old ones.

Rules:
1. Preserve the source language, factual content, and overall story unless an issue
   explicitly requires changing it.
2. Apply the requested fixes fully — especially HIGH severity ones (hook, generic
   language, narrative arc) — these are non-negotiable.
3. Never invent facts, names, dates, statistics, or events not present in the
   current script.
4. Never send a partial script — always return the FULL title and voice_script.
5. Preserve [INTRO], [SECTION N], [OUTRO] markers in voice_script, in the same positions
   unless restructuring is explicitly required by an issue.
6. The rewritten opening must satisfy: first sentence concrete and self-contained, central
   tension clear within the first three sentences, no generic AI-documentary phrasing
   ("This is a story about…", "Everything changed…", "But one question remains…") unless
   immediately grounded in a specific fact.
7. When fixing a narrative_arc issue about repeated or re-explained facts: remove the
   second occurrence entirely rather than rephrasing it. Do not just reword the
   repeated material — cut it, and let the surrounding sentences flow into each other.
8. When fixing a hook issue about revealing the ending: rewrite the opening to
   establish the situation or the sense of danger it creates, without naming what it
   turns out to be. The reveal must stay withheld for later in the script.
9. Fill the title and voice_script fields of the provided tool schema exactly.\
"""

# ── Public functions ───────────────────────────────────────────────────────────

def build_telegram_message(
    title: str,
    url: str,
    assessment: dict | None,
    target_languages: list[str] | None,
    user_language: str,
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

    Returns:
        Formatted Telegram Markdown string ready to send.
    """
    lang_key = (user_language or "en").lower()[:2]
    t = _TELEGRAM_TEMPLATES.get(lang_key, _TELEGRAM_TEMPLATES["en"])

    lines: list[str] = [
        t["header"],
        "",
        f"*{t['title_lbl']}:* {title}",
        f"*{t['source_lbl']}:* {url}",
    ]

    if assessment and isinstance(assessment.get("scores"), dict):
        dims: list[tuple[str, int]] = []
        for name, val in assessment["scores"].items():
            if isinstance(val, (int, float)):
                dims.append((name, int(val)))
            elif isinstance(val, dict):
                dims.append((name, int(val.get("score", 0))))
        dims.sort(key=lambda x: x[1], reverse=True)
        top2 = " · ".join(
            f"{name.replace('_', ' ').title()} ({score}/100)"
            for name, score in dims[:2]
        )
        lines.append(f"*{t['signals_lbl']}:* {top2}")

    if target_languages:
        lines.append(f"*{t['langs_lbl']}:* {' · '.join(lang.upper() for lang in target_languages)}")

    lines.append("")
    lines.append(t["action"])

    return "\n".join(lines)


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
    override_instruction: str = "",
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
        override_instruction: Optional correction instruction appended to the user
                            message (used by the child-Short translation retry loop
                            in ``scripts.py``, mirroring ``generate_short_episode_script``'s
                            existing correction-round pattern).

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
    )
    if ctx:
        user_message += f"\nHOOK_CONTEXT:\n{ctx}\n"
    user_message += f"\nSource voice script:\n{voice_script}"
    if override_instruction:
        user_message += f"\n\n{override_instruction}"
    # Intentional free-form JSON path: native script adaptation is a large text payload.
    # parse_claude_json validates required and allowed keys.
    response = call_claude(prompt, user_message, max_tokens=8192, task="native_adaptation")
    return parse_claude_json(
        response,
        required_keys=["voice_script"],
        type_checks={"voice_script": str},
        allowed_keys=["voice_script"],
    )


def generate_revised_scripts(
    current_scripts: dict,
    feedback: str,
    channel,
    tts_model: str = "sonic-2",
    tts_provider: str = "cartesia",
) -> dict:
    """Revise an existing script based on user feedback (called on CHANGE replies).

    Applies TTS_BLOCK to the revision system prompt so corrections cannot
    reintroduce TTS violations. Returns a ``changes`` array alongside the
    revised script — callers should persist this to script_issues_log.

    Args:
        current_scripts: Dict with ``title``, ``voice_script``.
        feedback:        The raw user feedback text from Telegram.
        channel:         Channel ORM object (provides niche and tone as context).
        tts_model:       TTS model ID for writing constraints.
        tts_provider:    TTS provider ("cartesia" | "elevenlabs").

    Returns:
        Dict with ``title``, ``voice_script``, and ``changes``
        (list of per-section change summaries).

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    prompt = with_tts_block(_REVISION_SYSTEM_PROMPT, tts_provider, tts_model)
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n\n"
        f"Current title: {current_scripts.get('title', '')}\n\n"
        f"Current voice script:\n{current_scripts.get('voice_script', '')}\n\n"
        f"User feedback:\n{feedback}"
    )
    # Intentional free-form JSON path: user-driven revisions may return large scripts.
    # parse_claude_json validates required and allowed keys.
    response = call_claude(prompt, user_message, max_tokens=8192, task="revision")
    return parse_claude_json(
        response,
        required_keys=["title", "voice_script", "changes"],
        type_checks={"title": str, "voice_script": str, "changes": list},
        allowed_keys=["title", "voice_script", "changes"],
    )


def assess_script_quality(scripts: dict, channel, script_format: str = "youtube_long") -> dict:
    """Run the Script Quality Gate — a YouTube-retention review distinct from Agent 3.

    Args:
        scripts:       Dict with ``title``, ``voice_script``.
        channel:       Channel ORM object (provides niche and tone as context).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        Dict with ``status`` ("PASSED" | "NEEDS_REWRITE") and ``issues``.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Title: {scripts.get('title', '')}\n\n"
        f"Voice script:\n{scripts.get('voice_script', '')}"
    )
    # Intentional free-form JSON path: retained to avoid changing quality-gate
    # prompt behavior in this rule-cleanup pass. parse_claude_json validates keys.
    response = call_claude(
        _SCRIPT_QUALITY_SYSTEM_PROMPT, user_message, max_tokens=1536, task="script_quality_check"
    )
    result = parse_claude_json(
        response,
        required_keys=["status", "issues"],
        type_checks={"status": str, "issues": list},
        allowed_keys=["status", "issues"],
    )
    if result["status"] not in {"PASSED", "NEEDS_REWRITE"}:
        raise ValueError(f"assess_script_quality: unexpected status {result['status']!r}")
    return result


def rewrite_script_for_quality(
    scripts: dict,
    issues: list[dict],
    channel,
    script_format: str = "youtube_long",
    tts_model: str = "sonic-2",
    tts_provider: str = "cartesia",
) -> dict:
    """Rewrite a full script to fix issues raised by the Script Quality Gate.

    Applies TTS_BLOCK for the given model so the rewrite cannot introduce new
    TTS compliance violations that would then fail Agent 3's deterministic checks.

    Args:
        scripts:       Dict with ``title``, ``voice_script``.
        issues:        Issue list from ``assess_script_quality()``.
        channel:       Channel ORM object (provides niche and tone as context).
        script_format: Format key from ``channel_config.script_format``.
        tts_model:     TTS model ID for writing constraints.
        tts_provider:  TTS provider ("cartesia" | "elevenlabs").

    Returns:
        Dict with ``title``, ``voice_script`` — fully rewritten.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    prompt = with_tts_block(_SCRIPT_QUALITY_REWRITE_BASE, tts_provider, tts_model)
    issue_lines = "\n".join(
        f"- [{issue.get('severity', '?')}] {issue.get('category', '?')}: "
        f"{issue.get('description', '')} → FIX: {issue.get('fix', '')}"
        for issue in issues
    )
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Current title: {scripts.get('title', '')}\n\n"
        f"Current voice script:\n{scripts.get('voice_script', '')}\n\n"
        f"Issues to fix:\n{issue_lines}"
    )
    result = call_claude_structured(
        task="quality_rewrite",
        system_prompt=prompt,
        user_message=user_message,
        schema_name="quality_rewrite",
        input_schema=_QUALITY_REWRITE_SCHEMA,
        max_tokens=8192,
    )
    for _key in ("title", "voice_script"):
        if not isinstance(result.get(_key), str):
            raise ValueError(f"rewrite_script_for_quality: missing or non-string key '{_key}'")
    return result


# ── Story Scoring Gate (single story) ─────────────────────────────────────────

_SCORING_DIMENSIONS: list[str] = [
    "visual_storytelling_potential",
    "social_media_clickability",
    "opening_scene_strength",
    "thumbnail_strength",
    "scroll_stopper_potential",
    "emotional_stakes",
    "viral_clip_count",
    "central_mystery",
    "curiosity_gap",
    "conflict_or_contradiction",
    "emotional_specificity",
    "title_thumbnail_potential",
    "visual_range",
    "stock_media_feasibility",
    "short_form_clip_potential",
    "comment_section_potential",
    "series_potential",
    "episode_two_potential",
]

_SINGLE_STORY_SCORING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "description": "Integer score 0–100 for each of the 18 dimensions.",
            "properties": {
                dim: {"type": "integer", "minimum": 0, "maximum": 100}
                for dim in _SCORING_DIMENSIONS
            },
            "required": _SCORING_DIMENSIONS,
        }
    },
    "required": ["scores"],
}

_SINGLE_STORY_SCORING_SYSTEM_PROMPT = """\
Score this story's potential to perform on YouTube, TikTok, Instagram Reels, and YouTube Shorts.
You are not deciding whether to accept or reject the story — another system makes that decision.
Score strictly using fixed anchors so the same story always receives the same scores.
Output ONLY the tool schema. No prose, no explanations.

Anchors (apply to all dimensions):
  0–30   = weak / absent (actively hurts the video or makes it unclickable)
  31–65  = moderate (present but needs heavy compensation)
  66–100 = strong (clear asset that makes the video noticeably better)

Dimensions:
  visual_storytelling_potential  Can be SHOWN on screen with 5+ distinct visual categories?
  social_media_clickability      User clicks based on realistic thumbnail + title alone?
  opening_scene_strength         First moment drops viewer into action/danger/contradiction?
  thumbnail_strength             Produces one powerful, nameable thumbnail image?
  scroll_stopper_potential       Opening sentence stops mid-scroll? Concrete + high-stakes?
  emotional_stakes               Named person in real human drama with personal consequence?
  viral_clip_count               Self-contained 30–90 second moments (need 3+)?
  central_mystery                Clear factual mystery or unexplained phenomenon?
  curiosity_gap                  Opening creates factual open question story credibly answers?
  conflict_or_contradiction      Real conflict or factual contradiction (not bland)?
  emotional_specificity          Emotion tied to a specific named person in a specific moment?
  title_thumbnail_potential      Compelling title AND strong nameable visual together?
  visual_range                   Multiple genuinely different visual contexts/environments?
  stock_media_feasibility        Visuals findable on Pexels/Unsplash/Pixabay stock platforms?
  short_form_clip_potential      At least one self-contained punchy 30–90 second moment?
  comment_section_potential      Viewers feel compelled to share strong opinions?
  series_potential               Could generate multiple follow-up videos?
  episode_two_potential          Clear factual "part two" question left unanswered?

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
        Dict with ``scores`` mapping each of the 18 dimensions to an integer 0–100.

    Raises:
        ValueError: If Claude's response is malformed or missing required dimensions.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Story title: {story.title}\n"
        f"Story URL: {story.url}\n"
        f"Metadata: upvotes={story.upvotes}, comments={story.comments}, "
        f"published_at={story.published_at.isoformat()}\n\n"
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


# ── Script auto-correction (moved from agent3_validation) ─────────────────────

_CORRECTION_SYSTEM_PROMPT_BASE = """\
You are a script editor for an automated multilingual video content system.

Your task: correct a specific language's voice script based on a list of
identified issues. Apply ONLY the changes needed to fix the listed issues — do not
rewrite sections that are not affected.

Rules:
1. Preserve all [SECTION N], [INTRO], and [OUTRO] markers in voice_script.
2. Keep the voice_script in the same language as the original.
3. Do not change the story, key facts, or overall narrative. Never invent new facts.
4. If minimum_length is flagged, expand existing sections with more depth, examples, or
   context from the source material excerpt provided in the user message (when present).
   Never pad with filler. The voice_script must reach the minimum word count stated in
   the user message. Match the declared script format's style (documentary pacing for
   youtube_long, short punchy sentences for short-form).
5. When fixing tts_compliance: replace digit-runs with words, remove forbidden characters
   (parentheses, slashes, percent signs, ampersands), expand abbreviations (Dr. → Doctor,
   vs. → versus, etc. → and so on, e.g. → for example), rewrite sentences longer than
   18 words, and convert ALL-CAPS words to mixed case or full form. Fix only the flagged
   sentences — do not touch others.
6. When fixing hook_quality: rewrite ONLY the first sentence of voice_script that
   follows the [INTRO] marker. The replacement sentence must:
     — be ≤12 words (strict — count every word)
     — name one specific person, place, or date drawn from facts already in the script
     — imply an unresolved outcome or open question without stating it explicitly
     — not start with any forbidden opener: "In", "Today", "Have you", "Welcome",
       "What if", "Did you", "Imagine", "This is", "I want", "Let me"
   Do not change any other sentence. Do not add facts not already present in the script.
7. When fixing linguistic_naturalness: rewrite the affected sentences entirely rather
   than patching individual words — half-fixed awkward phrasing is worse than the original.
8. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
   {"voice_script": "..."}\
"""

_CORR_MARKER_LINE_RE = re.compile(
    r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$",
    re.IGNORECASE,
)
_CORR_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_CORR_SPLIT_CONJUNCTIONS = frozenset({
    "and", "but", "or", "so", "yet", "nor", "for", "because", "although",
    "since", "while", "when", "if", "that", "which", "who", "where", "though",
    "et", "mais", "ou", "car", "donc", "ni", "que", "qui", "si",
    "y", "pero", "porque", "aunque", "cuando", "e", "ma", "perché",
})
_CORR_NONWS_RE = re.compile(r"\S+")


def _corr_terminate(s: str) -> str:
    s = s.rstrip()
    return s if s and s[-1] in ".!?" else (s + "." if s else s)


def _corr_capitalize(s: str) -> str:
    return s[0].upper() + s[1:] if s else s


def _corr_bisect_sentence(sent: str) -> tuple[list[str], int]:
    em_idx = sent.find("—")
    if em_idx > 0:
        left  = sent[:em_idx].rstrip()
        right = sent[em_idx + 1:].lstrip()
        if len(left.split()) >= 4 and right:
            return [_corr_terminate(left), _corr_capitalize(right)], 1

    tokens = list(_CORR_NONWS_RE.finditer(sent))
    words  = [m.group() for m in tokens]
    if not words:
        return [sent], 0

    comma_word_indices = [i for i, m in enumerate(tokens) if m.group().endswith(",")]

    def _comma_char_pos(ci: int) -> int:
        return tokens[ci].end() - 1

    for ci in comma_word_indices:
        if ci < 3 or ci >= len(words) - 2:
            continue
        next_word = words[ci + 1].lower().rstrip(".!?,")
        if next_word in _CORR_SPLIT_CONJUNCTIONS:
            comma_pos = _comma_char_pos(ci)
            left  = sent[:comma_pos].rstrip()
            right = sent[comma_pos + 1:].lstrip()
            if right:
                return [_corr_terminate(left), _corr_capitalize(right)], 1

    target = min(15, len(words) - 3)
    best_ci = -1
    best_dist: float = float("inf")
    for ci in comma_word_indices:
        if 3 <= ci <= len(words) - 3:
            d = abs(ci - target)
            if d < best_dist:
                best_dist = d
                best_ci = ci
    if best_ci >= 0:
        comma_pos = _comma_char_pos(best_ci)
        left  = sent[:comma_pos].rstrip()
        right = sent[comma_pos + 1:].lstrip()
        if right:
            return [_corr_terminate(left), _corr_capitalize(right)], 1

    cut = min(15, len(words) - 1)
    cut_pos = tokens[cut].start()
    left  = sent[:cut_pos].rstrip()
    right = sent[cut_pos:].lstrip()
    if right:
        return [_corr_terminate(left), _corr_capitalize(right)], 1

    return [sent], 0


def _corr_process_fragment(frag: str, depth: int = 0) -> tuple[list[str], int]:
    if len(frag.split()) <= 18 or depth >= 3:
        return [frag], 0
    parts, n = _corr_bisect_sentence(frag)
    if len(parts) == 1:
        return [frag], 0
    total = n
    result: list[str] = []
    for part in parts:
        sub_parts, sub_n = _corr_process_fragment(part, depth + 1)
        result.extend(sub_parts)
        total += sub_n
    return result, total


def _corr_process_line(text: str) -> tuple[str, int]:
    fragments = _CORR_SENTENCE_END_RE.split(text)
    result: list[str] = []
    n_splits = 0
    for frag in fragments:
        frag = frag.strip()
        if not frag:
            continue
        parts, n = _corr_process_fragment(frag)
        result.extend(parts)
        n_splits += n
    return " ".join(result), n_splits


def _split_long_sentences_agent2(voice_script: str) -> tuple[str, int]:
    """Post-process voice_script to deterministically split >18-word sentences."""
    out_lines: list[str] = []
    total_splits = 0
    for line in voice_script.split("\n"):
        if _CORR_MARKER_LINE_RE.match(line):
            out_lines.append(line)
            continue
        processed, n = _corr_process_line(line)
        total_splits += n
        out_lines.append(processed)
    return "\n".join(out_lines), total_splits


def auto_correct_script(
    current_scripts: dict,
    issues: list[dict],
    language: str,
    channel,
    script_format: str = "youtube_long",
    source_excerpt: str | None = None,
    tts_model: str = "sonic-2",
    tts_provider: str = "cartesia",
) -> dict:
    """Correct a single language's voice script based on identified MAJOR issues.

    Called for each auto-correction round after deterministic checks flag MAJOR issues.
    TTS_BLOCK for the target voice model is appended so corrections cannot reintroduce
    TTS violations.

    Args:
        current_scripts: Dict with ``voice_script`` for the language.
        issues:          List of issue dicts for this language (MAJOR and MINOR).
        language:        BCP-47 language code (e.g. "fr", "en").
        channel:         Channel ORM object (provides niche and tone).
        script_format:   Format key from ``channel_config.script_format``.
        source_excerpt:  Up to 8 000 chars of original source material — injected into
                         the correction prompt when minimum_length is among the issues.
        tts_model:       TTS model ID for this language's voice.
        tts_provider:    TTS provider ("cartesia" | "elevenlabs").

    Returns:
        Dict with corrected ``voice_script``.

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    min_words = 900 if script_format == "youtube_long" else 420

    issue_lines = "\n".join(
        f"- [{i['severity']}] {i['category']}: {i['description']} → {i['suggestion']}"
        for i in issues
    )

    user_message = (
        f"Language: {language}\n"
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format} (minimum expected voice_script length: {min_words} words)\n\n"
        f"Issues to fix:\n{issue_lines}\n\n"
        f"Current voice script:\n{current_scripts.get('voice_script', '')}"
    )

    has_min_length_issue = any(i.get("category") == "minimum_length" for i in issues)
    if source_excerpt and has_min_length_issue:
        user_message += (
            f"\n\nSource material excerpt — use this to expand the script. "
            f"Do not invent any fact not present here or already in the script:\n"
            f"{source_excerpt[:8000]}"
        )

    correction_prompt = with_tts_block(_CORRECTION_SYSTEM_PROMPT_BASE, tts_provider, tts_model)

    result = call_claude_structured(
        task="auto_correction",
        system_prompt=correction_prompt,
        user_message=user_message,
        schema_name="corrected_scripts",
        input_schema={
            "type": "object",
            "properties": {
                "voice_script": {
                    "type": "string",
                    "description": (
                        "The corrected voice script in the same language as the original. "
                        "All issues listed in the prompt must be fixed."
                    ),
                },
            },
            "required": ["voice_script"],
        },
        max_tokens=8192,
    )

    if not isinstance(result.get("voice_script"), str):
        raise ValueError(
            f"corrected_scripts tool response missing required string field: {list(result.keys())}"
        )

    fixed_voice, n_splits = _split_long_sentences_agent2(result["voice_script"])
    if n_splits:
        logger.debug(
            "TTS backstop: auto_correct_script fixed %d sentence(s) in language=%s",
            n_splits, language,
        )
        result = {**result, "voice_script": fixed_voice}

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
- Each part covers 60–90 seconds of spoken narration (≈160–250 words at Cartesia sonic-2 speed).
- Every part must be independently watchable: a viewer who starts on Part 3 must
  understand the situation from the first 5 seconds without having seen prior parts.
- opening_hook: 1–2 sentences, each ≤15 words, drops the viewer mid-story. No recap.
  Must reference something SPECIFIC from the story — not a generic "wait for it" tease.
- Part N's cliffhanger must be directly answered by Part N+1's main_reveal.
  The final part's cliffhanger is replaced by a comment trigger question (ends with "?").
- Never invent facts not present in the voice script or blueprint.
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
            },
            "minItems": 3,
            "maxItems": 5,
        },
    },
    "required": ["total_parts", "parts"],
}


def generate_shorts_plan(voice_script: str, blueprint: dict, channel) -> dict:
    """Plan 3–5 standalone TikTok episodes from a long-form voice script.

    Uses a Haiku structured call — the output is validated by Python for the
    total_parts range constraint (3 ≤ n ≤ 5). Callers should retry once if the
    constraint fails before giving up.

    Args:
        voice_script: Fully assembled long-form voice script (with markers).
        blueprint:    Blueprint dict from generate_story_blueprint().
        channel:      Channel ORM object (provides niche and tone).

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
        f"Blueprint:\n{json.dumps(blueprint, ensure_ascii=False)}\n\n"
        f"Long-form voice script:\n{voice_script[:8000]}"
    )
    result = call_claude_structured(
        task="shorts_planner",
        system_prompt=_SHORTS_PLANNER_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="shorts_plan",
        input_schema=_SHORTS_PLAN_SCHEMA,
        max_tokens=1024,
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
- Hard limit: 160–250 words. Count every word in voice_script before returning.
  If voice_script exceeds 250 words, cut it — remove the least essential sentences
  until the count is at or below 250. Do not return until the word count is ≤250.
  (250 words ≈ 83 seconds at Cartesia sonic-2 speed.)
- First sentence = the opening_hook from the plan, ≤15 words, drops viewer mid-story.
  If opening_hook or main_reveal already states the story's final answer or mechanism,
  do not restate it that directly here — open on the situation or the unresolved
  question instead, and let the reveal land later in this part's narration.
- Re-hook every 7–10 seconds of narration: a new curiosity gap, question, or micro-reveal
  that prevents the viewer from scrolling away. These are not summaries — they are new angles.
- Provide only the minimum context needed for a first-time viewer to immediately understand the current situation.
  Do not summarize earlier events unless they are essential to understand the current reveal.
- One clear main_reveal per part — this is the payoff for watching this part
- Do not state the same fact or implication twice in this script, even in different
  words. Once something is established, move forward — do not circle back to it.
- End by delivering the planned cliffhanger while preserving its narrative intent — this is what drives the viewer to Part N+1
- Sentence rhythm: short sentences (3–7 words) for tension, longer (8–15 words) for buildup.
  Never 3+ consecutive sentences of the same length.
- No filler, no recap, no "as I mentioned", no "in Part 1"
- No [SECTION N] markers — Short scripts are flat narration only
- ORIGINALITY — this is the most strictly enforced rule in this prompt: you will be
  given the long-form voice script for story grounding only. You must NEVER lift a
  run of 6 or more consecutive words directly from it, even when the long-form
  phrasing is already tight and factual. If a passage in the source is hard to
  paraphrase, that is a signal to find a different angle into the same fact — not
  a reason to copy it. Write this part's narration as if you had never read the
  long-form script word-for-word, only learned the underlying facts from it.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{"title": "Part N title (≤60 chars, TikTok-optimized)", "voice_script": "Full flat narration text"}\
"""


def generate_short_episode_script(
    part_plan: dict,
    long_voice_script: str,
    blueprint: dict,
    channel,
    channel_voice,
    override_instruction: str = "",
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
        override_instruction: Optional correction instruction appended to user message
                              (used by the 2-round auto-correction loop in run_shorts_planner).

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

    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Part: {part_n} of {total_parts}\n\n"
        f"Part plan:\n{part_json}\n\n"
        f"Blueprint:\n{bp_json}\n\n"
        f"Long-form voice script (for FACT GROUNDING ONLY — see ORIGINALITY rule above. "
        f"Do not reuse its exact phrasing):\n"
        f"{long_voice_script[:6000]}"
        )
    if override_instruction:
        user_message += f"\n\nIMPORTANT: {override_instruction}"

    # Intentional free-form JSON path: retained to avoid changing short-script
    # generation behavior in this rule-cleanup pass. parse_claude_json validates keys.
    response = call_claude(
        system_prompt, user_message, max_tokens=1024, task="short_script"
    )
    return parse_claude_json(
        response,
        required_keys=["title", "voice_script"],
        type_checks={"title": str, "voice_script": str},
        allowed_keys=["title", "voice_script"],
    )


# ── Short Quality Gate (Phase 13.2) ─────────────────────────────────────────────
# Holistic AI-judged quality review for standalone child Short narration —
# the Short-shaped counterpart to _SCRIPT_QUALITY_SYSTEM_PROMPT above. Runs only
# after a Short draft has already passed deterministic structural checks
# (_collect_short_script_major_issues — word cap, TTS compliance, hook opener,
# no section markers); this gate judges retention/narrative quality on a
# structurally-valid draft, exactly mirroring how run_script_quality_gate() only
# judges a parent script after its own section-level structural checks pass.

_SHORT_QUALITY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["PASSED", "NEEDS_REWRITE"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity":    {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "category":    {"type": "string"},
                    "description": {"type": "string"},
                    "fix":         {"type": "string"},
                },
                "required": ["severity", "category", "description", "fix"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["status", "issues"],
    "additionalProperties": False,
}

_SHORT_QUALITY_SYSTEM_PROMPT = """\
You are a short-form retention editor reviewing a standalone TikTok/Reels/Shorts episode
script BEFORE production. This is flat, unsectioned narration for a vertical short video —
NOT a long-form documentary script. Do not judge it as one, and do not expect or require
[INTRO], [SECTION N], [OUTRO] markers, or a 1200-1600 word arc. A complete, well-made Short
is 160-250 words of flat narration.

Your only job: decide whether a first-time viewer, with no knowledge of any other part of
this story, would watch this Short all the way through — or whether it needs a rewrite.
You are not checking facts or technical formatting — another system does that.

Judge the script against these dimensions, all specific to short-form vertical video:
  - hook: Does the first 1-2 sentences immediately grab attention with something concrete
    and specific? Would a viewer keep watching past 3 seconds? Does the opening already
    state the part's final answer or mechanism instead of creating a question the rest of
    the Short must answer? If it gives away the reveal early, this is a HIGH severity hook
    issue regardless of how concrete or well-written it is.
  - clarity: Could a viewer who has never seen any other part of this story follow what is
    happening, who is involved, and why it matters, using only this Short's own narration?
  - emotional_pull: Does the viewer have a concrete reason to care, and does the narration
    open a curiosity gap (an unanswered question) rather than just stating facts in order?
  - main_reveal: Is there exactly one clear reveal or payoff in this Short? Flag it as a
    HIGH severity narrative_arc issue if there is no clear reveal, or if two unrelated
    reveals are both compressed into one or two rushed sentences instead of giving the
    more important one room to land.
  - cliffhanger_intent: If this is not the final part, does the ending preserve a genuine
    forward-pulling cliffhanger (a specific unresolved element), rather than resolving
    everything or trailing off on a flat summary? (Only the literal wording may differ
    from the plan — the narrative intent of the cliffhanger must survive.)
  - recap: Does the narration over-explain or summarize events beyond the minimum context
    a first-time viewer needs for this part's own reveal to land? Re-stating an established
    fact, even in different words, is a HIGH severity recap issue.
  - generic_language: Does it contain stock AI-narration filler ("This is a story about…",
    "What happened next…", "Everything changed…", "Little did they know", "But that's not
    all") used as a crutch instead of a grounded specific?
  - tts_readability: Will this sound natural and human when read aloud by a TTS voice in
    roughly 80-90 seconds?
  - retention_suitability: Does the narration re-hook the viewer every 7-10 seconds with a
    new fact, twist, or micro-reveal, or does any stretch of the script plateau with no new
    information?

Use FIXED, repeatable criteria — do not be lenient or harsh based on mood.

Decision rule:
  - status = "PASSED" only if a first-time viewer would plausibly watch this Short to the
    end on its own, with no other context, AND contains no HIGH severity issue in any
    dimension.
  - status = "NEEDS_REWRITE" if there is at least one HIGH severity issue, or three or
    more issues of any severity.

For each issue found, report:
  - severity: "HIGH" (would cause viewers to scroll away), "MEDIUM", "LOW"
  - category: "hook" | "clarity" | "emotional_pull" | "main_reveal" | "cliffhanger_intent" |
    "recap" | "generic_language" | "tts_readability" | "retention_suitability"
  - description: the specific problem, quoting the offending text where useful
  - fix: a concrete, actionable instruction for how to fix it

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "status": "PASSED" | "NEEDS_REWRITE",
  "issues": [
    {"severity": "HIGH" | "MEDIUM" | "LOW", "category": "...", "description": "...", "fix": "..."}
  ]
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. If the script genuinely passes, return an empty issues array.
3. Be specific — quote the actual sentence and say why it fails.
4. Never require or suggest adding [INTRO], [SECTION N], [OUTRO], or any other bracketed
   structural marker — flat narration is correct for a Short, not a defect.\
"""


def assess_short_script_quality(voice_script: str, channel, is_final_part: bool = True) -> dict:
    """Run the Short Quality Gate — a short-form retention review for one child Short.

    The flat-narration counterpart to ``assess_script_quality()``. Runs only after a
    Short draft has already passed deterministic structural checks (word cap, TTS
    compliance, hook opener, no section markers) — see ``_collect_short_script_major_issues()``
    in ``scripts.py``.

    Args:
        voice_script:  The Short's flat narration text (no section markers).
        channel:       Channel ORM object (provides niche and tone as context).
        is_final_part: Whether this is the last part of the standalone-Short series.
                      When True, the cliffhanger_intent dimension is not scored — the
                      final part replaces its cliffhanger with a comment-trigger
                      question (see ``generate_shorts_plan()``'s schema), so there is
                      no forward-pulling cliffhanger to judge.

    Returns:
        Dict with ``status`` ("PASSED" | "NEEDS_REWRITE") and ``issues``.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    cliffhanger_note = (
        "This is the FINAL part — it ends on a comment-trigger question, not a "
        "cliffhanger. Do not score cliffhanger_intent for this part."
        if is_final_part else
        "This is NOT the final part — it must end on a genuine forward-pulling "
        "cliffhanger. Score cliffhanger_intent normally."
    )
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"{cliffhanger_note}\n\n"
        f"Voice script:\n{voice_script}"
    )
    result = call_claude_structured(
        task="short_quality_check",
        system_prompt=_SHORT_QUALITY_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="short_quality_check",
        input_schema=_SHORT_QUALITY_SCHEMA,
        max_tokens=1024,
    )
    if result["status"] not in {"PASSED", "NEEDS_REWRITE"}:
        raise ValueError(f"assess_short_script_quality: unexpected status {result['status']!r}")
    return result
