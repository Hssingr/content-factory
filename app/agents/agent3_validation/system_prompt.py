import json
import logging
import re

from app.services.claude_client import call_claude

logger = logging.getLogger(__name__)

# ── System prompts (>800 chars → call_claude applies cache_control: ephemeral) ──

_VALIDATION_SYSTEM_PROMPT = """\
You are a script quality validator for an automated multilingual video content system.

Your task: analyse ALL provided scripts for ALL languages simultaneously and identify issues.

== Validation checks ==

LENGTH COHERENCE (MAJOR if any language deviates > 30% from the median word count)
  — Compare voice_script word counts across languages.
  — A 30%+ deviation means one language is abnormally short or padded.

MINIMUM LENGTH (MAJOR if voice_script would produce < 5 minutes of narration)
  — Average narration speed is ~140 words/minute.
  — A voice_script with fewer than 700 words is too short for a 5-minute video.
  — Flag this as MAJOR regardless of other issues.

TONE (MINOR unless severely off)
  — Script tone must match the channel's declared tone (documentary, conversational, etc.).

COMPLETENESS (MAJOR if missing)
  — video_script must have [INTRO] and [OUTRO] markers and at least one [SECTION N:] block.
  — voice_script must have a hook in the first 3 sentences and a clear closing.
  — A script that ends abruptly mid-sentence is MAJOR.

SHORTS BREAKPOINTS (MINOR)
  — video_script should have enough section markers to support clean Shorts cuts.
  — If the entire content has only one section, flag as MINOR.

CONTENT POLICY (MAJOR)
  — No incitement, graphic violence, hate speech, or medical/legal/financial advice.
  — No naming suspects in open legal cases.

LINGUISTIC NATURALNESS (MINOR)
  — Scripts must read as natively written, not as translated content.
  — Unnatural idioms, literal translations, or awkward phrasing are MINOR.

== Output ==

Respond with ONLY valid JSON — no preamble, no code fence:
{
  "overall_status": "PASSED | MINOR_ISSUES | MAJOR_ISSUES",
  "issues": [
    {
      "language": "fr",
      "severity": "MAJOR | MINOR",
      "category": "length_coherence | minimum_length | tone | completeness | shorts_breakpoints | content_policy | linguistic_naturalness",
      "description": "Concise description of the problem",
      "suggestion": "Specific fix recommendation"
    }
  ]
}

If no issues are found, return {"overall_status": "PASSED", "issues": []}.\
"""

_CORRECTION_SYSTEM_PROMPT = """\
You are a script editor for an automated multilingual video content system.

Your task: correct a specific language's video and voice scripts based on a list of
identified issues. Apply ONLY the changes needed to fix the listed issues — do not
rewrite sections that are not affected.

Rules:
1. Preserve all [SECTION N:] markers, [INTRO], and [OUTRO] structure in video_script.
2. Keep the voice_script in the same language as the original.
3. Do not change the story, key facts, or overall narrative.
4. If minimum_length is flagged, expand existing sections with more depth, examples, or
   context — never pad with filler. The voice_script must reach at least 700 words.
5. When fixing linguistic_naturalness, rewrite the affected sentences entirely rather
   than patching individual words — half-fixed awkward phrasing is worse than original.
6. Respond with ONLY valid JSON — no preamble, no code fence:
   {"video_script": "...", "voice_script": "..."}\
"""


# ── Public functions ──────────────────────────────────────────────────────────

def validate_scripts(scripts_by_language: dict[str, dict], channel) -> dict:
    """Validate all language scripts for a piece of content simultaneously.

    Sends all scripts to Claude in a single call for cross-language comparison
    (length coherence and minimum_length require seeing all languages at once).

    Args:
        scripts_by_language: Dict mapping language code → {video_script, voice_script}.
        channel:             Channel ORM object (provides niche and tone as context).

    Returns:
        Dict with keys ``overall_status`` (PASSED | MINOR_ISSUES | MAJOR_ISSUES)
        and ``issues`` (list of issue dicts).

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    sections = []
    for lang, scripts in scripts_by_language.items():
        word_count = len(scripts.get("voice_script", "").split())
        sections.append(
            f"--- Language: {lang} ({word_count} words in voice script) ---\n"
            f"VIDEO SCRIPT:\n{scripts.get('video_script', '')}\n\n"
            f"VOICE SCRIPT:\n{scripts.get('voice_script', '')}"
        )

    user_message = (
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n\n"
        + "\n\n".join(sections)
    )

    raw = call_claude(_VALIDATION_SYSTEM_PROMPT, user_message, max_tokens=1024)
    return _parse_json(raw, required_keys=["overall_status", "issues"])


def auto_correct_script(
    current_scripts: dict,
    issues: list[dict],
    language: str,
    channel,
) -> dict:
    """Correct a single language's scripts based on identified issues.

    Called for each language that has MAJOR issues, up to 3 times per language
    until validation passes or the attempt limit is reached.

    Args:
        current_scripts: Dict with ``video_script`` and ``voice_script`` for the language.
        issues:          List of issue dicts from ``validate_scripts()`` for this language.
        language:        BCP-47 language code (e.g. "fr", "en").
        channel:         Channel ORM object (provides niche and tone).

    Returns:
        Dict with corrected ``video_script`` and ``voice_script``.

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    issue_lines = "\n".join(
        f"- [{i['severity']}] {i['category']}: {i['description']} → {i['suggestion']}"
        for i in issues
    )

    user_message = (
        f"Language: {language}\n"
        f"Channel niche: {channel.niche}\n"
        f"Channel tone: {channel.tone}\n\n"
        f"Issues to fix:\n{issue_lines}\n\n"
        f"Current video script:\n{current_scripts.get('video_script', '')}\n\n"
        f"Current voice script:\n{current_scripts.get('voice_script', '')}"
    )

    raw = call_claude(_CORRECTION_SYSTEM_PROMPT, user_message, max_tokens=4096)
    return _parse_json(raw, required_keys=["video_script", "voice_script"])


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_json(text: str, required_keys: list[str]) -> dict:
    """Parse a JSON response from Claude, stripping any accidental code fences."""
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", text).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Agent 3 JSON parse error: %s | Raw (first 300): %.300s", exc, text)
        raise ValueError(f"Claude returned invalid JSON: {exc}") from exc

    missing = [k for k in required_keys if k not in data]
    if missing:
        logger.error("Missing keys %s in response: %.300s", missing, text)
        raise ValueError(f"Claude response missing required keys: {missing}")

    return data
