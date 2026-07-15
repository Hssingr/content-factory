"""Subtitles generator — converts Whisper word-level timestamps into subtitle data.

Two output styles:

  standard  (main 16:9 video)
    Groups consecutive words into readable caption chunks (≤ MAX_WORDS or ≤ MAX_DURATION_MS).
    Output: [{text, start_ms, end_ms}, ...]

  karaoke   (Shorts 9:16)
    Groups words into chunks, but each chunk carries individual word timings so Remotion
    can highlight the word currently being spoken in the active color.
    Output: [{words: [{w, s, e}, ...], start_ms, end_ms}, ...]

No Claude call — pure Python computed from Whisper timestamps.
Whisper transcript format: [{"word": str, "start": float, "end": float}]

Caption punctuation restoration (roadmap Phase B2, operator video-output
audit): OpenAI Whisper's word-level `words` array strips punctuation and
mangles French elisions ("Sainte Sophie n était plus" instead of
"Sainte-Sophie n'était plus"), splits numbers into separate digit tokens
("30"/"000" instead of one "30,000"/"30 000"), and occasionally mishears a
recurring proper noun entirely (a real production run showed "Belisarius"
transcribed as "Narcissus"/"Bélisère"/"Narcès"). None of this is a rendering
bug — `_chunk_transcript()`'s own sentence/clause-boundary punctuation
regexes were always correct, they just never had any punctuation to match
against. `_restore_punctuated_words()` below realigns Whisper's real word
timings against the punctuated Script `voice_script` text (the ground-truth
narration) and takes DISPLAY text from the script side — deterministic, no
AI call, no new dependency (stdlib `difflib`).
"""

import difflib
import logging
import re

logger = logging.getLogger(__name__)

# Caption chunk limits — chunks split on natural phrase/sentence boundaries with a
# minimum size, rather than purely on a word-count or duration ceiling that could
# fire mid-phrase and produce broken-fragment captions.
_MIN_WORDS_STANDARD      = 3
_TARGET_WORDS_STANDARD   = 7
_MAX_WORDS_STANDARD      = 10     # hard cap; was 12 — tighter for readability
_MAX_DURATION_MS         = 4500   # split chunk if it would exceed 4.5 s

_MIN_WORDS_KARAOKE       = 2
_TARGET_WORDS_KARAOKE    = 4
_MAX_WORDS_KARAOKE       = 5      # Shorts: 4-5 word karaoke chunks for mobile readability
_MAX_DURATION_MS_KARAOKE = 3000

_DEFAULT_KARAOKE_COLOR = "#FFD700"

_SENTENCE_END_RE = re.compile(r"[.!?…]$")
_CLAUSE_END_RE   = re.compile(r"[,;:—–]$")

# ── Script-based caption punctuation restoration (roadmap Phase B2) ──────────

_SECTION_MARKER_LINE_RE = re.compile(
    r"^\s*\[(?:INTRO|OUTRO|SECTION[^\]]*)\]\s*$", re.IGNORECASE | re.MULTILINE,
)
# Any other bracketed span — ElevenLabs v3 audio tags ([dramatic pause],
# [whispers], …) embedded in voice_script by Agent 2's AUDIO_TAGS_INSTRUCTION
# / eleven_v3 TTS block. Pre-next-test roadmap Tier 2 R5: three confirmed
# production leaks (FR main caption @389.2s, FR Short 0 @77.6s, FR Short 2
# @39.5s) where a tag left in the caption-restoration alignment reference
# fell inside a replace-block merge and shipped verbatim as caption display
# text. Scrubbing every bracket span is safe by construction: no bracket
# content is ever spoken (the TTS provider consumes tags; markers are
# stripped before TTS), so it can never legitimately align to a Whisper word.
_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_NORMALIZE_STRIP_RE = re.compile(r"[^\w]+", re.UNICODE)
_ALL_DIGITS_RE      = re.compile(r"^\d+$")
_THREE_DIGITS_RE    = re.compile(r"^\d{3}$")

# A 'replace' opcode block bigger than this on either side signals a real
# divergence (not just noisy transcription of the same words) — too risky
# to trust the script's text for, so we fail open to Whisper's own raw text.
_MAX_MERGE_SPAN = 6

# difflib.get_close_matches() similarity threshold and minimum word length
# for the named-entity correction pass — short/common words are excluded so
# this never misfires on ordinary vocabulary, only genuinely name-shaped words.
_ENTITY_MATCH_CUTOFF = 0.72
_ENTITY_MIN_WORD_LEN = 4


