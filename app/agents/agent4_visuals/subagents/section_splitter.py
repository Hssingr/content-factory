"""Section Splitter — divides a video script into timed visual sections.

Python handles parsing and timing. Claude handles creative decisions (search query, visual type).

Timing strategy (in priority order):
  1. Voice-script markers: if the voice_script contains [INTRO]/[SECTION N]/[OUTRO] markers,
     use per-section word counts from the voice_script for timing ratios. This is the most
     accurate method because the voice_script words drive the audio duration.
  2. Equal splits: fallback when marker count does not match section count. Each section
     receives an equal share of the total duration.

NOTE: _refine_with_whisper is intentionally removed. It used video_script word counts
(visual direction notes, ~40 words/section) as a proxy for voice_script word counts
(~150 words/section), causing all sections to map to the first ~27% of the audio and
producing a black screen for the remaining 73% of the video.
"""

import logging
import re

from app.agents.agent4_visuals.system_prompt import enrich_sections_with_visuals

logger = logging.getLogger(__name__)

_SECTION_MARKER = re.compile(
    r"^\s*\[(INTRO|SECTION\s*\d+[^]]*|OUTRO)\]",
    re.IGNORECASE | re.MULTILINE,
)


def split_into_sections(
    video_script: str,
    voice_script: str,
    duration_ms: int,
    channel_niche: str,
    channel_tone: str,
    whisper_transcript: list[dict] | None = None,
) -> list[dict]:
    """Split a video script into timed visual sections enriched with search queries.

    Steps:
      1. Parse [INTRO] / [SECTION N] / [OUTRO] markers from video_script (Python)
      2. Calculate audio_start_ms / audio_end_ms — uses voice_script section word counts
         when voice_script has matching markers, otherwise equal splits (Python)
      3. Ask Claude to enrich each section with search_query + suggested_visual

    Args:
        video_script:       Structured script with [INTRO], [SECTION N: ...], [OUTRO] markers.
        voice_script:       Narrator text — may include [SECTION N] markers for timing.
        duration_ms:        Exact audio duration from Agent 3 (mutagen).
        channel_niche:      Channel niche passed as context to Claude.
        channel_tone:       Channel tone passed as context to Claude.
        whisper_transcript: Unused — kept for API compatibility.

    Returns:
        List of section dicts:
        {section_order, script_text, audio_start_ms, audio_end_ms,
         duration_sec, search_query, suggested_visual}
    """
    raw_sections = _parse_sections(video_script)
    if not raw_sections:
        logger.warning("No section markers found — treating entire script as one section")
        raw_sections = [{"order": 0, "label": "FULL", "text": video_script.strip()}]

    timed = _assign_timings(raw_sections, voice_script, duration_ms)

    enriched = enrich_sections_with_visuals(timed, channel_niche, channel_tone)

    logger.info(
        "Section Splitter: %d section(s), total %.1fs, method=%s",
        len(enriched),
        duration_ms / 1000,
        "voice_markers" if _has_voice_markers(voice_script) else "equal_splits",
    )
    return enriched


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_sections(video_script: str) -> list[dict]:
    """Extract text blocks between [SECTION] / [INTRO] / [OUTRO] markers."""
    splits = _SECTION_MARKER.split(video_script)
    sections = []
    i = 1
    order = 0
    while i + 1 < len(splits):
        label = splits[i].strip()
        text  = splits[i + 1].strip()
        if text:
            sections.append({"order": order, "label": label, "text": text})
            order += 1
        i += 2
    return sections


def _has_voice_markers(voice_script: str) -> bool:
    """Return True if the voice_script contains any section markers."""
    return bool(_SECTION_MARKER.search(voice_script))


def _split_voice_by_markers(voice_script: str) -> list[str]:
    """Split voice_script into per-section text blocks using its markers.

    Returns one text block per marker (in order). Returns an empty list if no
    markers are found.
    """
    splits = _SECTION_MARKER.split(voice_script)
    if len(splits) < 3:
        return []
    blocks = []
    i = 2   # first text block after the first label
    while i < len(splits):
        text = splits[i].strip()
        if text:
            blocks.append(text)
        i += 2
    return blocks


def _word_count(text: str) -> int:
    return len(text.split())


def _assign_timings(
    sections: list[dict],
    voice_script: str,
    duration_ms: int,
) -> list[dict]:
    """Assign audio_start_ms / audio_end_ms to each section.

    Uses voice_script section word counts when the voice_script contains matching
    markers. Falls back to equal splits otherwise.

    Args:
        sections:     Parsed video_script sections (order, label, text).
        voice_script: Narrator text — may include [SECTION N] markers.
        duration_ms:  Total audio duration in milliseconds.

    Returns:
        List of timed section dicts ready for visual enrichment.
    """
    n = max(len(sections), 1)

    # Try voice_script marker-based timing
    voice_blocks = _split_voice_by_markers(voice_script)
    if len(voice_blocks) == len(sections):
        word_counts = [_word_count(b) for b in voice_blocks]
        logger.debug(
            "Using voice_script marker timing: %d blocks, word counts=%s",
            len(voice_blocks), word_counts,
        )
    else:
        if voice_blocks:
            logger.warning(
                "Voice marker count (%d) != section count (%d) — falling back to equal splits",
                len(voice_blocks), len(sections),
            )
        word_counts = [1] * n

    total_words = max(sum(word_counts), 1)
    result = []
    cumulative_ms = 0

    for i, (s, wc) in enumerate(zip(sections, word_counts)):
        is_last     = (i == n - 1)
        section_ms  = duration_ms - cumulative_ms if is_last else int((wc / total_words) * duration_ms)
        section_ms  = max(section_ms, 0)

        result.append({
            "section_order":   s["order"],
            "script_text":     s["text"],
            "audio_start_ms":  cumulative_ms,
            "audio_end_ms":    cumulative_ms + section_ms,
            "duration_sec":    section_ms / 1000,
        })
        cumulative_ms += section_ms

    return result
