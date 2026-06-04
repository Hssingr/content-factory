import json
import logging
import re

from app.services.claude_client import call_claude
from app.agents.agent2_discovery.services.story import Story

logger = logging.getLogger(__name__)

# ── System prompts (>800 chars → call_claude() applies cache_control: ephemeral) ──

_SCRIPTS_SYSTEM_PROMPT = """\
You are an expert content creator specialised in educational and investigative video scripts \
for YouTube, TikTok, Instagram, and Facebook.

You produce scripts that are:
- Attention-grabbing from the very first sentence (strong hook in the opening 3 sentences)
- Well-structured with clear sections and natural transitions between them
- Calibrated to the channel tone (documentary, conversational, educational, investigative…)
- Written in authentic, native-sounding language for the source audience — never translated
- Factually grounded in the provided source material, without inventing details

Script components:
VIDEO SCRIPT — narrative structure divided into labelled sections:
  [INTRO] hook + context setup
  [SECTION N: Descriptive Title] key points, evidence, narrative beats
  [OUTRO] conclusion + call to action

VOICE SCRIPT — the exact words spoken by the TTS narrator:
  No stage directions, no brackets, no section labels.
  Continuous natural spoken narration. Every sentence will be read aloud.
  Aim for 800–1 500 words (≈ 6–12 minutes of narration).

Output: valid JSON only — no preamble, no code fence, no explanation.
{
  "title": "Compelling video title, 60–80 characters",
  "video_script": "Full structured script with [INTRO], [SECTION N], [OUTRO] markers",
  "voice_script": "Full narrator text in natural spoken language"
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. Write title, video_script, and voice_script in the SAME language as the source content.
3. The hook (first 3 sentences of voice_script) must immediately capture attention.
4. Never fabricate facts — use only what the source material provides.\
"""

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

_NATIVE_SCRIPTS_SYSTEM_PROMPT = """\
You are an expert multilingual content adapter specialised in cultural localisation for \
video platforms (YouTube, TikTok, Instagram, Facebook).

Your task is to produce a culturally adapted version of a video script for a new target \
language and audience. This is NOT translation — it is full cultural adaptation.

Cultural adaptation means:
- Replace culture-specific examples, figures, and metaphors with equivalents that resonate \
  with the target culture
- Use idioms, expressions, and references that feel native to the target language audience
- Adjust historical or geographic context where the target audience needs different anchoring
- Maintain the same story structure, key facts, emotional arc, and narrative beats
- Write as if the content was created originally in the target language — not adapted from another
- Match the original tone exactly (documentary, conversational, educational, investigative…)

Output: valid JSON only — no preamble, no code fence, no explanation.
{
  "video_script": "Culturally adapted script with [INTRO], [SECTION N], [OUTRO] markers",
  "voice_script": "Culturally adapted narrator text, natural spoken language for TTS"
}

Strict rules:
1. JSON only — the response will be parsed programmatically.
2. Facts and core story must remain accurate — only cultural framing changes.
3. Keep similar length to the source scripts.\
"""


# ── Public functions ─────────────────────────────────────────────────────────

def generate_scripts(story: Story, channel) -> dict:
    """Generate title + video_script + voice_script in the story's source language.

    Uses the story body and channel niche/tone to produce a complete video script pair
    ready to be saved as a ``Script`` record (source language, version=1).

    Args:
        story:   The discovered story (url, title, body, language).
        channel: The Channel ORM object (provides niche, tone, name).

    Returns:
        Dict with keys ``title``, ``video_script``, ``voice_script``.

    Raises:
        ValueError: If Claude returns malformed JSON or a key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n"
        f"Source language: {story.language}\n\n"
        f"Story title: {story.title}\n"
        f"Source URL: {story.url}\n\n"
        f"Story content:\n{story.body[:4000]}"   # cap to avoid excessive context
    )
    response = call_claude(_SCRIPTS_SYSTEM_PROMPT, user_message, max_tokens=4096)
    return _parse_json(response, required_keys=["title", "video_script", "voice_script"])


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
    title = scripts.get("title", content.title)
    excerpt = scripts.get("voice_script", "")[:500]

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
) -> dict:
    """Culturally adapt source-language scripts for a target language and audience.

    This is NOT translation — it is a full cultural localisation. The output reads
    as if the content was originally produced in ``target_language``.

    Args:
        video_script:    Source-language structured video script.
        voice_script:    Source-language narrator text.
        target_language: BCP-47 language code for the output (e.g. "fr", "de", "es").
        niche:           Channel niche (used to maintain topical framing).
        tone:            Channel tone (documentary | conversational | educational | …).

    Returns:
        Dict with keys ``video_script`` and ``voice_script`` in ``target_language``.

    Raises:
        ValueError: If Claude returns malformed JSON or a key is missing.
        anthropic.APIError: On non-retryable Claude API errors.
    """
    user_message = (
        f"Target language: {target_language}\n"
        f"Channel niche: {niche}\n"
        f"Channel tone: {tone}\n\n"
        f"Source video script:\n{video_script}\n\n"
        f"Source voice script:\n{voice_script}"
    )
    response = call_claude(_NATIVE_SCRIPTS_SYSTEM_PROMPT, user_message, max_tokens=4096)
    return _parse_json(response, required_keys=["video_script", "voice_script"])


_REVISION_SYSTEM_PROMPT = """\
You revise existing video scripts based on user feedback.

Rules:
1. Preserve the source language, tone, and factual content unless the feedback explicitly asks to change them.
2. Apply changes accurately and minimally — do not rewrite what the feedback does not address.
3. Output valid JSON only — no preamble, no code fence.
{"title": "...", "video_script": "...", "voice_script": "..."}\
"""


def generate_revised_scripts(current_scripts: dict, feedback: str, channel) -> dict:
    """Revise existing scripts based on user feedback (called on CHANGE replies).

    Args:
        current_scripts: Dict with ``title``, ``video_script``, ``voice_script``.
        feedback:        The raw user feedback text from Telegram.
        channel:         Channel ORM object (provides niche and tone as context).

    Returns:
        Dict with ``title``, ``video_script``, ``voice_script`` — revised version.

    Raises:
        ValueError: If Claude returns malformed JSON or a required key is missing.
    """
    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n\n"
        f"Current title: {current_scripts.get('title', '')}\n\n"
        f"Current voice script (first 2 000 chars):\n"
        f"{current_scripts.get('voice_script', '')[:2000]}\n\n"
        f"User feedback:\n{feedback}"
    )
    response = call_claude(_REVISION_SYSTEM_PROMPT, user_message, max_tokens=4096)
    return _parse_json(response, required_keys=["title", "video_script", "voice_script"])


# ── Internal helpers ─────────────────────────────────────────────────────────

def _parse_json(text: str, required_keys: list[str]) -> dict:
    """Parse a JSON response from Claude, stripping any accidental code fences.

    Raises:
        ValueError: If JSON is malformed or a required key is absent.
    """
    # Remove ```json ... ``` or ``` ... ``` wrappers Claude sometimes adds
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", text).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("JSON parse error: %s | Raw (first 300): %.300s", exc, text)
        raise ValueError(f"Claude returned invalid JSON: {exc}") from exc

    missing = [k for k in required_keys if k not in data]
    if missing:
        logger.error("Missing keys %s in Claude response: %.300s", missing, text)
        raise ValueError(f"Claude response missing required keys: {missing}")

    return data
