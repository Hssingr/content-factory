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

These checks run deterministically (no I/O, no Claude calls). Since the
Elimination Mandate there is no AI validation stage behind them — findings are
telemetry only; mechanical fixes (normalize_tts_chars/split_long_sentences) are
the only enforcement.
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
    "Today,", "Today ",
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
_WORD_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_ENGLISH_STYLE_LANGS = {"source", "en", "en-us", "en-gb", "en-ca", "en-au"}

# Split candidates for long-sentence backstop (natural break points only)
# Group 1 alternative: semicolon or em-dash with trailing space
# Group 2 alternative: comma + conjunction (lookahead — conjunction is kept in `after`)
_LONG_SENT_SPLIT_RE = re.compile(
    r"[;—]\s+"
    r"|,\s+(?=(?:and|but|so|because|although|while|when|which|who|that)\b)",
    re.IGNORECASE,
)

# TTS character normalisation — applied as a final backstop after retries are exhausted
_NORM_PARENS_RE       = re.compile(r"\(([^)]*)\)")           # balanced (content) → content
_NORM_LONE_PAREN_RE   = re.compile(r"[()]")                  # lone parens → remove
_NORM_SLASH_WORD_RE   = re.compile(r"(\w+)\s*/\s*(\w+)")    # word/word → word or word
_NORM_SLASH_RE        = re.compile(r"\s*/\s*")               # remaining / → space
_NORM_PCT_RE          = re.compile(r"(\d+)\s*%")             # 50% → 50 percent
_NORM_AMP_RE          = re.compile(r"\s*&\s*")               # & → and
_NORM_MULTI_SPACE_RE  = re.compile(r" {2,}")                 # collapse double spaces

_GENERIC_DOCUMENTARY_PHRASES: tuple[str, ...] = (
    "this is not just",
    "something far worse",
    "what happened next",
    "the answer is worse",
    "but here's the thing",
    "but that's not all",
    "little did they know",
    "it gets worse",
    "you won't believe",
    "the truth is",
    "believe it or not",
    "here's where it gets",
    "things took a turn",
    "what nobody knew",
    "and that's when everything changed",
    "in ways nobody could have imagined",
    "a shocking revelation",
    "brace yourself",
    "this is the story of",
    "but the story doesn't end there",
    "what really happened",
    "the real story behind",
    "this changes everything",
    "nobody saw it coming",
)

