import logging

from app.services.claude_client import call_claude, parse_claude_json

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.3"  # bump when any prompt below changes behaviour

# ── System prompts (>800 chars → call_claude applies cache_control: ephemeral) ──

_VALIDATION_SYSTEM_PROMPT = """\
You are a script quality validator for an automated multilingual social video content system.

Your task: analyse ALL provided scripts for ALL languages simultaneously and identify issues.

== Validation checks ==

LENGTH COHERENCE (MAJOR if any language deviates > 30% from the median word count)
  — Compare voice_script word counts across languages.
  — A 30%+ deviation means one language is abnormally short or padded.

MINIMUM LENGTH (MAJOR if voice_script word count is below the format's minimum)
  — The user message states the script format and its minimum expected word count
    (e.g. "Script format: youtube_long (minimum expected voice_script length: 900 words)").
  — A voice_script below that stated minimum is too short for its target format and duration.
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

Return ONLY valid JSON. No markdown. No code fence. No extra keys.
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

If no issues are found, return {"overall_status": "PASSED", "issues": []}.
Never invent issues that are not actually present in the scripts provided.\
"""

_CORRECTION_SYSTEM_PROMPT = """\
You are a script editor for an automated multilingual video content system.

Your task: correct a specific language's video and voice scripts based on a list of
identified issues. Apply ONLY the changes needed to fix the listed issues — do not
rewrite sections that are not affected.

Rules:
1. Preserve all [SECTION N:] markers, [INTRO], and [OUTRO] structure in video_script.
2. Keep the voice_script in the same language as the original.
3. Do not change the story, key facts, or overall narrative. Never invent new facts.
4. If minimum_length is flagged, expand existing sections with more depth, examples, or
   context — never pad with filler. The voice_script must reach at least the minimum
   word count stated in the user message for this format.
   Match the style of the declared script format (documentary pacing for youtube_long,
   short punchy sentences and direct viewer address for short-form).
5. When fixing linguistic_naturalness, rewrite the affected sentences entirely rather
   than patching individual words — half-fixed awkward phrasing is worse than original.
6. Return ONLY valid JSON. No markdown. No code fence. No extra keys.
   {"video_script": "...", "voice_script": "..."}\
"""


# ── Public functions ──────────────────────────────────────────────────────────

def validate_scripts(scripts_by_language: dict[str, dict], channel, script_format: str = "youtube_long") -> dict:
    """Validate all language scripts for a piece of content simultaneously.

    Sends all scripts to Claude in a single call for cross-language comparison
    (length coherence and minimum_length require seeing all languages at once).

    The minimum word count for the MINIMUM LENGTH check depends on ``script_format``:
      - "youtube_long" → 900 words (6–8 min documentary)
      - "youtube_short" / "tiktok" / "reels" → 420 words (3–5 min short-form)

    Args:
        scripts_by_language: Dict mapping language code → {video_script, voice_script}.
        channel:             Channel ORM object (provides niche and tone as context).
        script_format:       Format key from ``channel_config.script_format``.

    Returns:
        Dict with keys ``overall_status`` (PASSED | MINOR_ISSUES | MAJOR_ISSUES)
        and ``issues`` (list of issue dicts).

    Raises:
        ValueError: If Claude returns malformed JSON.
    """
    min_words = 900 if script_format == "youtube_long" else 420

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
        f"Channel tone: {channel.tone}\n"
        f"Script format: {script_format} (minimum expected voice_script length: {min_words} words)\n\n"
        + "\n\n".join(sections)
    )

    # max_tokens=2048 — validation with many languages + many issues can exceed 1024
    raw = call_claude(_VALIDATION_SYSTEM_PROMPT, user_message, max_tokens=2048)
    return parse_claude_json(raw, required_keys=["overall_status", "issues"],
                             type_checks={"overall_status": str, "issues": list})


def auto_correct_script(
    current_scripts: dict,
    issues: list[dict],
    language: str,
    channel,
    script_format: str = "youtube_long",
) -> dict:
    """Correct a single language's scripts based on identified issues.

    Called for each language that has MAJOR issues, up to 3 times per language
    until validation passes or the attempt limit is reached.

    Args:
        current_scripts: Dict with ``video_script`` and ``voice_script`` for the language.
        issues:          List of issue dicts from ``validate_scripts()`` for this language.
        language:        BCP-47 language code (e.g. "fr", "en").
        channel:         Channel ORM object (provides niche and tone).
        script_format:   Format key from ``channel_config.script_format`` — determines
                         the minimum word count referenced when fixing minimum_length issues.

    Returns:
        Dict with corrected ``video_script`` and ``voice_script``.

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
        f"Current video script:\n{current_scripts.get('video_script', '')}\n\n"
        f"Current voice script:\n{current_scripts.get('voice_script', '')}"
    )

    raw = call_claude(_CORRECTION_SYSTEM_PROMPT, user_message, max_tokens=4096)
    return parse_claude_json(raw, required_keys=["video_script", "voice_script"],
                             type_checks={"video_script": str, "voice_script": str})
