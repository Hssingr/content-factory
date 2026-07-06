import logging
import re

from app.services.claude_client import call_claude_structured
from app.agents.agent2_discovery.services.story import MAX_SOURCE_EXCERPT_CHARS

logger = logging.getLogger(__name__)

PROMPT_VERSION = "4.6"  # v4.6: Elimination Mandate extension (post-roadmap deep audit) —
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
- Preserve the source's narration POV (the "Narration POV" value in the user message
  describes it): if the source narrates in first person ("I"/"me"/"my"), the adaptation
  must too; if it narrates in third person, keep it in third person. Never convert one
  to the other.

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
  niche and Channel tone values provided below (provided below). Horror/thriller/mystery: favor dread,
  withheld information, and escalating unease. Documentary/educational: favor
  clarity and context. Match the configured niche — do not default to a neutral
  documentary register regardless of niche.
- Phrase hook, central_question, and final_payoff so they can be delivered in the
  Narration POV value provided below: for "first_person_storytime", phrase them as
  something that happened to the narrator ("I heard it three nights in a row"), not
  as an observation about someone else. For "third_person" (default), phrase them
  about the story's people using third-person pronouns and names, as usual.

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
        "midpoint_retention_trap": {"type": "string"},
        "suggested_section_count": {"type": "integer", "minimum": 2, "maximum": 5},
        "suggested_title":        {"type": "string"},
    },
    "required": [
        "hook", "central_question", "major_turns", "final_payoff", "midpoint_retention_trap",
        "comment_trigger", "suggested_section_count", "suggested_title",
    ],
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
        comment_trigger, suggested_section_count, suggested_title.

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
        # that. max_tokens is a cap, not a spend — 1024 is free headroom
        # against a truncated forced-tool-use response.
        max_tokens=1024,
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
  - The comment-trigger sentence must feel unique to this story, not like a reusable
    channel CTA. Avoid generic endings such as "what would you do?" or "would you
    go back?" unless the concrete story-specific noun, place, or consequence is included.
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
    primary_required_turn: str | None = None,
    future_uncovered_turns: list[str] | None = None,
    visual_style: str = "",
    image_style: str = "",
    narration_pov: str = "third_person",
    midpoint_retention_trap: str | None = None,
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
        f"Visual style: {visual_style or 'story_driven'}\n"
        f"Image style: {image_style or 'photorealistic'}\n"
        f"Narration POV: {narration_pov or 'third_person'}\n"
        f"Script format: {script_format}\n\n"
        f"Blueprint:\n{blueprint_json}\n\n"
        f"Prior sections summary:\n{prior_json}\n\n"
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
    return call_claude_structured(
        task="section_generation",
        system_prompt=system_prompt,
        user_message=user_message,
        schema_name="section_output",
        input_schema=_SECTION_GENERATION_SCHEMA,
        max_tokens=3072,
    )


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
        "signals_lbl": "Signaux principaux",
        "langs_lbl":   "Langues",
        "rights_lbl":  "Revue droits/IP",
        "action":      "Répondez *APPROVE* pour valider, ou décrivez ce que vous souhaitez changer.",
    },
    "en": {
        "header":      "📺 New story found",
        "title_lbl":   "Title",
        "source_lbl":  "Source",
        "signals_lbl": "Top signals",
        "langs_lbl":   "Languages",
        "rights_lbl":  "Rights/IP review",
        "action":      "Reply *APPROVE* to proceed, or describe what you would like to change.",
    },
    "es": {
        "header":      "📺 Nuevo contenido encontrado",
        "title_lbl":   "Título",
        "source_lbl":  "Fuente",
        "signals_lbl": "Señales principales",
        "langs_lbl":   "Idiomas",
        "rights_lbl":  "Revisión derechos/IP",
        "action":      "Responde *APPROVE* para continuar, o describe lo que quieres cambiar.",
    },
    "it": {
        "header":      "📺 Nuovo contenuto trovato",
        "title_lbl":   "Titolo",
        "source_lbl":  "Fonte",
        "signals_lbl": "Segnali principali",
        "langs_lbl":   "Lingue",
        "rights_lbl":  "Revisione diritti/IP",
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


_REVISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title":        {"type": "string"},
        "voice_script": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section":         {"type": "string"},
                    "before_summary":  {"type": "string"},
                    "after_summary":   {"type": "string"},
                },
                "required": ["section", "before_summary", "after_summary"],
            },
        },
    },
    "required": ["title", "voice_script", "changes"],
    "additionalProperties": False,
}


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
    return call_claude_structured(
        task="revision",
        system_prompt=prompt,
        user_message=user_message,
        schema_name="revision_output",
        input_schema=_REVISION_SCHEMA,
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
    "image_generation_feasibility",
    "short_form_clip_potential",
    "comment_section_potential",
    "series_potential",
    "episode_two_potential",
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
        }
    },
    "required": ["scores"],
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
  image_generation_feasibility   Key moments can be depicted as distinct concrete generated images?
  short_form_clip_potential      At least one self-contained punchy 30–90 second moment?
  comment_section_potential      Viewers feel compelled to share strong opinions?
  series_potential               Could generate multiple follow-up videos?
  episode_two_potential          Clear factual "part two" question left unanswered?
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


