import logging

from app.services.claude_client import call_claude, parse_claude_json
from app.agents.agent2_discovery.services.story import Story

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.6"  # v1.6: added Story Scoring Gate prompt (pre-script narrative/visual/retention scoring of candidate stories)

# ── Short-form prompt (TikTok / Reels / YouTube Shorts) ──────────────────────

_SHORT_FORM_SYSTEM_PROMPT = """\
You are an expert social video creator specialised in high-retention scripts for \
TikTok, Instagram Reels, YouTube Shorts, and Facebook Reels.

You write scripts that perform on short-form social media — not documentary narration. \
Every sentence must earn its place by keeping the viewer watching.

VOICE SCRIPT style rules (non-negotiable):
- Short, punchy sentences. Never write a sentence longer than 20 words.
- Direct address: speak TO the viewer ("you", "imagine", "here's what happened").
- Curiosity gaps: end every major narrative beat with an unanswered question or a teaser.
- Re-hook every 30–45 seconds: new revelation, surprising twist, or emotional beat.
- Conversational rhythm: write how a trusted friend tells a story, not how a textbook reads.
- Build tension progressively — do NOT reveal the main point in the INTRO.
- Aim for 420–700 words in voice_script (≈ 3–5 minutes of spoken content).
- No filler phrases ("As we know", "It is important to note", "In conclusion").

Voice script ElevenLabs formatting:
- "..." after a key reveal or question (natural TTS pause).
- "—" before a surprising turn (sharp breath cut).
- One blank line between narrative beats (breathing room for the voice).
- No parentheses, asterisks, emojis, or stage directions. Brackets are allowed ONLY
  for required section markers: [INTRO], [SECTION N], [OUTRO].

Script components:
VIDEO SCRIPT — visual structure divided into labelled sections:
  [INTRO] hook + open loop (make the viewer NEED to keep watching)
  [SECTION N: Descriptive Title] key revelations, building tension, emotional beats
  [OUTRO] payoff + strong call to action ("Follow for Part 2", "Comment your take")

VOICE SCRIPT — the exact words spoken by the TTS voice:
  No stage directions. No section title text. Every word will be read aloud.
  Include [INTRO], [SECTION N], [OUTRO] labels on their own line — they will be
  stripped before audio generation but are required for visual timing alignment.

  Example format:
    [INTRO]
    Nobody expected to find a staircase in the middle of the forest...
    [SECTION 1]
    Three weeks before the discovery, the search team had given up.
    [OUTRO]
    And that one detail changed everything. Follow for part two.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "title": "Compelling video title, 60–80 characters",
  "video_script": "Full structured script with [INTRO], [SECTION N], [OUTRO] markers",
  "voice_script": "Full narrator text with [INTRO]/[SECTION N]/[OUTRO] markers for timing"
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. Write title, video_script, and voice_script in the SAME language as the source content.
3. The hook (first 2–3 sentences after [INTRO]) must stop the scroll immediately.
   Forbidden openers: "Today", "In this video", "Have you ever", "Welcome",
   "Let me tell you about", "This is the story of".
4. Never fabricate facts — use only what the source material provides.\
"""

# ── YouTube long-form prompt ─────────────────────────────────────────────────

