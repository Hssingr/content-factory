"""Shorts Cutter — groups validated sections into Short-form video segments.

Uses the ``shorts_breakpoints`` list from Agent 4 (audio_files table) as cut points.
Each Short is ≤ 58 seconds of audio. The cutter:

  1. Maps each breakpoint interval to the sections it covers.
  2. Assigns a language-specific part label (e.g. "Partie 1/8", "Part 1 of 8").
  3. Flags the first section of each Short as hook_modified=True so Remotion can
     apply the attention-grabbing treatment to the first 3 seconds.

No Claude call needed here — this is pure deterministic logic.
"""

import logging

logger = logging.getLogger(__name__)

# Part-label templates per language (keys match channel_languages.language)
_PART_LABELS: dict[str, str] = {
    "fr": "Partie {n}/{total}",
    "en": "Part {n} of {total}",
    "es": "Parte {n}/{total}",
    "it": "Parte {n}/{total}",
    "de": "Teil {n}/{total}",
    "pt": "Parte {n}/{total}",
    "ar": "الجزء {n} من {total}",
    "ja": "パート {n}/{total}",
    "ko": "파트 {n}/{total}",
    "zh": "第 {n} 部分，共 {total} 部分",
}
_DEFAULT_LABEL = "Part {n} of {total}"


def cut_shorts(
    sections: list[dict],
    shorts_breakpoints: list[int],
    language: str,
    label_style: str = "standard",
) -> list[dict]:
    """Group validated sections into Shorts segments using breakpoint boundaries.

    Each element of ``shorts_breakpoints`` is the end timestamp (ms) of a Short.
    Section ``[audio_start_ms, audio_end_ms)`` is assigned to the first Short whose
    end boundary is greater than the section's midpoint.

    Args:
        sections:           Validated, media-enriched sections (sorted by section_order).
        shorts_breakpoints: End timestamps (ms) from audio_files.shorts_breakpoints.
                            Example: [57000, 115000, 173000]
        language:           Channel language code (e.g. "fr", "en") for part labels.
        label_style:        Reserved for future custom label styles (unused currently).

    Returns:
        List of Short dicts, one per breakpoint interval:
        {
          "short_index":    int,             # 0-based
          "part_label":     str,             # "Partie 1/8"
          "total_parts":    int,
          "sections":       list[dict],      # section dicts that belong to this Short
          "start_ms":       int,             # audio start of the first section
          "end_ms":         int,             # audio end of the last section
          "duration_sec":   float,
          "hook_modified":  True,            # always True — Remotion handles the hook
        }
        Empty list if shorts_breakpoints is empty or no sections provided.
    """
    if not sections or not shorts_breakpoints:
        logger.info("No shorts breakpoints — skipping Shorts Cutter")
        return []

    sorted_sections    = sorted(sections, key=lambda s: s["section_order"])
    sorted_breakpoints = sorted(shorts_breakpoints)
    total_parts        = len(sorted_breakpoints)

    # Build intervals: [start_ms, end_ms)
    # Interval 0: [0, breakpoints[0])
    # Interval i: [breakpoints[i-1], breakpoints[i])
    intervals: list[tuple[int, int]] = []
    prev = 0
    for bp in sorted_breakpoints:
        intervals.append((prev, bp))
        prev = bp

    # Assign each section to an interval based on its midpoint
    short_buckets: dict[int, list[dict]] = {i: [] for i in range(total_parts)}

    for s in sorted_sections:
        s_mid = (s.get("audio_start_ms", 0) + s.get("audio_end_ms", 0)) // 2
        assigned = False
        for idx, (iv_start, iv_end) in enumerate(intervals):
            if s_mid < iv_end:
                short_buckets[idx].append(s)
                assigned = True
                break
        if not assigned:
            # Section midpoint is beyond all breakpoints — attach to last Short
            short_buckets[total_parts - 1].append(s)

    label_template = _PART_LABELS.get(language.lower(), _DEFAULT_LABEL)
    shorts: list[dict] = []

    for idx in range(total_parts):
        bucket = short_buckets[idx]
        if not bucket:
            logger.debug("Short %d/%d: no sections — skipping", idx + 1, total_parts)
            continue

        start_ms    = bucket[0].get("audio_start_ms", 0)
        end_ms      = bucket[-1].get("audio_end_ms", 0)
        duration_s  = (end_ms - start_ms) / 1000

        part_label = label_template.format(n=idx + 1, total=total_parts)

        short: dict = {
            "short_index":  idx,
            "part_label":   part_label,
            "total_parts":  total_parts,
            "sections":     bucket,
            "start_ms":     start_ms,
            "end_ms":       end_ms,
            "duration_sec": duration_s,
            "hook_modified": True,
        }
        shorts.append(short)

    logger.info(
        "Shorts Cutter: %d short(s) from %d section(s) | language=%s",
        len(shorts), len(sorted_sections), language,
    )
    return shorts
