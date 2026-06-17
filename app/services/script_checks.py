"""Pure-Python deterministic script checks for Agent 3 validation pipeline.

All check functions return a list of Issue dicts in the same schema used by
Agent 3's Claude validation output, so they can be merged and processed together:

    {
        "language":      str,
        "severity":      "MAJOR" | "MINOR",
        "category":      str,
        "description":   str,
        "suggestion":    str,
        "offending_text": str | None,  # optional
    }

These checks run first (no I/O, no Claude calls). Claude validation is then scoped
only to subjective checks (tone, linguistic_naturalness, content_policy) and only
for languages that have no deterministic MAJOR issue.
"""

import logging
import re
import statistics

logger = logging.getLogger(__name__)

# ── Compiled patterns ─────────────────────────────────────────────────────────

_MARKER_LINE_RE = re.compile(
    r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$", re.I | re.M
)
_SECTION_MARKER_RE = re.compile(
    r"^\s*(\[SECTION\s+\d+[^\]]*\])\s*$", re.I | re.M
)
_SECTION_NUM_RE = re.compile(r"\[SECTION\s+(\d+)[^\]]*\]", re.I)
_INTRO_LINE_RE = re.compile(r"^\s*\[INTRO\]\s*$", re.I | re.M)
_OUTRO_LINE_RE = re.compile(r"^\s*\[OUTRO\]\s*$", re.I | re.M)
_ANY_SECTION_LINE_RE = re.compile(
    r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$", re.I | re.M
)

_FORBIDDEN_OPENERS: tuple[str, ...] = (
    "In this video", "In this story",  # check longer forms first
    "In ",     "Today,", "Today ",
    "Have you ", "Have you,",
    "Welcome,", "Welcome ",
    "What if",
    "Did you ", "Did you,",
    "Imagine,", "Imagine ",
    "This is", "This was",
    "I want",  "Let me",
)

_ABBREVIATION_RE = re.compile(r"\b(Dr|vs|etc)\.\s|\be\.g\.\s", re.I)
_FORBIDDEN_CHARS_RE = re.compile(r"[()/%&]")
_DIGIT_RUN_RE = re.compile(r"\b\d{2,}\b")
_CAPS_WORD_RE = re.compile(r"\b[A-Z]{3,}\b")