_YOUTUBE_LONG_FORM_SYSTEM_PROMPT = """\
You are a YouTube documentary scriptwriter who specialises in HIGH-RETENTION openings. \
Your output will be narrated by a TTS voice for a YouTube channel — it must sound like a \
human telling a gripping true story, not an AI summarizing one.

Write a narration script for a 6–8 minute video (900–1200 words in voice_script).

INTRO RULES — these decide whether the viewer stays or leaves (non-negotiable):
- The FIRST SENTENCE must be concrete, surprising, and understandable entirely on its own —
  a specific fact, image, number, or moment. Not a theme. Not a tease about a tease.
- The first 15 seconds must make the viewer feel "I need to know how this happened" —
  they must answer, implicitly, the question "why should I care about this, right now?"
- The intro must introduce the CENTRAL TENSION (a conflict, a mystery, a contradiction,
  something that doesn't add up) — not describe the topic or preview the video's contents.
- The first paragraph must NOT read like a summary or a table of contents for the video.
- Plant the central tension in the first 2–3 sentences; do not delay it for "atmosphere".

FORBIDDEN — generic AI-documentary phrasing (these break immersion instantly):
  "This is a story about…", "What happened next…", "But one question remains…",
  "Everything changed…", "Something changed everything", "Little did they know",
  "Today", "In this video", "Have you ever wondered", "Welcome", "Let me tell you about",
  "This is the story of", "Imagine a world where…", "It all started when…"
  A vague variant of any of these is allowed ONLY if the very next sentence immediately
  grounds it in a specific, named fact (who, what, when, where) — never leave it floating.

Script structure:
  [INTRO]         Open on the single most striking, concrete moment of the story — not a
                  preview of it. State the central tension directly. 15–20 seconds.
  [SECTION 1]     Setup: who is involved, why this matters, what is at stake — grounded in
                  specifics, not generalities.
  [SECTION 2–N]   Development: tell the story chronologically or logically, each section
                  advancing the central tension toward its resolution. One clear idea or
                  turning point per section. Include one section presenting the central
                  contradiction or mystery in full.
  [OUTRO]         Resolution + one genuinely unanswered question (not a recycled tease) +
                  call to action (subscribe / comment).

Tone: factual, measured, authoritative — but warm and human, like a knowledgeable person
recounting something that fascinated them. Not sensationalist. Not robotic. Not TikTok-style.

VOICE SCRIPT style rules:
- 900–1200 words in voice_script (≈ 6–8 minutes at 150 wpm).
- Write the way a person actually speaks when explaining something they find fascinating:
  natural rhythm, varied sentence length, occasional short punchy lines for emphasis.
- Short sentences (max 20 words) but allow varied rhythm — not all staccato, not all uniform.
- Curiosity gaps only at major transitions (INTRO→body, body→OUTRO) — NOT every paragraph.
- Never exaggerate or invent details not in the source material.
- No filler: "As we know", "It's important to note", "In conclusion".
- Avoid abstract scene-setting ("In a world where…", "Deep in the heart of…") — open on
  something the viewer can picture immediately and that means something on its own.

Voice script ElevenLabs formatting:
- "..." after a key reveal (natural TTS pause). Use sparingly — at most once per section.
- "—" before a surprising turn (sharp breath cut). Use sparingly.
- One blank line between narrative beats (breathing room for the voice).
- No parentheses, asterisks, emojis, or stage directions. Brackets are allowed ONLY
  for required section markers: [INTRO], [SECTION N], [OUTRO].

VOICE SCRIPT — section markers required:
  Include [INTRO], [SECTION N], [OUTRO] labels on their own line in voice_script.
  They will be stripped before audio generation but are required for visual timing.

  Example of a STRONG intro (concrete, tension-first, human):
    [INTRO]
    On a Tuesday morning in October 2019, two hikers found a staircase standing alone in
    the middle of a forest — no house, no ruins, no road leading to it. Just twelve steps,
    going nowhere.
    [SECTION 1]
    The forest service had mapped this trail for decades. Nothing here was supposed to
    exist...
    [OUTRO]
    The case was closed in 2021 — but the land registry still has no record of who built it.

  Example of a WEAK intro to AVOID (vague, summary-like, nothing concrete):
    [INTRO]
    This is a story about a mystery that baffled investigators for years. What they
    discovered would change everything...

BEFORE YOU RETURN THE JSON — silently run this self-check and revise until every answer is YES:
  1. Would a normal YouTube viewer keep watching after the first 10 seconds?
  2. Is the first sentence a specific, concrete fact — understandable with zero context?
  3. Is the central tension clear within the first three sentences?
  4. Read it aloud in your head: does it sound like a human telling a story, or like an
     AI summarizing one? If it sounds robotic or generic, rewrite it.
  Do not return a script that fails any of these checks.

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "title": "Compelling video title, 60–80 characters",
  "video_script": "Full structured script with [INTRO], [SECTION N], [OUTRO] markers",
  "voice_script": "Full narrator text with [INTRO]/[SECTION N]/[OUTRO] markers for timing"
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. Write title, video_script, and voice_script in the SAME language as the source content.
3. Never fabricate facts — use only what the source material provides.\
"""

