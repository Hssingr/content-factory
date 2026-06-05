"""Section Splitter — divides a video script into timed visual sections.

Python is responsible for parsing the script structure and calculating timing.
Claude is responsible for creative decisions (search query, visual type).

Design rationale:
- Timing is deterministic: word-count ratio × total audio duration.
  If Whisper transcript is available, refine start/end to actual word boundaries.
- Claude only sees the section text and channel context — no timing maths.
  This keeps Claude's prompt stable and cacheable.
"""

import logging
import re

from app.agents.agent5_video.system_prompt import enrich_sections_with_visuals

logger = logging.getLogger(__name__)

# Regex that matches [INTRO], [SECTION N: Title], [OUTRO] markers (case-insensitive)
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
      2. Calculate audio_start_ms / audio_end_ms from word-count ratios (Python)
      3. Optionally refine start/end timestamps using Whisper word data (Python)
      4. Ask Claude to enrich each section with search_query + suggested_visual

    Args:
        video_script:       Structured script with [INTRO], [SECTION N: ...], [OUTRO] markers.
        voice_script:       Continuous narrator text (word counts drive timing).
        duration_ms:        Exact audio duration from Agent 4 (mutagen).
        channel_niche:      Channel niche passed as context to Claude.
        channel_tone:       Channel tone passed as context to Claude.
        whisper_transcript: Optional word-level timestamps from Agent 4 Whisper.
                            Format: [{"word": str, "start": float, "end": float}]

    Returns:
        List of section dicts, one per parsed marker:
        {section_order, script_text, audio_start_ms, audio_end_ms,
         duration_sec, search_query, suggested_visual}
    """
    # ── 1. Parse markers ──────────────────────────────────────────────────────
    raw_sections = _parse_sections(video_script)
    if not raw_sections:
        logger.warning("No section markers found — treating entire script as one section")
        raw_sections = [{"order": 0, "label": "FULL", "text": video_script.strip()}]

    # ── 2. Calculate timing from voice_script word counts ─────────────────────
    timed = _assign_timings(raw_sections, voice_script, duration_ms)

    # ── 3. Optional: refine with Whisper timestamps ───────────────────────────
    if whisper_transcript:
        timed = _refine_with_whisper(timed, whisper_transcript, voice_script)

    # ── 4. Claude enrichment: search_query + suggested_visual ─────────────────
    enriched = enrich_sections_with_visuals(timed, channel_niche, channel_tone)

    logger.info(
        "Section Splitter: %d section(s), total duration %.1fs",
        len(enriched), duration_ms / 1000,
    )
    return enriched


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_sections(video_script: str) -> list[dict]:
    """Extract text blocks between [SECTION] / [INTRO] / [OUTRO] markers."""
    splits = _SECTION_MARKER.split(video_script)
    # split() with a capturing group interleaves: [before, label, text, label, text, ...]
    # Index 0 is text before the first marker (usually empty), then pairs of (label, text)
    sections = []
    i = 1   # skip index 0 (pre-marker content)
    order = 0
    while i + 1 < len(splits):
        label = splits[i].strip()
        text  = splits[i + 1].strip()
        if text:
            sections.append({"order": order, "label": label, "text": text})
            order += 1
        i += 2
    return sections


def _word_count(text: str) -> int:
    return len(text.split())


def _assign_timings(
    sections: list[dict],
    voice_script: str,
    duration_ms: int,
) -> list[dict]:
    """Assign audio_start_ms / audio_end_ms to each section via word-count ratio."""
    total_voice_words = _word_count(voice_script)
    if total_voice_words == 0:
        # Fallback: equal splits
        per_section = duration_ms // max(len(sections), 1)
        for i, s in enumerate(sections):
            s["audio_start_ms"] = i * per_section
            s["audio_end_ms"]   = (i + 1) * per_section
            s["duration_sec"]   = per_section / 1000
        return sections

    # Approximate how many voice-script words each video section covers.
    # We use the section's own text word count as a proxy.
    section_word_counts = [_word_count(s["text"]) for s in sections]
    total_section_words = max(sum(section_word_counts), 1)

    cumulative_ms = 0
    for s, wc in zip(sections, section_word_counts):
        section_ms   = int((wc / total_section_words) * duration_ms)
        s["audio_start_ms"] = cumulative_ms
        s["audio_end_ms"]   = cumulative_ms + section_ms
        s["duration_sec"]   = section_ms / 1000
        cumulative_ms += section_ms

    # Snap the last section's end to the exact total duration
    if sections:
        sections[-1]["audio_end_ms"] = duration_ms
        sections[-1]["duration_sec"] = (duration_ms - sections[-1]["audio_start_ms"]) / 1000

    # Rename for return format
    result = []
    for s in sections:
        result.append({
            "section_order": s["order"],
            "script_text":   s["text"],
            "audio_start_ms": s["audio_start_ms"],
            "audio_end_ms":   s["audio_end_ms"],
            "duration_sec":   s["duration_sec"],
        })
    return result


def _refine_with_whisper(
    sections: list[dict],
    whisper_transcript: list[dict],
    voice_script: str,
) -> list[dict]:
    """Refine section timings using Whisper word-level timestamps.

    Finds the Whisper word whose position (by word-count index) corresponds
    to the start of each section and uses its actual ``start`` timestamp.
    Falls back silently to the word-count estimate if matching fails.
    """
    if not whisper_transcript:
        return sections

    total_voice_words = len(voice_script.split())
    if total_voice_words == 0:
        return sections

    cumulative_words = 0
    for s in sections:
        section_wc      = _word_count(s["script_text"])
        start_word_idx  = int((cumulative_words / total_voice_words) * len(whisper_transcript))
        end_word_idx    = int(((cumulative_words + section_wc) / total_voice_words) * len(whisper_transcript))

        start_word_idx = max(0, min(start_word_idx, len(whisper_transcript) - 1))
        end_word_idx   = max(start_word_idx + 1, min(end_word_idx, len(whisper_transcript)))

        try:
            s["audio_start_ms"] = int(whisper_transcript[start_word_idx]["start"] * 1000)
            s["audio_end_ms"]   = int(whisper_transcript[end_word_idx - 1]["end"] * 1000)
            s["duration_sec"]   = (s["audio_end_ms"] - s["audio_start_ms"]) / 1000
        except (IndexError, KeyError, TypeError):
            pass   # keep word-count estimate

        cumulative_words += section_wc

    return sections