def _normalize_token(raw: str) -> str:
    """Lowercase, Unicode-aware comparison key: strips ALL punctuation,
    including apostrophes — so "n'était" and Whisper's own "n" + "etait"
    split are recognized as occupying the *same span* by sequence-alignment
    position even though neither individually equals the other. Used only
    for comparison, never for display (display always comes from the
    original, unstripped token)."""
    return _NORMALIZE_STRIP_RE.sub("", raw).lower()


def _merge_number_groups(tokens: list[str]) -> list[str]:
    """Merge a leading all-digit token followed by one or more exact 3-digit
    groups into one atomic display token ("30", "000" -> "30 000") — the
    French/European thousands-grouping convention using a space separator.
    A script already written with commas ("30,000") is already a single
    whitespace token and needs no merging here."""
    merged: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if _ALL_DIGITS_RE.match(token):
            group = [token]
            j = i + 1
            while j < n and _THREE_DIGITS_RE.match(tokens[j]):
                group.append(tokens[j])
                j += 1
            if len(group) > 1:
                merged.append(" ".join(group))
                i = j
                continue
        merged.append(token)
        i += 1
    return merged


def _tokenize_script(voice_script: str) -> list[str]:
    """Split punctuated narration into display tokens: strips
    [INTRO]/[SECTION N]/[OUTRO] marker lines AND every other bracketed span
    (v3 audio tags — Tier 2 R5, see ``_BRACKET_TAG_RE``), splits on
    whitespace (preserving each word's own attached punctuation for
    display), and merges French-style space-grouped numbers into one
    atomic token."""
    stripped = _SECTION_MARKER_LINE_RE.sub("", voice_script or "")
    stripped = _BRACKET_TAG_RE.sub(" ", stripped)
    return _merge_number_groups(stripped.split())


def _correct_named_entities(
    whisper_words: list[dict], proper_nouns: list[str] | None,
) -> list[dict]:
    """Replace a Whisper word's own text with a known story proper noun's
    canonical spelling when the two are a close fuzzy match — a safety net
    for the specific failure sequence-alignment alone can miss: Whisper
    hearing a totally different word for a character/place name (a real
    production run showed "Belisarius" transcribed as "Narcissus"). Never
    touches timing. No-op when no proper_nouns are supplied.
    """
    if not proper_nouns:
        return whisper_words
    lookup = {noun.lower(): noun for noun in proper_nouns}
    candidates = list(lookup.keys())

    corrected: list[dict] = []
    for w in whisper_words:
        norm = _normalize_token(w.get("word", ""))
        if len(norm) >= _ENTITY_MIN_WORD_LEN and norm not in lookup:
            match = difflib.get_close_matches(norm, candidates, n=1, cutoff=_ENTITY_MATCH_CUTOFF)
            if match:
                corrected.append({**w, "word": lookup[match[0]]})
                continue
        corrected.append(w)
    return corrected