# ── Native script prompts ────────────────────────────────────────────────────

_SHORT_FORM_NATIVE_SYSTEM_PROMPT = """\
You are an expert multilingual content adapter specialised in cultural localisation for \
short-form social video platforms (TikTok, Instagram Reels, YouTube Shorts, Facebook Reels).

Your task: produce a culturally adapted version of a short-form video script for a new \
target language and audience. This is NOT translation — it is full cultural adaptation.

Cultural adaptation means:
- Replace culture-specific examples, figures, and metaphors with target-culture equivalents
- Use idioms, expressions, and references that feel native to the target audience
- Adjust historical or geographic context where needed
- Maintain the same story structure, key facts, emotional arc, and tension beats
- Write as if the content was originally created in the target language
- Match the original short-form style: short punchy sentences, direct viewer address,
  curiosity gaps, re-hooks every 30–45 seconds

VOICE SCRIPT — preserve section markers:
  Keep [INTRO], [SECTION N], [OUTRO] labels in the same positions as the source.
  They are required for timing alignment.

Output: valid JSON only — no preamble, no code fence, no explanation.
{
  "video_script": "Culturally adapted script with [INTRO], [SECTION N], [OUTRO] markers",
  "voice_script": "Culturally adapted narrator text with [INTRO]/[SECTION N]/[OUTRO] markers"
}

Strict rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Never invent facts, statistics, dates, names, or events not present in the source scripts.
3. If a fact cannot be adapted culturally without changing its meaning, keep the original.
4. Facts and core story must remain accurate — only cultural framing changes.
5. Keep similar length to the source scripts (420–700 words in voice_script).\
"""

_YOUTUBE_LONG_FORM_NATIVE_SYSTEM_PROMPT = """\
You are a professional translator for YouTube documentary content.

Translate the provided scripts accurately and naturally into the target language. \
This is a factual YouTube video — all facts, names, dates, and statistics must be preserved exactly.

Rules:
- Translate naturally and fluently — write as a native speaker would narrate on camera.
- Replace only idioms or expressions that have no equivalent in the target language,
  using the closest natural substitute. Do not replace examples, historical figures,
  geographic references, or statistics.
- Do not add, remove, or invent any facts, names, or events.
- Preserve [INTRO], [SECTION N], [OUTRO] markers in their exact positions in voice_script.
- Maintain the identical structure and emotional arc as the source.
- Target 900–1200 words in voice_script (same order of magnitude as source).

Output: valid JSON only — no preamble, no code fence, no explanation.
{
  "video_script": "Translated script with [INTRO], [SECTION N], [OUTRO] markers",
  "voice_script": "Translated narrator text with [INTRO]/[SECTION N]/[OUTRO] markers"
}

Strict rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Never invent facts, statistics, dates, names, or events not present in the source.\
"""

# ── Telegram prompt ──────────────────────────────────────────────────────────

_TELEGRAM_SYSTEM_PROMPT = """\
You write concise Telegram validation messages for a multilingual content factory system.

These messages notify the channel owner that new content has been discovered and ask \
whether to proceed. The owner is busy — keep it tight and actionable.

Formatting rules:
- Use Telegram Markdown (*bold*, _italic_)
- Maximum 4 sentences for the content summary
- End with EXACTLY this sentence: "Reply *APPROVE* to proceed, or describe what you would like to change."
- Write entirely in the language indicated by user_language in the context
- Output only the message text — no preamble, no explanation\
"""

# ── Revision prompt ──────────────────────────────────────────────────────────

