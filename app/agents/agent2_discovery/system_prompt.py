import logging

from app.services.claude_client import call_claude, parse_claude_json
from app.agents.agent2_discovery.services.story import Story

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.4"  # bump when any prompt below changes behaviour

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
You are a YouTube documentary scriptwriter. \
Your output will be narrated by a TTS voice for a YouTube channel.

Write a narration script for a 6–8 minute video (900–1200 words in voice_script).

Script structure:
  [INTRO]         Hook: state the single most striking fact from the story. 15–20 seconds.
                  Do not explain everything. Open a question the viewer needs answered.
  [SECTION 1]     Setup: who is involved, why this matters, what is at stake.
  [SECTION 2–N]   Development: tell the story chronologically or logically.
                  Each section covers one clear idea or turning point.
                  Include one section presenting the central contradiction or mystery.
  [OUTRO]         Resolution + one unanswered question + call to action (subscribe / comment).

Tone: factual, measured, authoritative. Not sensationalist. Not TikTok-style.

VOICE SCRIPT style rules:
- 900–1200 words in voice_script (≈ 6–8 minutes at 150 wpm).
- Short sentences (max 20 words) but allow varied rhythm — not all staccato.
- Curiosity gaps only at major transitions (INTRO→body, body→OUTRO) — NOT every paragraph.
- The first sentence must state the most striking fact from the story directly.
  Forbidden openers: "Today", "In this video", "Have you ever wondered",
  "Welcome", "Let me tell you about", "This is the story of".
- Never exaggerate or invent details not in the source material.
- No filler: "As we know", "It's important to note", "In conclusion".

Voice script ElevenLabs formatting:
- "..." after a key reveal (natural TTS pause).
- "—" before a surprising turn (sharp breath cut).
- One blank line between narrative beats (breathing room for the voice).
- No parentheses, asterisks, emojis, or stage directions. Brackets are allowed ONLY
  for required section markers: [INTRO], [SECTION N], [OUTRO].

VOICE SCRIPT — section markers required:
  Include [INTRO], [SECTION N], [OUTRO] labels on their own line in voice_script.
  They will be stripped before audio generation but are required for visual timing.

  Example format:
    [INTRO]
    A staircase was found in the middle of a dense forest. No building. No road. Just stairs.
    [SECTION 1]
    The discovery was made by hikers on a Tuesday morning in October 2019...
    [OUTRO]
    The investigation officially closed in 2021. But one question was never answered.

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