def _restore_punctuated_words(
    whisper_transcript: list[dict],
    voice_script: str,
    proper_nouns: list[str] | None = None,
) -> list[dict]:
    """Realign Whisper's word-level timestamps against the punctuated Script
    ``voice_script`` text: display text comes from the script (real
    punctuation, apostrophes, elisions, atomic numbers, correct proper-noun
    spelling); timing stays Whisper's own, word for word.

    Deterministic, no AI call — monotonic sequence alignment
    (``difflib.SequenceMatcher`` over normalized comparison tokens) between
    the two near-identical word sequences, since both are the same
    narration read in the same order. Falls open to Whisper's own raw
    (named-entity-corrected) text for anything it can't confidently align:
    an oversized mismatch block, or a Whisper word with no script
    counterpart at all — never fabricates content or timing.

    Returns the exact same shape as ``whisper_transcript`` itself
    (``[{"word": str, "start": float, "end": float}, ...]``) so it is a
    drop-in replacement anywhere a raw Whisper transcript is consumed —
    ``_chunk_transcript()`` needs no changes at all. A script-only word (no
    Whisper timing to anchor it to) is dropped, never given a guessed
    timestamp.
    """
    if not whisper_transcript or not voice_script:
        return []

    whisper_words = _correct_named_entities(
        [w for w in whisper_transcript if (w.get("word") or "").strip()],
        proper_nouns,
    )
    whisper_norms = [_normalize_token(w["word"]) for w in whisper_words]

    script_tokens = _tokenize_script(voice_script)
    if not script_tokens:
        return whisper_words
    script_norms = [_normalize_token(t) for t in script_tokens]

    matcher = difflib.SequenceMatcher(None, whisper_norms, script_norms, autojunk=False)

    result: list[dict] = []
    merged_blocks = 0
    fallback_words = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                w = whisper_words[i1 + k]
                result.append({"word": script_tokens[j1 + k], "start": w.get("start", 0), "end": w.get("end", 0)})
        elif tag == "replace" and (i2 - i1) <= _MAX_MERGE_SPAN and (j2 - j1) <= _MAX_MERGE_SPAN:
            first_w = whisper_words[i1]
            last_w  = whisper_words[i2 - 1]
            result.append({
                "word": " ".join(script_tokens[j1:j2]),
                "start": first_w.get("start", 0),
                "end": last_w.get("end", 0),
            })
            merged_blocks += 1
        elif tag in ("replace", "delete"):
            # Oversized/unreliable mismatch, or Whisper-only words with no
            # script counterpart — fail open to Whisper's own text rather
            # than inventing or silently dropping real spoken content.
            for k in range(i1, i2):
                w = whisper_words[k]
                result.append({"word": w["word"], "start": w.get("start", 0), "end": w.get("end", 0)})
                fallback_words += 1
        # tag == "insert": script-only words with no Whisper timing to
        # anchor them to — skipped, never given a fabricated timestamp.

    logger.debug(
        "Caption punctuation restoration: %d whisper words, %d script tokens, "
        "%d merged blocks, %d words fell back to raw Whisper text",
        len(whisper_words), len(script_tokens), merged_blocks, fallback_words,
    )
    return result


