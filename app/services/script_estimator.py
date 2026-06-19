"""Duration estimation helpers.

Pure Python helpers used before real audio exists. Agent 3 later records the
measured duration from generated audio; parent-short breakpoint estimation is
not part of the V2 standalone child-short architecture.
"""

# Average narration speed in words per minute per language (conservative estimates)
SPEECH_RATES: dict[str, float] = {
    "en": 150.0,
    "fr": 140.0,
    "de": 130.0,
    "es": 160.0,
    "it": 150.0,
    "pt": 145.0,
}
_DEFAULT_RATE = 140.0   # used when language is unknown



def estimate_duration_sec(voice_script: str, language: str) -> float:
    """Estimate narration duration from a voice script's word count.

    Uses language-specific average speech rates. The result feeds
    ``scripts.estimated_duration_sec`` before real audio exists.

    Args:
        voice_script: The narrator text (no stage directions).
        language:     BCP-47 language code (e.g. "fr", "en").

    Returns:
        Estimated duration in seconds, rounded to 1 decimal place.
    """
    rate = SPEECH_RATES.get(language, _DEFAULT_RATE)
    word_count = len(voice_script.split())
    result = round((word_count / rate) * 60.0, 1)
    return result