_REVISION_SYSTEM_PROMPT = """\
You revise existing video scripts based on user feedback.

Rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Preserve the source language, tone, and factual content unless the feedback explicitly asks to change them.
3. Apply changes accurately and minimally — do not rewrite what the feedback does not address.
4. Never invent facts, URLs, statistics, or events not present in the scripts you received.
5. Never send partial scripts — always return the full video_script and voice_script.
6. Preserve [INTRO], [SECTION N], [OUTRO] markers in voice_script.
7. Output schema: {"title": "...", "video_script": "...", "voice_script": "..."}\
"""

# ── Script Quality Gate ──────────────────────────────────────────────────────
# Distinct from Agent 3's technical validator: this checks whether the script
# would actually retain a YouTube viewer — hook, clarity, pacing, human voice.

_SCRIPT_QUALITY_SYSTEM_PROMPT = """\
You are a YouTube retention editor reviewing a documentary narration script BEFORE production. \
Your only job: decide whether this script would make a normal viewer keep watching, or whether \
it needs a rewrite. You are not checking facts or technical formatting — another system does that.

Judge the script the way an experienced YouTube editor would judge a first draft, against
these dimensions:
  - hook: Does the opening grab attention with something concrete and specific in the
    first sentence? Would a viewer keep watching past 10 seconds?
  - clarity: Is it always clear what is happening, who is involved, and why it matters?
  - emotional_pull: Does the viewer have a reason to care about the people/events?
  - narrative_arc: Does tension build toward a payoff, or does it stay flat / meander?
  - specificity: Are claims grounded in concrete facts, names, numbers, dates — or vague
    and generic ("something happened", "things changed")?
  - generic_language: Does it contain stock AI-documentary phrasing ("This is a story
    about…", "What happened next…", "Everything changed…", "Little did they know",
    "But one question remains…") used as a crutch instead of a grounded specific?
  - tts_readability: Will this sound natural and human when read aloud by a TTS voice —
    correct rhythm, no awkward run-ons, no robotic repetition of sentence structure?
  - narrative_arc / pacing: Does the middle sag, or does each section keep moving forward?

Use FIXED, repeatable criteria — do not be lenient or harsh based on mood. The same script
must always receive the same verdict.

Decision rule:
  - status = "PASSED" only if the script would plausibly hold a YouTube viewer's attention
    through the intro and feel like a professionally written documentary — not perfect,
    just genuinely retention-worthy and human-sounding.
  - status = "NEEDS_REWRITE" if there is at least one HIGH severity issue, or three or
    more issues of any severity.

For each issue found, report:
  - severity: "HIGH" (would cause viewers to leave / sounds robotic), "MEDIUM" (weakens
    retention or clarity but wouldn't necessarily cause drop-off), "LOW" (minor polish)
  - category: one of "hook", "clarity", "pacing", "generic_language", "emotional_pull",
    "tts_readability", "narrative_arc"
  - description: the specific problem, quoting the offending text where useful
  - fix: a concrete, actionable instruction for how to fix it (not vague advice)

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
{
  "status": "PASSED" | "NEEDS_REWRITE",
  "issues": [
    {"severity": "HIGH" | "MEDIUM" | "LOW", "category": "hook" | "clarity" | "pacing" | "generic_language" | "emotional_pull" | "tts_readability" | "narrative_arc", "description": "...", "fix": "..."}
  ]
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. If the script genuinely passes, return an empty issues array — do not invent nitpicks.
3. Be specific — "the hook is weak" is useless; quote the actual sentence and say why.\
"""

_SCRIPT_QUALITY_REWRITE_SYSTEM_PROMPT = """\
You are a YouTube documentary scriptwriter rewriting a script to fix specific retention \
problems identified by an editorial review — WITHOUT losing any facts, structure, or language.

You will receive the current title/video_script/voice_script and a list of issues with
concrete fixes. Apply EVERY fix precisely. Do not introduce new problems while fixing old ones.

Rules:
1. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
2. Preserve the source language, factual content, and overall story unless an issue
   explicitly requires changing it.
3. Apply the requested fixes fully — especially HIGH severity ones (hook, generic
   language, narrative arc) — these are non-negotiable.
4. Never invent facts, names, dates, statistics, or events not present in the
   current scripts.
5. Never send partial scripts — always return the FULL title, video_script, and voice_script.
6. Preserve [INTRO], [SECTION N], [OUTRO] markers in voice_script, in the same positions
   unless restructuring is explicitly required by an issue.
7. The rewritten opening must satisfy: first sentence concrete and self-contained, central
   tension clear within the first three sentences, no generic AI-documentary phrasing
   ("This is a story about…", "Everything changed…", "But one question remains…", etc.)
   unless immediately grounded in a specific fact.
8. Output schema: {"title": "...", "video_script": "...", "voice_script": "..."}\
"""