_SUMMARY_STARTERS = frozenset({
    "so",     "that",    "this",    "and that",
    "in the", "therefore", "thus",  "ultimately",
    "in short", "in summary", "in conclusion",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_markers(text: str) -> str:
    """Remove standalone section-marker lines from a script body."""
    return _MARKER_LINE_RE.sub("", text).strip()


def _word_tokens(text: str) -> list[str]:
    """Return Unicode-aware word tokens, excluding punctuation/underscores."""
    return _WORD_TOKEN_RE.findall(text or "")


def _word_count(text: str) -> int:
    """Count Unicode word tokens after stripping section-marker lines."""
    cleaned = _strip_markers(text)
    return len(_word_tokens(cleaned)) if cleaned else 0


def _uses_english_style_rules(language: str | None) -> bool:
    """True for English/source scripts where English phrase checks are meaningful."""
    lang = (language or "").strip().casefold().replace("_", "-")
    return lang in _ENGLISH_STYLE_LANGS or lang.startswith("en-")


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


def split_sentences(text: str) -> list[str]:
    """Public sentence splitter — the single tokenizer for sentence-level rules.

    Fresh full-system audit §3.5/D-1: the ≤18-word rule used to be counted with
    four different ad-hoc splitters across scripts.py / script_checks.py, so a
    borderline sentence could be "fixed" by one function and still "flagged" by
    another. Every sentence-splitting call site should use this (it guards
    common abbreviations and closing-quote punctuation, unlike a plain
    ``re.split`` on sentence-ending characters).
    """
    return _split_sentences(text)


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


# ── Public backstop ──────────────────────────────────────────────────────────

def split_long_sentences(text: str, max_words: int = 18) -> str:
    """Deterministically split sentences that exceed max_words at a natural break point.

    Applied as a mechanical backstop (``_clean_generated_section()`` /
    ``_apply_final_tts_backstop()``) before ``check_tts_compliance`` runs —
    eliminates the most common long-sentence TTS violations deterministically.

    Break points tried in order (first match wins):
    1. ``[;—]`` followed by whitespace
    2. ``,`` followed by whitespace and a coordinating/subordinating conjunction
       (``and``, ``but``, ``so``, ``because``, ``although``, ``while``, ``when``,
       ``which``, ``who``, ``that``)

    Sentences with no break point are left unchanged — ``check_tts_compliance``
    still flags them (telemetry only; no regeneration exists).

    Processes the text line-by-line to preserve paragraph structure (newlines).
    Each line is split into sentences using the same ``_split_sentences()`` tokeniser
    as ``check_tts_compliance`` so both functions see the same sentence boundaries.

    Args:
        text:      Section body text (no section-marker lines expected).
        max_words: Sentence length threshold. Defaults to 18 to match TTS compliance rule.

    Returns:
        Text with qualifying long sentences split; original text returned if nothing changed.
    """
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    total_fixed = 0

    for line in lines:
        stripped = line.rstrip("\n\r")
        if not stripped.strip():
            result.append(line)
            continue

        sentences = _split_sentences(stripped)
        out: list[str] = []

        for sent in sentences:
            if len(_word_tokens(sent)) <= max_words:
                out.append(sent)
                continue

            m = _LONG_SENT_SPLIT_RE.search(sent)
            if not m:
                logger.debug(
                    "split_long_sentences: no split point for %d-word sentence — leaving: %.80r",
                    len(_word_tokens(sent)), sent,
                )
                out.append(sent)
                continue

            before = sent[: m.start()].rstrip(",;—").rstrip()
            after  = sent[m.end() :].strip()

            if before and before[-1] not in ".!?":
                before += "."
            if after:
                after = after[0].upper() + after[1:]

            if before:
                out.append(before)
            if after:
                out.append(after)
            total_fixed += 1
            logger.debug(
                "split_long_sentences: split %d-word sentence at pos %d",
                len(_word_tokens(sent)), m.start(),
            )

        trailing = line[len(stripped):]   # restore original line ending (\n etc.)
        result.append(" ".join(out) + trailing)

    if total_fixed:
        logger.debug(
            "split_long_sentences: fixed %d long sentence(s) (max_words=%d)",
            total_fixed, max_words,
        )
    return "".join(result)


def normalize_tts_chars(text: str) -> str:
    """Replace or remove characters that are forbidden by check_tts_compliance.

    Applied as a final deterministic backstop after Claude retries are exhausted.
    Safe to call multiple times — idempotent.

    Transformations (in order):
    - ``(content)`` → ``content``  (parens removed, inner text kept)
    - ``word/word`` → ``word or word``
    - remaining ``/`` → space
    - ``50%`` → ``50 percent``
    - ``&`` → ``and``
    - Collapses any double-spaces introduced by the above.

    Args:
        text: Script section body text (markers acceptable — they pass through unchanged).

    Returns:
        Text with forbidden characters normalised.
    """
    text = _NORM_PARENS_RE.sub(r"\1", text)
    text = _NORM_LONE_PAREN_RE.sub("", text)
    text = _NORM_SLASH_WORD_RE.sub(r"\1 or \2", text)
    text = _NORM_SLASH_RE.sub(" ", text)
    text = _NORM_PCT_RE.sub(r"\1 percent", text)
    text = _NORM_AMP_RE.sub(" and ", text)
    text = _NORM_MULTI_SPACE_RE.sub(" ", text)
    return text


# ── Individual check functions ────────────────────────────────────────────────

def check_completeness(voice_script: str, language: str) -> list[dict]:
    """Check structural completeness: markers, section numbering, empty bodies, terminal punctuation.

    Args:
        voice_script: The voice script text.
        language:     BCP-47 language code used to tag issues.

    Returns:
        List of MAJOR Issue dicts for each completeness violation found.
    """
    issues: list[dict] = []

    has_intro = bool(_INTRO_LINE_RE.search(voice_script))
    has_outro = bool(_OUTRO_LINE_RE.search(voice_script))
    section_nums = [int(m) for m in _SECTION_NUM_RE.findall(voice_script)]

    if not has_intro:
        issues.append({
            "language": language, "severity": "MAJOR", "category": "completeness",
            "description": "[INTRO] marker missing from voice_script",
            "suggestion": "Add [INTRO] on its own line at the beginning of the voice_script.",
            "offending_text": None,
        })
    if not has_outro:
        issues.append({
            "language": language, "severity": "MAJOR", "category": "completeness",
            "description": "[OUTRO] marker missing from voice_script",
            "suggestion": "Add [OUTRO] on its own line at the end of the voice_script.",
            "offending_text": None,
        })
    if not section_nums:
        issues.append({
            "language": language, "severity": "MAJOR", "category": "completeness",
            "description": "No [SECTION N] markers found in voice_script",
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

    # Check for empty section bodies in voice_script
    section_positions = list(_SECTION_MARKER_RE.finditer(voice_script))
    outro_pos = _OUTRO_LINE_RE.search(voice_script)
    vs_end = outro_pos.start() if outro_pos else len(voice_script)
    for i, m in enumerate(section_positions):
        body_end = (
            section_positions[i + 1].start() if i + 1 < len(section_positions) else vs_end
        )
        body = voice_script[m.end():body_end].strip()
        if not body:
            issues.append({
                "language": language, "severity": "MAJOR", "category": "completeness",
                "description": f"Empty body for {m.group(1).strip()} in voice_script",
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


# Roadmap 4b / audit P1-5: a real production run generated a 1,384-word
# long-form script grounded on a 3,070-char (~560-word) source_excerpt — every
# specific detail beyond that thin summary was necessarily model-invented
# ("page fourteen", "nine hundred feet in", the K-initials device), and no
# validator can check faithfulness against a source that isn't there. The
# floor mirrors check_minimum_length()'s own per-format word targets just
# above: a script cannot be responsibly grounded in a source with fewer words
# than the shortest acceptable script itself.

# The Solo Short's own script_format value (output_mode="shorts_only" — see
# code_report/output_mode_shorts_only_and_youtube_long_only_roadmap.md).
# check_source_material_floor()'s and agent4's _BEAT_SECONDS_BY_FORMAT's
# branching both key on "== 'youtube_long'" — any other string already
# satisfies "not youtube_long" — so this constant exists purely so every
# caller that needs a short-appropriate value shares one name instead of
# three ad-hoc string literals (discovery.py's floor check, Agent 2's
# _run_solo_short_script_workflow() floor check, Agent 4's split_into_beats()
# call in _run_solo_short_visuals()).
SOLO_SHORT_SCRIPT_FORMAT = "solo_short"


def check_source_material_floor(
    source_excerpt: str, language: str, script_format: str = "youtube_long"
) -> list[dict]:
    """Check that source_excerpt carries enough material to ground a full script.

    Args:
        source_excerpt: Raw discovered source material (``Content.source_excerpt``).
        language:       BCP-47 language code used to tag issues (the content's
                        source language — this check runs before any Script row
                        exists for any language).
        script_format:  Format key — "youtube_long" requires 900 words (mirrors
                        check_minimum_length's own floor); any other format
                        requires 420.

    Returns:
        List with one MAJOR Issue dict if the source is too thin, else empty list.
    """
    min_words = 900 if script_format == "youtube_long" else 420
    wc = _word_count(source_excerpt)
    if wc < min_words:
        return [{
            "language": language, "severity": "MAJOR", "category": "source_material_floor",
            "description": (
                f"source_excerpt has {wc} words — below the {min_words}-word floor "
                f"required to ground a {script_format} script without fabricating detail"
            ),
            "suggestion": (
                "Discover a story with richer source material (full verbatim post "
                "text plus top comments, not a summary), or configure a shorter "
                "target script format that this source can actually support."
            ),
            "offending_text": None,
        }]
    return []


# Roadmap 3.8 / audit G-7: the quality gate had no upper bound at all before
# this — a real production script reached 1,799 words against the documented
# 1,200-1,600 word spec (§9.3's _BASE_YOUTUBE_LONG_FORM_NATIVE), producing a
# 13.3-minute video instead of the intended ~9-10 minutes. Tolerance keeps a
# script that is only slightly over target from tripping a rewrite loop for a
# negligible overage; anything past the ceiling is a real spec violation.
_MAX_LONG_FORM_WORDS = 1600
_MAX_LONG_FORM_TOLERANCE = 150  # ceiling: 1,750 words
_MAX_SHORT_FORM_WORDS = 700     # matches _BASE_SHORT_FORM_NATIVE's own 420-700 word spec
_MAX_SHORT_FORM_TOLERANCE = 100  # ceiling: 800 words


def check_maximum_length(voice_script: str, language: str, script_format: str = "youtube_long") -> list[dict]:
    """Check that the voice script does not exceed its format's calibrated maximum.

    Args:
        voice_script:  The voice script text.
        language:      BCP-47 language code used to tag issues.
        script_format: Format key — "youtube_long" caps at 1,600 (+150 tolerance)
                       words; any other format caps at 700 (+100 tolerance) words.

    Returns:
        List with one MAJOR Issue dict if over the calibrated ceiling, else empty list.
    """
    is_long_form = script_format == "youtube_long"
    max_words = _MAX_LONG_FORM_WORDS if is_long_form else _MAX_SHORT_FORM_WORDS
    tolerance = _MAX_LONG_FORM_TOLERANCE if is_long_form else _MAX_SHORT_FORM_TOLERANCE
    ceiling = max_words + tolerance

    wc = _word_count(voice_script)
    if wc > ceiling:
        return [{
            "language": language, "severity": "MAJOR", "category": "maximum_length",
            "description": (
                f"voice_script has {wc} words — exceeds the {ceiling}-word calibrated "
                f"ceiling for {script_format} (target {max_words} words)."
            ),
            "suggestion": (
                f"Cut {wc - max_words} words by removing redundant recap, tightening "
                f"description, or trimming the least essential sentences. Do not remove "
                f"required structural markers or narrative content needed for coherence."
            ),
            "offending_text": None,
        }]
    return []


def check_length_coherence(scripts_by_lang: dict[str, dict]) -> list[dict]:
    """Flag languages whose word count deviates >30% from the cross-language median.

    PLACEMENT CONSTRAINT: must only be called after ALL language scripts (source +
    all native adaptations) are fully assembled. Never call at section level or before
    multilingual generation completes. The caller is generate_multilingual_scripts()
    (agent2/scripts.py), invoked once after all Script rows are persisted.

    Args:
        scripts_by_lang: Dict mapping language code → {"voice_script": str}.

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
    word_count = len(_word_tokens(first_sentence))
    if word_count > 15:
        problems.append(f"{word_count} words (max 15)")

    if _uses_english_style_rules(language):
        first_sentence_lower = first_sentence.casefold()
        for opener in _FORBIDDEN_OPENERS:
            if first_sentence_lower.startswith(opener.casefold()):
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
        words = _word_tokens(sentence)
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
            if not _uses_english_style_rules(language):
                continue
            first_words = " ".join(token.casefold() for token in _word_tokens(last_sent)[:4])
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
            wc = _word_count(body)
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


def check_section_transition(
    current_section_text: str,
    prior_section_summary: str,
    language: str = "source",
) -> list[dict]:
    """Detect the recap anti-pattern: current section opens by restating prior section.

    Pure Python — no I/O, no Claude calls.

    Heuristic: the first 2 sentences of ``current_section_text`` contain ≥3 distinct
    content tokens (>3 chars) that also appear in ``prior_section_summary``. This
    signals the section is opening with a summary of what was just said rather than
    advancing the story.

    Always MINOR — telemetry only (no retry mechanism exists). Never blocks
    section acceptance.

    Args:
        current_section_text:  Narration text of the section being validated (no markers).
        prior_section_summary: Summary string from the prior section's generation result.
        language:              BCP-47 code used to tag the issue.

    Returns:
        List with one MINOR Issue dict if recap is detected, else empty list.
    """
    if not prior_section_summary.strip() or not current_section_text.strip():
        return []

    # Extract first 2 sentences of current section
    raw_sentences = re.split(r"(?<=[.!?])\s+", current_section_text.strip())
    opening = " ".join(s.strip() for s in raw_sentences[:2] if s.strip())
    if not opening:
        return []

    def _content_tokens(text: str) -> set[str]:
        return {w.lower() for w in re.findall(r"\b\w+\b", text) if len(w) > 3}

    prior_tokens   = _content_tokens(prior_section_summary)
    opening_tokens = _content_tokens(opening)
    overlap        = prior_tokens & opening_tokens

    if len(overlap) >= 3:
        sample = ", ".join(sorted(overlap)[:5])
        return [{
            "language":      language,
            "severity":      "MINOR",
            "category":      "section_transition",
            "description":   (
                f"Section opens with recap of prior section "
                f"({len(overlap)} overlapping phrases: {sample})"
            ),
            "suggestion": (
                "Open this section with new information, not a restatement of what "
                "was just said. Start with the next reveal, a new fact, or a question."
            ),
            "offending_text": opening[:120],
        }]

    return []


# Sentence-length bands for check_sentence_rhythm_variance — mirrors
# TTS_BLOCK["sonic-2"]'s rhythm rule (short 3-7 word punchy sentences
# alternating with longer 12-18 word buildup sentences). "long" uses the
# existing 18-word TTS ceiling as its upper reference point, not a new constant.
_RHYTHM_SHORT_MAX_WORDS  = 7    # <= 7 words: short
_RHYTHM_MEDIUM_MAX_WORDS = 11   # 8-11 words: medium
# >= 12 words: long

# A run of this many or more consecutive sentences in the same length band
# is flagged — matches the real defect found: 4 consecutive 12-18 word
# sentences in one INTRO, with no short sentences anywhere in it.
_RHYTHM_RUN_LENGTH = 3


def _rhythm_band(word_count: int) -> str:
    if word_count <= _RHYTHM_SHORT_MAX_WORDS:
        return "short"
    if word_count <= _RHYTHM_MEDIUM_MAX_WORDS:
        return "medium"
    return "long"


def check_sentence_rhythm_variance(section_text: str, language: str = "source") -> list[dict]:
    """Detect a run of 3+ consecutive sentences in the same length band (flat rhythm).

    Pure Python — no I/O, no Claude calls. Deterministic backstop for
    TTS_BLOCK["sonic-2"]'s rhythm rule (alternate short punchy sentences with
    longer buildup sentences) for the cases where that prompt instruction is
    not followed consistently within one section — an execution-consistency
    gap, not a missing-instruction gap (the instruction already exists in
    every script-producing prompt via TTS_BLOCK; this only catches it when
    reinforcement isn't enough).

    Bands: short (<=7 words), medium (8-11 words), long (>=12 words).

    Always MINOR — advisory telemetry only, same as check_retention_structure()
    and check_section_transition(). No retry mechanism exists; findings are
    logged and never block section acceptance.

    Args:
        section_text: Narration text for one section (markers, if present,
                      are stripped before splitting).
        language:     BCP-47 code used to tag the issue.

    Returns:
        List with one MINOR Issue dict for the first same-band run of
        length >= _RHYTHM_RUN_LENGTH found, else empty list — same
        single-issue return shape as check_section_transition().
    """
    sentences = _split_sentences(_strip_markers(section_text))
    if len(sentences) < _RHYTHM_RUN_LENGTH:
        return []

    bands = [_rhythm_band(len(s.split())) for s in sentences]

    run_band = bands[0]
    run_start = 0
    for i in range(1, len(bands) + 1):
        if i < len(bands) and bands[i] == run_band:
            continue
        run_length = i - run_start
        if run_length >= _RHYTHM_RUN_LENGTH:
            offending = " ".join(sentences[run_start:i])
            return [{
                "language":      language,
                "severity":      "MINOR",
                "category":      "sentence_rhythm_variance",
                "description": (
                    f"{run_length} consecutive sentences are all '{run_band}' length "
                    f"— flat narration rhythm, no short/long alternation"
                ),
                "suggestion": (
                    "Break up this run by rewriting one sentence as a short (3-7 word) "
                    "punchy sentence or a longer (12-18 word) buildup sentence, matching "
                    "the TTS rhythm rule (alternate short and long, never 3+ in a row "
                    "the same length)."
                ),
                "offending_text": offending[:150],
            }]
        if i < len(bands):
            run_band = bands[i]
            run_start = i

    return []


def detect_generic_documentary_phrases(voice_script: str) -> list[dict]:
    """Scan an English/source voice_script for banned generic AI-documentary phrases.

    Pure Python — no I/O, no Claude calls. The phrase list is intentionally
    English-only; callers should use it only for source/English scripts. The caller is
    responsible for logging at WARNING level. This function never blocks or fails the pipeline.

    Args:
        voice_script: Fully assembled voice_script (markers included are fine).

    Returns:
        List of dicts: {"phrase": str, "sentence": str}. Empty list = clean.
    """
    script_lower = voice_script.lower()
    sentences    = re.split(r"(?<=[.!?])\s+", voice_script)
    hits: list[dict] = []
    for phrase in _GENERIC_DOCUMENTARY_PHRASES:
        if phrase not in script_lower:
            continue
        for sent in sentences:
            if phrase in sent.lower():
                hits.append({"phrase": phrase, "sentence": sent.strip()[:150]})
                break  # report the phrase once — first sentence containing it
    return hits