_SUMMARY_STARTERS = frozenset({
    "so",     "that",    "this",    "and that",
    "in the", "therefore", "thus",  "ultimately",
    "in short", "in summary", "in conclusion",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_markers(text: str) -> str:
    """Remove standalone section-marker lines from a script body."""
    return _MARKER_LINE_RE.sub("", text).strip()


def _word_count(text: str) -> int:
    """Count words after stripping section-marker lines."""
    cleaned = _strip_markers(text)
    return len(cleaned.split()) if cleaned else 0


_ABBREV_SENTINEL = "⁠"  # word joiner — invisible, never appears in scripts

def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, protecting common abbreviations from splitting."""
    sentinel = _ABBREV_SENTINEL
    # Protect abbreviation-period-space sequences so they don't look like sentence ends
    guarded = re.sub(
        r"\b(Dr|vs|etc)\.\s",
        lambda m: m.group(1) + sentinel + " ",
        text,
        flags=re.I,
    )
    guarded = re.sub(
        r"\be\.g\.\s",
        "e" + sentinel + "g" + sentinel + " ",
        guarded,
        flags=re.I,
    )
    # Second alternative handles period/!? inside closing quotes (.'  ."  .'  .") —
    # the quote character prevents the first lookbehind from firing.
    parts = re.split(r"(?<=[.!?])\s+|(?<=[.!?]['\"’”])\s+", guarded.strip())
    return [p.replace(sentinel, ".").strip() for p in parts if p.strip()]


def _section_bodies(voice_script: str) -> list[tuple[str, str, bool]]:
    """Return (marker_text, body_text, is_last) for every [SECTION N] in voice_script."""
    positions = list(_SECTION_MARKER_RE.finditer(voice_script))
    if not positions:
        return []

    outro = _OUTRO_LINE_RE.search(voice_script)
    script_end = outro.start() if outro else len(voice_script)

    result = []
    for i, match in enumerate(positions):
        body_start = match.end()
        body_end = (
            positions[i + 1].start() if i + 1 < len(positions) else script_end
        )
        marker_text = match.group(1).strip()
        body = voice_script[body_start:body_end].strip()
        result.append((marker_text, body, i == len(positions) - 1))
    return result


# ── Individual check functions ────────────────────────────────────────────────

def check_completeness(video_script: str, voice_script: str, language: str) -> list[dict]:
    """Check structural completeness: markers, section numbering, empty bodies, terminal punctuation.

    Args:
        video_script: The video script text.
        voice_script: The voice script text.
        language:     BCP-47 language code used to tag issues.

    Returns:
        List of MAJOR Issue dicts for each completeness violation found.
    """
    issues: list[dict] = []

    has_intro = bool(_INTRO_LINE_RE.search(video_script))
    has_outro = bool(_OUTRO_LINE_RE.search(video_script))
    section_nums = [int(m) for m in _SECTION_NUM_RE.findall(video_script)]

    if not has_intro:
        issues.append({
            "language": language, "severity": "MAJOR", "category": "completeness",
            "description": "[INTRO] marker missing from video_script",
            "suggestion": "Add [INTRO] on its own line at the beginning of the video_script.",
            "offending_text": None,
        })
    if not has_outro:
        issues.append({
            "language": language, "severity": "MAJOR", "category": "completeness",
            "description": "[OUTRO] marker missing from video_script",
            "suggestion": "Add [OUTRO] on its own line at the end of the video_script.",
            "offending_text": None,
        })
    if not section_nums:
        issues.append({
            "language": language, "severity": "MAJOR", "category": "completeness",
            "description": "No [SECTION N] markers found in video_script",
            "suggestion": "Add at least one [SECTION 1] marker between [INTRO] and [OUTRO].",
            "offending_text": None,
        })
    elif sorted(section_nums) != list(range(1, len(section_nums) + 1)):
        issues.append({
            "language": language, "severity": "MAJOR", "category": "completeness",
            "description": f"Non-consecutive [SECTION N] numbers: {section_nums}",
            "suggestion": "Renumber sections 1, 2, 3, … with no gaps.",
            "offending_text": None,
        })

    # Check for empty section bodies in video_script
    section_positions = list(_SECTION_MARKER_RE.finditer(video_script))
    outro_pos = _OUTRO_LINE_RE.search(video_script)
    vs_end = outro_pos.start() if outro_pos else len(video_script)
    for i, m in enumerate(section_positions):
        body_end = (
            section_positions[i + 1].start() if i + 1 < len(section_positions) else vs_end
        )
        body = video_script[m.end():body_end].strip()
        if not body:
            issues.append({
                "language": language, "severity": "MAJOR", "category": "completeness",
                "description": f"Empty body for {m.group(1).strip()} in video_script",
                "suggestion": f"Add scene description / script content after {m.group(1).strip()}.",
                "offending_text": None,
            })

    # Terminal punctuation check on voice_script
    content_lines = [
        ln.strip()
        for ln in voice_script.strip().splitlines()
        if ln.strip() and not _ANY_SECTION_LINE_RE.match(ln)
    ]
    if content_lines:
        last_ln = content_lines[-1]
        if last_ln and last_ln[-1] not in ".!?":
            issues.append({
                "language": language, "severity": "MAJOR", "category": "completeness",
                "description": "voice_script ends without terminal punctuation",
                "suggestion": "End the last sentence with a period, exclamation mark, or question mark.",
                "offending_text": last_ln[-60:],
            })

    return issues


def check_minimum_length(voice_script: str, language: str, script_format: str = "youtube_long") -> list[dict]:
    """Check that the voice script meets the format's minimum word count.

    Args:
        voice_script:  The voice script text.
        language:      BCP-47 language code used to tag issues.
        script_format: Format key — "youtube_long" requires 900 words; all others 420.

    Returns:
        List with one MAJOR Issue dict if below minimum, else empty list.
    """
    min_words = 900 if script_format == "youtube_long" else 420
    wc = _word_count(voice_script)
    if wc < min_words:
        return [{
            "language": language, "severity": "MAJOR", "category": "minimum_length",
            "description": (
                f"voice_script has {wc} words — minimum for {script_format} is {min_words}"
            ),
            "suggestion": (
                f"Expand existing sections with more depth, examples, or context from "
                f"the source material to reach {min_words} words. "
                f"Never pad with filler sentences."
            ),
            "offending_text": None,
        }]
    return []


def check_length_coherence(scripts_by_lang: dict[str, dict]) -> list[dict]:
    """Flag languages whose word count deviates >30% from the cross-language median.

    Args:
        scripts_by_lang: Dict mapping language code → {"video_script": str, "voice_script": str}.

    Returns:
        List of MAJOR Issue dicts for each outlier language found.
    """
    if len(scripts_by_lang) < 2:
        return []

    word_counts = {
        lang: _word_count(scripts.get("voice_script", ""))
        for lang, scripts in scripts_by_lang.items()
    }
    counts = list(word_counts.values())
    if not counts:
        return []

    median_wc = statistics.median(counts)
    if median_wc == 0:
        return []

    issues: list[dict] = []
    for lang, wc in word_counts.items():
        deviation = abs(wc - median_wc) / median_wc
        if deviation > 0.30:
            direction = "short" if wc < median_wc else "long"
            issues.append({
                "language": lang, "severity": "MAJOR", "category": "length_coherence",
                "description": (
                    f"voice_script has {wc} words — {deviation:.0%} {direction} "
                    f"of cross-language median ({int(median_wc)} words)"
                ),
                "suggestion": (
                    "Align the word count with other language versions (within 30% of median). "
                    "Expand thin sections with depth, or trim padded ones."
                ),
                "offending_text": None,
            })
    return issues


def check_hook_quality(voice_script: str, language: str) -> list[dict]:
    """Validate the hook: first sentence after [INTRO] must be ≤15 words and not start with a forbidden opener.

    Args:
        voice_script: The voice script text.
        language:     BCP-47 language code used to tag issues.

    Returns:
        List with one MAJOR Issue dict if hook fails, else empty list.
    """
    intro_match = _INTRO_LINE_RE.search(voice_script)
    if not intro_match:
        return []  # completeness check handles missing [INTRO]

    after_intro = voice_script[intro_match.end():]
    # Stop at the next marker line
    next_marker = _ANY_SECTION_LINE_RE.search(after_intro)
    intro_body = after_intro[:next_marker.start()] if next_marker else after_intro

    # Find the first non-empty line and extract first sentence from it
    first_sentence = ""
    for line in intro_body.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        sent_match = re.match(r"^([^.!?]+[.!?])", line)
        first_sentence = sent_match.group(1).strip() if sent_match else line
        break

    if not first_sentence:
        return []

    problems: list[str] = []
    word_count = len(first_sentence.split())
    if word_count > 15:
        problems.append(f"{word_count} words (max 15)")

    for opener in _FORBIDDEN_OPENERS:
        if first_sentence.lower().startswith(opener.lower()):
            problems.append(f"forbidden opener '{opener.strip()}'")
            break

    if not problems:
        return []

    return [{
        "language": language, "severity": "MAJOR", "category": "hook_quality",
        "description": f"Opening hook has problem(s): {'; '.join(problems)}: {first_sentence!r}",
        "suggestion": (
            "Rewrite ONLY the first sentence after [INTRO]: ≤12 words, "
            "name one specific person/place/date from facts already in the script, "
            "imply an unresolved outcome, no forbidden opener."
        ),
        "offending_text": first_sentence,
    }]


def check_tts_compliance(voice_script: str, language: str) -> list[dict]:
    """Detect text patterns that cause poor TTS audio quality.

    Checks each sentence for:
      - Length >18 words
      - Digit-runs (2+ consecutive digits — must be spelled out)
      - Forbidden characters: ( ) / % &
      - Abbreviations: Dr. vs. etc. e.g.
      - ALL-CAPS words of 3+ letters (not section markers — they are stripped first)

    Args:
        voice_script: The voice script text.
        language:     BCP-47 language code used to tag issues.

    Returns:
        List of MAJOR Issue dicts, one per offending sentence per violation type.
        Capped at 6 total to keep correction prompts manageable.
    """
    cleaned = _strip_markers(voice_script)
    sentences = _split_sentences(cleaned)
    issues: list[dict] = []

    for sentence in sentences:
        if len(issues) >= 6:
            break

        # 1. Sentence too long
        words = sentence.split()
        if len(words) > 18:
            issues.append({
                "language": language, "severity": "MAJOR", "category": "tts_compliance",
                "description": f"Sentence has {len(words)} words (max 18 for TTS readability)",
                "suggestion": "Split into two shorter sentences at a natural pause.",
                "offending_text": sentence[:150],
            })
            continue  # sentence already flagged — skip further checks on it

        # 2. Digit-run
        if _DIGIT_RUN_RE.search(sentence):
            issues.append({
                "language": language, "severity": "MAJOR", "category": "tts_compliance",
                "description": "Sentence contains digits — must be written as words for TTS",
                "suggestion": (
                    "Replace all numbers (years, figures, percentages) with their full "
                    "word equivalents in the target language."
                ),
                "offending_text": sentence[:150],
            })
            continue

        # 3. Forbidden characters
        if _FORBIDDEN_CHARS_RE.search(sentence):
            found = sorted(set(c for c in sentence if c in "()/%&"))
            issues.append({
                "language": language, "severity": "MAJOR", "category": "tts_compliance",
                "description": f"Sentence contains TTS-forbidden characters: {found}",
                "suggestion": (
                    "Remove parentheses, slashes, percent signs, and ampersands. "
                    "Rewrite the sentence as plain spoken language."
                ),
                "offending_text": sentence[:150],
            })
            continue

        # 4. Abbreviations
        if _ABBREVIATION_RE.search(sentence):
            issues.append({
                "language": language, "severity": "MAJOR", "category": "tts_compliance",
                "description": "Sentence contains abbreviation that TTS cannot pronounce naturally",
                "suggestion": (
                    "Expand abbreviations to full spoken form: "
                    "'Dr.' → 'Doctor', 'vs.' → 'versus', 'etc.' → 'and so on', "
                    "'e.g.' → 'for example'."
                ),
                "offending_text": sentence[:150],
            })
            continue

        # 5. ALL-CAPS words
        caps_words = _CAPS_WORD_RE.findall(sentence)
        if caps_words:
            issues.append({
                "language": language, "severity": "MAJOR", "category": "tts_compliance",
                "description": (
                    f"Sentence contains ALL-CAPS word(s) not suitable for TTS: {caps_words[:3]}"
                ),
                "suggestion": (
                    "Use mixed case or write out the full form. "
                    "TTS may read ALL-CAPS as spelled-out acronyms or shouts."
                ),
                "offending_text": sentence[:150],
            })

    return issues


def check_retention_structure(
    voice_script: str, language: str, script_format: str = "youtube_long"
) -> list[dict]:
    """Check section-level retention structure (curiosity gaps, no dead endings).

    For short-form formats: each section body should be ≤130 words OR contain
    at least one question mark (curiosity gap).

    For youtube_long: each section except the last must not end with a summary-pattern
    sentence (words like "So", "Thus", "In conclusion" create dead air before the next
    section, killing retention).

    Args:
        voice_script:  The voice script text.
        language:      BCP-47 language code used to tag issues.
        script_format: Format key from channel_config.

    Returns:
        List of MINOR Issue dicts.
    """
    issues: list[dict] = []
    bodies = _section_bodies(voice_script)

    for marker_text, body, is_last in bodies:
        if script_format == "youtube_long":
            if is_last:
                continue
            sentences = _split_sentences(body)
            if not sentences:
                continue
            last_sent = sentences[-1].strip()
            first_words = " ".join(last_sent.lower().split()[:4])
            for starter in _SUMMARY_STARTERS:
                if first_words.startswith(starter):
                    issues.append({
                        "language": language, "severity": "MINOR",
                        "category": "retention_structure",
                        "description": (
                            f"{marker_text} ends with a summary-pattern sentence "
                            f"— kills momentum before the next section"
                        ),
                        "suggestion": (
                            "Replace the closing sentence with a curiosity gap: "
                            "pose a question, hint at the next reveal, or end on an "
                            "unresolved beat that makes the viewer keep watching."
                        ),
                        "offending_text": last_sent[:100],
                    })
                    break
        else:
            # Short-form: ≤130 words OR has a question mark
            wc = len(body.split())
            if wc > 130 and "?" not in body:
                issues.append({
                    "language": language, "severity": "MINOR",
                    "category": "retention_structure",
                    "description": (
                        f"{marker_text} has {wc} words with no curiosity gap "
                        f"(short-form sections should be ≤130 words or contain a '?')"
                    ),
                    "suggestion": (
                        "Split into two sections (add a [SECTION N] marker), or add "
                        "a rhetorical question to create a re-hook point."
                    ),
                    "offending_text": None,
                })

    return issues


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_deterministic_checks(
    scripts_by_lang: dict[str, dict],
    script_format: str = "youtube_long",
) -> dict[str, list[dict]]:
    """Run all deterministic checks on the full set of scripts.

    Runs per-language checks (completeness, minimum_length, hook_quality,
    tts_compliance, retention_structure) and the cross-language check
    (length_coherence) on every language simultaneously.

    Args:
        scripts_by_lang: Dict mapping language code → {"video_script": str, "voice_script": str}.
        script_format:   Format key from channel_config (default "youtube_long").

    Returns:
        Dict mapping language code → list of Issue dicts (MAJOR and MINOR combined).
        Every language in scripts_by_lang has an entry (possibly empty).
    """
    issues_by_lang: dict[str, list[dict]] = {lang: [] for lang in scripts_by_lang}

    # ── Per-language checks ───────────────────────────────────────────────────
    for lang, scripts in scripts_by_lang.items():
        video_script = scripts.get("video_script", "")
        voice_script = scripts.get("voice_script", "")

        issues_by_lang[lang].extend(check_completeness(video_script, voice_script, lang))
        issues_by_lang[lang].extend(check_minimum_length(voice_script, lang, script_format))
        issues_by_lang[lang].extend(check_hook_quality(voice_script, lang))
        issues_by_lang[lang].extend(check_tts_compliance(voice_script, lang))
        issues_by_lang[lang].extend(check_retention_structure(voice_script, lang, script_format))

        n_major = sum(1 for i in issues_by_lang[lang] if i["severity"] == "MAJOR")
        n_minor = sum(1 for i in issues_by_lang[lang] if i["severity"] == "MINOR")
        logger.info(
            "Deterministic checks lang=%s: %d MAJOR, %d MINOR",
            lang, n_major, n_minor,
        )

    # ── Cross-language check ──────────────────────────────────────────────────
    for issue in check_length_coherence(scripts_by_lang):
        lang = issue["language"]
        if lang in issues_by_lang:
            issues_by_lang[lang].append(issue)

    return issues_by_lang
