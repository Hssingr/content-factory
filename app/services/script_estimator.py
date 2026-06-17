"""Duration estimation and Shorts breakpoints calculation.

All functions are pure Python — no Claude or external API calls.
Agent 4 will RECALCULATE breakpoints using real audio duration from ElevenLabs;
these estimates exist so Agent 2 can flag structurally thin scripts before audio is generated.
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

# A Short must be ≤ 60 s; we target ≤ 58 s to leave a safe margin
_MAX_SHORT_SEC = 58.0

# Minimum video length threshold
MIN_VIDEO_DURATION_SEC = 300.0   # 5 minutes


def estimate_duration_sec(voice_script: str, language: str) -> float:
    """Estimate narration duration from a voice script's word count.

    Uses language-specific average speech rates. The result feeds:
    - The Shorts breakpoints calculator below
    - ``scripts.estimated_duration_sec`` stored in the DB

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


def compute_shorts_breakpoints(
    voice_script: str,
    duration_sec: float,
    shorts_rule: str,
) -> list[int]:
    """Estimate millisecond offsets where each Short ends.

    Splits the voice_script at double-newline paragraph boundaries, accumulating
    word counts to map each paragraph to a millisecond position in the audio.
    A new breakpoint is added whenever the current Short segment would exceed
    58 seconds if extended by the next paragraph.

    Args:
        voice_script: The narrator text — paragraph breaks are used as cut candidates.
        duration_sec: Total estimated duration from ``estimate_duration_sec()``.
        shorts_rule:  Channel config value: ``"always" | "auto" | "never"``.

    Returns:
        List of millisecond offsets (from audio start) where each Short ends.
        Empty list when ``shorts_rule = "never"`` or the whole video fits in one Short.
    """
    if shorts_rule == "never":
        return []

    if duration_sec <= _MAX_SHORT_SEC:
        return []   # entire video fits in a single Short — no splits needed

    duration_ms = int(duration_sec * 1000)
    max_ms = int(_MAX_SHORT_SEC * 1000)

    paragraphs = [p.strip() for p in voice_script.split("\n\n") if p.strip()]

    if len(paragraphs) <= 1:
        # No paragraph structure — fall back to equal-interval splits
        n = max(2, int(duration_sec / _MAX_SHORT_SEC) + 1)
        step = duration_ms // n
        return [step * i for i in range(1, n)]

    # Map each paragraph to its ending millisecond position
    word_counts = [len(p.split()) for p in paragraphs]
    total_words = sum(word_counts)
    if total_words == 0:
        return []

    para_end_ms: list[int] = []
    cumulative = 0
    for w in word_counts:
        cumulative += w
        para_end_ms.append(int((cumulative / total_words) * duration_ms))

    # Build breakpoints: cut just before a paragraph that would push the segment
    # past the 58-second limit
    breakpoints: list[int] = []
    segment_start_ms = 0
    prev_end_ms = 0

    for end_ms in para_end_ms:
        if end_ms - segment_start_ms > max_ms and prev_end_ms > segment_start_ms:
            # Cut at previous paragraph boundary
            breakpoints.append(prev_end_ms)
            segment_start_ms = prev_end_ms
        prev_end_ms = end_ms

    return breakpoints