def _chunk_transcript(
    whisper_transcript: list[dict],
    min_words: int,
    target_words: int,
    max_words: int,
    max_duration_ms: int,
) -> list[list[dict]]:
    """Group Whisper words into readable caption chunks on natural boundaries.

    Splits preferentially at sentence-ending punctuation (``.!?…``), then at
    clause-ending punctuation (``,;:—–``) once the chunk has reached
    ``target_words``, and only falls back to a hard word-count/duration ceiling
    when no natural boundary appears in time. A trailing chunk smaller than
    ``min_words`` is merged into the previous chunk so captions never end on an
    orphaned one- or two-word fragment.

    Args:
        whisper_transcript: Word-level Whisper output
                            (``[{"word": str, "start": float, "end": float}]``).
        min_words:          Minimum words before a punctuation-based split is allowed.
        target_words:       Word count at which a clause-boundary split becomes preferred.
        max_words:          Hard ceiling — always split once a chunk reaches this size.
        max_duration_ms:    Hard ceiling — always split once a chunk reaches this duration.

    Returns:
        List of chunks; each chunk is a list of
        ``{"word": str, "start_ms": int, "end_ms": int}`` dicts, in order.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []

    for word_data in whisper_transcript:
        word = word_data.get("word", "").strip()
        if not word:
            continue

        current.append({
            "word":     word,
            "start_ms": int(word_data.get("start", 0) * 1000),
            "end_ms":   int(word_data.get("end", 0) * 1000),
        })

        n        = len(current)
        duration = current[-1]["end_ms"] - current[0]["start_ms"]

        if n >= max_words or duration >= max_duration_ms:
            chunks.append(current)
            current = []
        elif n >= min_words and _SENTENCE_END_RE.search(word):
            chunks.append(current)
            current = []
        elif n >= target_words and _CLAUSE_END_RE.search(word):
            chunks.append(current)
            current = []

    if current:
        if chunks and len(current) < min_words:
            chunks[-1].extend(current)
        else:
            chunks.append(current)

    return chunks


def build_standard_subtitles(
    whisper_transcript: list[dict],
    voice_script: str = "",
    proper_nouns: list[str] | None = None,
) -> list[dict]:
    """Generate standard subtitle captions from Whisper word timestamps.

    Chunks split on natural sentence/clause boundaries (with minimum-size and
    hard-ceiling safeguards via ``_chunk_transcript``) so captions read as clean,
    grammatically coherent phrases rather than broken word fragments. Timestamps
    come directly from Whisper — no approximation.

    Args:
        whisper_transcript: Word-level Whisper output.
                            Each word: {"word": str, "start": float, "end": float}
        voice_script:       Optional punctuated narrator script text (roadmap
                            Phase B2). When supplied, caption display text is
                            restored from the script (real punctuation,
                            apostrophes/elisions, atomic numbers, correct
                            proper-noun spelling) while keeping Whisper's own
                            word timings — see ``_restore_punctuated_words()``.
                            Omit for the exact pre-existing behavior (raw
                            Whisper words as display text).
        proper_nouns:       Optional list of the story's recurring proper
                            nouns (``app.services.story_entities.
                            extract_recurring_proper_nouns()``) — corrects
                            Whisper mishearings of these specific names even
                            where sequence alignment alone might not.

    Returns:
        List of caption dicts: [{text, start_ms, end_ms}, ...]
        Empty list if transcript is empty.
    """
    if not whisper_transcript:
        return []

    words_for_chunking = (
        _restore_punctuated_words(whisper_transcript, voice_script, proper_nouns)
        if voice_script else whisper_transcript
    )

    max_words = _MAX_WORDS_STANDARD
    chunks = _chunk_transcript(
        words_for_chunking,
        min_words=_MIN_WORDS_STANDARD,
        target_words=_TARGET_WORDS_STANDARD,
        max_words=max_words,
        max_duration_ms=_MAX_DURATION_MS,
    )
    captions = [
        {
            "text":     " ".join(w["word"] for w in chunk),
            "start_ms": chunk[0]["start_ms"],
            "end_ms":   chunk[-1]["end_ms"],
        }
        for chunk in chunks
    ]

    total_words       = sum(len(c["text"].split()) for c in captions)
    avg_words         = total_words / len(captions) if captions else 0.0
    max_caption_words = max((len(c["text"].split()) for c in captions), default=0)
    over_cap          = sum(1 for c in captions if len(c["text"].split()) > max_words)
    logger.info(
        "Standard subtitles: %d captions, avg %.1f words, max %d words, %d over cap",
        len(captions), avg_words, max_caption_words, over_cap,
    )
    return captions


def build_karaoke_subtitles(
    whisper_transcript: list[dict],
    active_color: str = _DEFAULT_KARAOKE_COLOR,
    voice_script: str = "",
    proper_nouns: list[str] | None = None,
) -> list[dict]:
    """Generate karaoke-style subtitle chunks from Whisper word timestamps.

    Each chunk contains individual word timings so Remotion can highlight the
    currently spoken word in ``active_color``.

    Args:
        whisper_transcript: Word-level Whisper output.
        active_color:       CSS hex color for the currently spoken word.
                            Defaults to #FFD700 (gold).
        voice_script:       Optional punctuated narrator script text (roadmap
                            Phase B2) — see ``build_standard_subtitles()`` for
                            the full contract. A merged multi-word unit (e.g.
                            an atomic restored number) highlights as one
                            karaoke word, not several.
        proper_nouns:       Optional recurring proper nouns for Whisper
                            mishearing correction — see
                            ``build_standard_subtitles()``.

    Returns:
        List of karaoke chunk dicts:
        [{words: [{w, s, e}, ...], start_ms, end_ms, active_color}, ...]
        Empty list if transcript is empty.
    """
    if not whisper_transcript:
        return []

    words_for_chunking = (
        _restore_punctuated_words(whisper_transcript, voice_script, proper_nouns)
        if voice_script else whisper_transcript
    )

    raw_chunks = _chunk_transcript(
        words_for_chunking,
        min_words=_MIN_WORDS_KARAOKE,
        target_words=_TARGET_WORDS_KARAOKE,
        max_words=_MAX_WORDS_KARAOKE,
        max_duration_ms=_MAX_DURATION_MS_KARAOKE,
    )
    chunks = [
        {
            "words":        [{"w": w["word"], "s": w["start_ms"], "e": w["end_ms"]} for w in chunk],
            "start_ms":     chunk[0]["start_ms"],
            "end_ms":       chunk[-1]["end_ms"],
            "active_color": active_color,
        }
        for chunk in raw_chunks
    ]

    avg_chunk_words = (sum(len(c["words"]) for c in chunks) / len(chunks)) if chunks else 0.0
    over_cap        = sum(1 for c in chunks if len(c["words"]) > _MAX_WORDS_KARAOKE)
    logger.debug(
        "Karaoke subtitles: %d chunks, avg %.1f words, %d over cap",
        len(chunks), avg_chunk_words, over_cap,
    )
    return chunks