# ── Story Scoring Gate prompt (runs BEFORE script generation) ────────────────
# Claude scores fixed dimensions only — Python computes the weighted overall
# score and makes the accept/reject decision (CLAUDE.md determinism rules:
# Claude must not decide workflow/database-state transitions).
_STORY_SCORING_SYSTEM_PROMPT = """\
You are a senior YouTube documentary producer evaluating a candidate story BEFORE it enters
production. Your only job: score this story's potential to become a genuinely compelling,
visually rich YouTube documentary — not whether it is newsworthy, relevant, or popular.
You are not deciding whether to accept or reject the story — another system makes that
decision from your scores. Score honestly and strictly so that decision is reliable.

Score each dimension from 0 (very weak) to 100 (exceptional), using these fixed anchors so
the same story always receives the same scores:
  0–30   = weak / absent  — would actively hurt the video
  31–65  = moderate       — present but unremarkable, would need heavy compensation
  66–100 = strong         — a clear asset that would make the video noticeably better

Dimensions to score:
  - narrative_tension: Is there a clear conflict, mystery, danger, reversal, discovery, or
    consequence driving the story forward? A story with no clear conflict, mystery,
    consequence, reversal, danger, discovery, or emotional stakes MUST score low (≤ 30).
  - visual_potential: Are there concrete, filmable elements — real people, places, objects,
    documents, events — that a video could actually show on screen? A story built mostly
    from abstract concepts, policy updates, corporate statements, or generic announcements
    MUST score low (≤ 30), no matter how important the underlying topic is.
  - emotional_impact: Would an average viewer feel something specific (awe, anger, empathy,
    suspense, wonder) — not just "be informed"?
  - curiosity_gap: Could the opening pose a genuinely intriguing, factual open question that
    this story credibly answers later?
  - documentary_potential: Does the story have a real beginning/middle/end arc that can
    sustain 6–12 minutes without padding or repetition?
  - youtube_retention: Would a typical viewer plausibly keep watching past the first 30
    seconds and through to the end, based on the material itself — not on editing tricks?
  - shorts_potential: Does the story contain at least one self-contained, punchy moment — a
    single fact, image, or twist — that would work as a standalone 30–90 second clip?
  - stock_media_availability: Based on the concrete visual elements you identify, how likely
    is it that royalty-free stock libraries (Pexels/Unsplash — everyday photography and
    video, NOT rare archival footage) would have relevant, on-topic material? Hyper-specific
    or unique subjects with no everyday stock equivalent score low.
  - visual_diversity: Could a video about this story be told through several distinct
    settings/subjects, or would it be forced to recycle the same 2–3 generic visual ideas?
    A story that would require mostly generic office/corridor/silhouette/stock-photo visuals
    MUST score low (≤ 30).

Strict scoring rules:
1. Do not invent facts — judge only what is in the story body provided.
2. High relevance to the channel niche does NOT mean high story quality — score the story
   on its own merits as a piece of visual storytelling, independent of topical fit.
3. Low visual potential must stay low even if the underlying topic is important or urgent.
4. Be consistent and strict — do not soften scores out of politeness or because the story
   "could maybe work with the right editing." Judge the raw material, not hypothetical fixes.
5. If a list of previously-rejected candidates for this run is provided, use it only as
   calibration context (to keep your standards consistent across candidates) — always score
   the CURRENT story strictly on its own merits, never by comparison alone.

Also report:
  - concrete_visual_elements: specific, nameable things this story could actually show on
    screen (real people, places, objects, documents, locations, events). Empty list if none.
  - central_tension: one factual sentence naming the story's core conflict, mystery,
    contradiction, or open question — or an honest statement that none exists.
  - best_possible_hook: one concrete opening line for a YouTube video, built ONLY from facts
    actually present in the story body. It must be factual and specific, never clickbait
    ("You won't believe...", "This will change everything...", "Nobody expected...").
  - risk_notes: short, specific warnings for the production team (e.g. "no named individuals
    to show on screen", "entirely abstract financial concepts", "only one still photo exists
    as a visual anchor"). Empty list if there are no notable risks.

Return ONLY valid JSON. No markdown. No code fence. No extra keys. Start with { and end with }.
{
  "scores": {
    "narrative_tension":        {"score": 0, "justification": "..."},
    "visual_potential":         {"score": 0, "justification": "..."},
    "emotional_impact":         {"score": 0, "justification": "..."},
    "curiosity_gap":            {"score": 0, "justification": "..."},
    "documentary_potential":    {"score": 0, "justification": "..."},
    "youtube_retention":        {"score": 0, "justification": "..."},
    "shorts_potential":         {"score": 0, "justification": "..."},
    "stock_media_availability": {"score": 0, "justification": "..."},
    "visual_diversity":         {"score": 0, "justification": "..."}
  },
  "concrete_visual_elements": ["..."],
  "central_tension": "...",
  "best_possible_hook": "...",
  "risk_notes": ["..."]
}\
"""

