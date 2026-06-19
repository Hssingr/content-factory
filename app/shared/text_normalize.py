"""Text normalization utilities shared across agents.

Provides ``normalize_for_matching`` — used by Agent 3 audio normalization and Agent 4 visual
matching to make fuzzy
string comparison numeral-proof and language-aware.
"""

import re
import logging

logger = logging.getLogger(__name__)

# BCP-47 prefix → num2words language code
_LANG_MAP: dict[str, str] = {
    "fr": "fr",
    "en": "en",
    "de": "de",
    "it": "it",
    "es": "es",
    "pt": "pt",
    "nl": "nl",
    "pl": "pl",
}

_DIGIT_RE = re.compile(r"\b\d+\b")
_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def normalize_for_matching(text: str, language: str) -> list[str]:
    """Normalize text for fuzzy phrase matching across agents.

    Converts to lowercase, expands digit runs to words in the target language
    (using ``num2words``), strips punctuation, and splits on whitespace.

    Designed for matching Whisper transcripts (which contain spoken-form numbers)
    against Claude-returned phrases (which may contain digit-form numbers).

    Args:
        text:     Raw text to normalize (voice_script excerpt or Whisper word).
        language: BCP-47 language code (e.g. "fr", "en-US"). Only the primary
                  subtag is used for the num2words language lookup.

    Returns:
        List of lowercase, punctuation-free word tokens, with digit runs expanded
        to their spoken form. Empty tokens are excluded.

    Example::
        normalize_for_matching("In 1984, he was 24.", "en")
        # → ["in", "nineteen", "eighty", "four", "he", "was", "twenty", "four"]
    """
    lang_key = language.lower().split("-")[0]
    num2words_lang = _LANG_MAP.get(lang_key, "en")

    text = text.lower()
    text = _DIGIT_RE.sub(lambda m: _expand_number(m.group(), num2words_lang), text)
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return [t for t in text.split() if t]


def _expand_number(digit_str: str, lang: str) -> str:
    """Convert a digit string to its spoken form using num2words.

    Uses ``to='year'`` for 4-digit numbers in [1000, 2099] so that values like
    "1984" expand to "nineteen eighty four" — matching how speech-recognition
    engines (including Whisper) transcribe spoken years — instead of the cardinal
    form "one thousand nine hundred and eighty four".

    Falls back to the original string on any error (missing lang, overflow, etc.).

    Args:
        digit_str: String of digits (e.g. "1984" or "24").
        lang:      num2words language code.

    Returns:
        Spoken form (e.g. "nineteen eighty four") or original string on failure.
    """
    try:
        from num2words import num2words  # lazy import — not always installed
        n = int(digit_str)
        # Spoken-year form for dates in range [1000, 2099]
        if 1000 <= n <= 2099:
            return num2words(n, lang=lang, to="year")
        return num2words(n, lang=lang)
    except Exception:
        logger.debug("num2words expansion failed for %r lang=%s", digit_str, lang)
        return digit_str