# ── Standalone short planning: Shorts Planner ──────────────────────────────────────────────────

_SHORTS_PLANNER_SYSTEM_PROMPT = """\
You are a Short-form content strategist planning how to split a long-form story into
3–5 standalone TikTok episodes.

Your task: read the source story (voice script + blueprint) and produce a part plan.

Rules:
- total_parts must be between 3 and 5 (inclusive). Never fewer than 3 or more than 5.
- Split at narrative boundaries: reveals, discoveries, reversals, or escalations.
  Never split primarily by time — narrative logic is paramount.
- Each part covers 60–90 seconds of spoken narration (≈125–180 words at the measured
  ~120 wpm real Short narration rate).
- Every part must be independently watchable: a viewer who starts on Part 3 must
  understand the situation from the first 5 seconds without having seen prior parts.
- opening_hook: 1–2 sentences, each ≤15 words, drops the viewer mid-story. No recap.
  Must reference something SPECIFIC from the story — not a generic "wait for it" tease.
- Part N's cliffhanger must be directly answered by Part N+1's main_reveal.
  The final part's cliffhanger is replaced by a comment trigger question (ends with "?").
  That final question must be unique to this story and must not copy blueprint.comment_trigger
  verbatim or near-verbatim; avoid reusable CTAs such as "what would you do?".
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
- Hard limit: 125–180 words. Count every word in voice_script before returning.
  If voice_script exceeds 180 words, cut it — remove the least essential sentences
  until the count is at or below 180. Do not return until the word count is ≤180.
  (180 words ≈ 90 seconds at the measured ~120 wpm real Short narration rate —
  calibrated from production audio, not the raw "words per second" of the TTS voice.)
- First sentence = the opening_hook from the plan, ≤15 words, drops viewer mid-story.
  If opening_hook or main_reveal already states the story's final answer or mechanism,
  do not restate it that directly here — open on the situation or the unresolved
  question instead, and let the reveal land later in this part's narration.
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
- Do not state the same fact or implication twice in this script, even in different
  words. Once something is established, move forward — do not circle back to it.
- End by delivering the planned cliffhanger while preserving its narrative intent — this is what drives the viewer to Part N+1
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


_SHORT_EPISODE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title":        {"type": "string", "description": "Part N title (≤60 chars, TikTok-optimized)."},
        "voice_script": {"type": "string", "description": "Full flat narration text, 125-180 words."},
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
        f"Visual style: {visual_style or 'story_driven'}\n"
        f"Image style: {image_style or 'photorealistic'}\n"
        f"Narration POV: {narration_pov or 'third_person'}\n"
        f"Part: {part_n} of {total_parts}\n\n"
        f"Part plan:\n{part_json}\n\n"
        f"Blueprint:\n{bp_json}\n\n"
        f"CTA guardrail: do not copy blueprint.comment_trigger verbatim or near-verbatim; "
        f"write a story-specific final question for this part only.\n\n"
        f"Long-form voice script (for FACT GROUNDING ONLY — see ORIGINALITY rule above. "
        f"Do not reuse its exact phrasing):\n"
        f"{long_voice_script[:6000]}"
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