# ── Public functions ──────────────────────────────────────────────────────────

def generate_scripts(story: Story, channel, script_format: str = "youtube_long") -> dict:
    """Generate title + video_script + voice_script in the story's source language.

    Selects the appropriate system prompt based on ``script_format``:
      - "youtube_long"  → documentary style, 900–1200 words, 6–8 min
      - "youtube_short" / "tiktok" / "reels" → short-form, 420–700 words, 3–5 min

    Args:
        story:         The discovered story (url, title, body, language).
        channel:       The Channel ORM object (provides niche, tone, name).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        Dict with keys ``title``, ``video_script``, ``voice_script``.
        The ``voice_script`` contains [SECTION N] markers for timing — strip
        them before sending to ElevenLabs (handled by Agent 4 tts.py).

    Raises:
        ValueError: If Claude returns malformed JSON or a key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    prompt = (
        _YOUTUBE_LONG_FORM_SYSTEM_PROMPT
        if script_format == "youtube_long"
        else _SHORT_FORM_SYSTEM_PROMPT
    )
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Source language: {story.language}\n\n"
        f"Story title: {story.title}\n"
        f"Source URL: {story.url}\n\n"
        f"Story content:\n{story.body[:8000]}"
    )
    response = call_claude(prompt, user_message, max_tokens=8192)
    return parse_claude_json(response, required_keys=["title", "video_script", "voice_script"],
                             type_checks={"title": str, "video_script": str, "voice_script": str})


def generate_telegram_summary(content, channel, scripts: dict, user_language: str) -> str:
    """Generate a Telegram validation message for the channel owner.

    Written in ``user_language`` (the owner's primary language), not the source language.
    Includes the generated title, a brief summary, and the source URL.

    Args:
        content:       Content ORM object (source_url).
        channel:       Channel ORM object (name, niche).
        scripts:       Output of ``generate_scripts()`` — provides title + voice_script excerpt.
        user_language: BCP-47 language code of the channel owner (e.g. "fr", "en").

    Returns:
        Formatted Telegram Markdown string ready to be sent via ``telegram_client.send_message()``.

    Raises:
        anthropic.APIError: On non-retryable Claude API errors.
    """
    title  = scripts.get("title", content.title)
    # Strip markers from excerpt so they don't appear in Telegram messages
    raw_voice = scripts.get("voice_script", "")
    import re
    excerpt = re.sub(r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$", "", raw_voice, flags=re.I | re.M).strip()[:500]

    user_message = (
        f"user_language: {user_language}\n"
        f"Channel name: {channel.name}\n"
        f"Channel niche: {channel.niche}\n\n"
        f"Discovered content title: {title}\n"
        f"Source URL: {content.source_url}\n\n"
        f"Script opening (first 500 chars):\n{excerpt}"
    )
    return call_claude(_TELEGRAM_SYSTEM_PROMPT, user_message, max_tokens=512)


def generate_native_script(
    video_script: str,
    voice_script: str,
    target_language: str,
    niche: str,
    tone: str,
    script_format: str = "youtube_long",
) -> dict:
    """Adapt source-language scripts for a target language and audience.

    For ``youtube_long``: accurate translation with natural fluency (facts unchanged).
    For short-form formats: full cultural adaptation (examples, metaphors replaced).

    Args:
        video_script:    Source-language structured video script.
        voice_script:    Source-language narrator text (may include section markers).
        target_language: BCP-47 language code for the output (e.g. "fr", "de", "es").
        niche:           Channel niche (used to maintain topical framing).
        tone:            Channel tone (documentary | conversational | educational | …).
        script_format:   Format key from ``channel_config.script_format``.

    Returns:
        Dict with keys ``video_script`` and ``voice_script`` in ``target_language``.
        The ``voice_script`` preserves [SECTION N] markers for timing alignment.

    Raises:
        ValueError: If Claude returns malformed JSON or a key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    prompt = (
        _YOUTUBE_LONG_FORM_NATIVE_SYSTEM_PROMPT
        if script_format == "youtube_long"
        else _SHORT_FORM_NATIVE_SYSTEM_PROMPT
    )
    user_message = (
        f"Target language: {target_language}\n"
        f"Channel niche: {niche}\n"
        f"Channel tone: {tone}\n\n"
        f"Source video script:\n{video_script}\n\n"
        f"Source voice script:\n{voice_script}"
    )
    response = call_claude(prompt, user_message, max_tokens=8192)
    return parse_claude_json(response, required_keys=["video_script", "voice_script"],
                             type_checks={"video_script": str, "voice_script": str})


def generate_revised_scripts(current_scripts: dict, feedback: str, channel) -> dict:
    """Revise existing scripts based on user feedback (called on CHANGE replies).

    Sends the FULL video_script and voice_script to Claude so the revision
    is complete — not a fragment. Claude must return all three fields.

    Args:
        current_scripts: Dict with ``title``, ``video_script``, ``voice_script``.
        feedback:        The raw user feedback text from Telegram.
        channel:         Channel ORM object (provides niche and tone as context).

    Returns:
        Dict with ``title``, ``video_script``, ``voice_script`` — fully revised.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n\n"
        f"Current title: {current_scripts.get('title', '')}\n\n"
        f"Current video script:\n{current_scripts.get('video_script', '')}\n\n"
        f"Current voice script:\n{current_scripts.get('voice_script', '')}\n\n"
        f"User feedback:\n{feedback}"
    )
    response = call_claude(_REVISION_SYSTEM_PROMPT, user_message, max_tokens=8192)
    return parse_claude_json(response, required_keys=["title", "video_script", "voice_script"],
                             type_checks={"title": str, "video_script": str, "voice_script": str})


def assess_script_quality(scripts: dict, channel, script_format: str = "youtube_long") -> dict:
    """Run the Script Quality Gate — a YouTube-retention review distinct from Agent 3.

    Judges hook strength, clarity, emotional pull, narrative arc, specificity,
    generic AI phrasing, and TTS readability using fixed, repeatable criteria.
    Agent 3 checks technical/structural correctness; this checks whether a viewer
    would actually keep watching.

    Args:
        scripts:       Dict with ``title``, ``video_script``, ``voice_script``.
        channel:       Channel ORM object (provides niche and tone as context).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        Dict with ``status`` ("PASSED" | "NEEDS_REWRITE") and ``issues`` —
        a list of ``{"severity", "category", "description", "fix"}`` dicts.

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
    response = call_claude(_SCRIPT_QUALITY_SYSTEM_PROMPT, user_message, max_tokens=1536)
    result = parse_claude_json(response, required_keys=["status", "issues"],
                               type_checks={"status": str, "issues": list})
    if result["status"] not in {"PASSED", "NEEDS_REWRITE"}:
        raise ValueError(f"assess_script_quality: unexpected status {result['status']!r}")
    return result


def rewrite_script_for_quality(scripts: dict, issues: list[dict], channel,
                               script_format: str = "youtube_long") -> dict:
    """Rewrite a full script to fix issues raised by the Script Quality Gate.

    Sends the FULL current scripts plus the issue list (with concrete fixes) and
    requires a complete rewritten title/video_script/voice_script back — never a
    partial script when a full regeneration is expected.

    Args:
        scripts:       Dict with ``title``, ``video_script``, ``voice_script``.
        issues:        Issue list from ``assess_script_quality()``.
        channel:       Channel ORM object (provides niche and tone as context).
        script_format: Format key from ``channel_config.script_format``.

    Returns:
        Dict with ``title``, ``video_script``, ``voice_script`` — fully rewritten.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
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
        f"Current video script:\n{scripts.get('video_script', '')}\n\n"
        f"Current voice script:\n{scripts.get('voice_script', '')}\n\n"
        f"Issues to fix:\n{issue_lines}"
    )
    response = call_claude(_SCRIPT_QUALITY_REWRITE_SYSTEM_PROMPT, user_message, max_tokens=8192)
    return parse_claude_json(response, required_keys=["title", "video_script", "voice_script"],
                             type_checks={"title": str, "video_script": str, "voice_script": str})


def assess_story_quality(
    story: Story,
    channel,
    script_format: str = "youtube_long",
    rejected_candidates: list[dict] | None = None,
) -> dict:
    """Score a candidate story's documentary/visual potential — the Story Scoring Gate.

    Runs BEFORE script generation. Claude judges nine fixed dimensions (narrative
    tension, visual potential, emotional impact, curiosity gap, documentary
    potential, YouTube retention, Shorts potential, stock media availability,
    visual diversity) plus concrete visual elements, the story's central tension,
    a factual hook candidate, and risk notes. Claude does NOT decide accept/reject —
    ``score_story_assessment()`` and ``decide_story_acceptance()`` in
    ``services/scoring.py`` own that (CLAUDE.md: business rules belong in Python).

    Args:
        story:               Candidate ``Story`` returned by the fetcher.
        channel:             Channel ORM object (provides niche and tone as context).
        script_format:       Format key from ``channel_config.script_format``.
        rejected_candidates: Optional list of ``{"title", "url"}`` dicts for stories
                             already rejected this run — passed as calibration context
                             so Claude's standards stay consistent across candidates.

    Returns:
        Dict with ``scores`` (per-dimension ``{"score", "justification"}``),
        ``concrete_visual_elements``, ``central_tension``, ``best_possible_hook``,
        and ``risk_notes``.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    rejected_block = ""
    if rejected_candidates:
        lines = "\n".join(
            f"  - {c.get('title', '')!r} ({c.get('url', '')})"
            for c in rejected_candidates
        )
        rejected_block = (
            "\n\nPreviously rejected candidates this run (calibration context only — "
            f"score the CURRENT story strictly on its own merits):\n{lines}"
        )

    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format}\n\n"
        f"Story title: {story.title}\n"
        f"Story URL: {story.url}\n"
        f"Source metadata: upvotes={story.upvotes}, comments={story.comments}, "
        f"published_at={story.published_at.isoformat()}\n\n"
        f"Story body:\n{story.body}"
        f"{rejected_block}"
    )
    response = call_claude(_STORY_SCORING_SYSTEM_PROMPT, user_message, max_tokens=2048)
    return parse_claude_json(
        response,
        required_keys=[
            "scores", "concrete_visual_elements", "central_tension",
            "best_possible_hook", "risk_notes",
        ],
        type_checks={
            "scores": dict,
            "concrete_visual_elements": list,
            "central_tension": str,
            "best_possible_hook": str,
            "risk_notes": list,
        },
    )
