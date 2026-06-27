import logging
import re

from elevenlabs.types import VoiceSettings

from app.services.elevenlabs_client import get_client
from app.services.claude_client import call_claude

logger = logging.getLogger(__name__)

# ── Section marker regex ──────────────────────────────────────────────────────
# Matches [INTRO], [OUTRO], [SECTION N], [SECTION N: Title] on their own line.
# Used by prepare_script_for_tts (strip) and _chunk_script_at_sections (split).
_MARKER_RE = re.compile(
    r"^\s*\[(INTRO|OUTRO|SECTION[^\]]*)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Zero-width lookahead — splits at marker line positions without consuming them,
# so each chunk keeps its opening [INTRO] / [SECTION N] label.
_SECTION_SPLIT_RE = re.compile(
    r"(?=^\s*\[(?:INTRO|OUTRO|SECTION[^\]]*)\]\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# ── Punctuation-cleanup regexes ───────────────────────────────────────────────
# Collapses patterns that make TTS narration sound robotic:
# spaced/repeated ellipses ("... ...", ". . .", "....") and dashes/exclamations.
_ELLIPSIS_RUN_RE   = re.compile(r"\.(?:\s*\.){1,}")
_DASH_RUN_RE       = re.compile(r"(?:[—–-]\s*){2,}")
_PUNCT_REPEAT_RE   = re.compile(r"([!?])\1+")
_MULTI_SPACE_RE    = re.compile(r"[ \t]{2,}")
_BLANK_LINES_RE    = re.compile(r"\n{3,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")

# ── Sentence-length limiter ───────────────────────────────────────────────────
_LONG_SENTENCE_RE  = re.compile(r"(?<=[.!?])\s+")
_MAX_SENTENCE_WORDS = 18
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['][A-Za-z0-9]+)?")
_PAUSE_TAG_RE = re.compile(r"\[\s*dramatic\s+pause\s*\]", re.IGNORECASE)

# Natural split points inside a long sentence — tried in order of preference.
_SPLIT_CANDIDATES = re.compile(
    r"([;—])\s+|,\s+(?:and|but|so|because|although|while|when|which|who|that)\s+",
    re.IGNORECASE,
)

# ── TTS model character limits ────────────────────────────────────────────────
_MODEL_CHAR_LIMITS: dict[str, int] = {
    # ElevenLabs
    "eleven_v3":              4_500,
    "eleven_multilingual_v2": 9_500,
    "eleven_flash_v2_5":     39_000,
    # Cartesia
    "sonic-2":                9_500,
    "sonic-3":                9_500,
    "sonic-3.5":              9_500,
}

_ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"   # 44 100 Hz stereo, 128 kbps
_CARTESIA_OUTPUT_FORMAT: dict = {
    "container": "wav",
    "encoding": "pcm_s16le",
    "sample_rate": 44100,
}

# ── Cartesia speed mapping ─────────────────────────────────────────────────────
# Maps channel_voice.speed_profile → Cartesia _experimental_voice_controls speed string.
_CARTESIA_SPEED_MAP: dict[str, str] = {
    "slow":      "slow",
    "normal":    "normal",
    "fast":      "fast",
    "very_fast": "fastest",
}
_CARTESIA_NUMERIC_SPEED_MIN = 0.6
_CARTESIA_NUMERIC_SPEED_MAX = 1.5

# ── Cartesia emotion mapping ───────────────────────────────────────────────────
# Maps channel_voice.emotion → Cartesia _experimental_voice_controls emotion list.
# Format: "{emotion_name}:{level}" where level ∈ {lowest,low,medium,high,highest}.
# Supported emotion names: anger, positivity, surprise, sadness, curiosity.
_CARTESIA_EMOTION_MAP: dict[str, list[str]] = {
    "neutral":       [],
    "calm":          ["positivity:low"],
    "warm":          ["positivity:medium"],
    "authoritative": ["anger:low", "curiosity:low"],
    "enthusiastic":  ["positivity:high"],
    "dramatic":      ["sadness:low", "surprise:low"],
    "curious":       ["curiosity:medium"],
    "tense":         ["curiosity:high", "surprise:low"],
    "scared":        ["surprise:high", "sadness:medium"],
    "somber":        ["sadness:medium"],
}
_CARTESIA_SONIC3_EMOTION_MAP: dict[str, str] = {
    "neutral": "neutral",
    "calm": "calm",
    "warm": "content",
    "authoritative": "confident",
    "enthusiastic": "enthusiastic",
    "dramatic": "scared",
    "curious": "curious",
    "tense": "scared",
    "scared": "scared",
    "somber": "sad",
}
_CARTESIA_LEGACY_MODELS = {"sonic-2"}
_CARTESIA_GENERATION_CONFIG_MODELS = {"sonic-3", "sonic-3.5"}
_CLIMAX_SECTION_WORDS = {"reveal", "climax", "truth", "answer", "found", "discovered", "discovery", "horror", "vanished", "missing"}

# ── Emotion VoiceSettings presets ────────────────────────────────────────────
# stability        : 0–1  (higher = more consistent; lower = more expressive)
# similarity_boost : 0–1  (how closely to mimic the original voice clone)
# style            : 0–1  (style exaggeration; v2 models only)
# use_speaker_boost: improves clarity at slight latency cost
# speed            : reference value at "normal" speed_profile; overridden at
#                    runtime by _resolve_voice_settings() using speed_profile + delta.
_EMOTION_SETTINGS: dict[str, dict] = {
    "neutral":       {"stability": 0.75, "similarity_boost": 0.75, "style": 0.00, "use_speaker_boost": True, "speed": 0.97},
    "calm":          {"stability": 0.85, "similarity_boost": 0.80, "style": 0.00, "use_speaker_boost": True, "speed": 0.93},
    "warm":          {"stability": 0.65, "similarity_boost": 0.80, "style": 0.15, "use_speaker_boost": True, "speed": 0.97},
    "authoritative": {"stability": 0.80, "similarity_boost": 0.85, "style": 0.20, "use_speaker_boost": True, "speed": 0.93},
    "enthusiastic":  {"stability": 0.45, "similarity_boost": 0.75, "style": 0.45, "use_speaker_boost": True, "speed": 1.05},
    "dramatic":      {"stability": 0.30, "similarity_boost": 0.70, "style": 0.60, "use_speaker_boost": True, "speed": 0.92},
}
_DEFAULT_EMOTION = "neutral"

# ── Speed profile + emotion delta ─────────────────────────────────────────────
# Final speed = clamp(base + delta, 0.7, 1.2).
# speed_override on the channel_voice takes full precedence when set.
_SPEED_PROFILE_BASE: dict[str, float] = {
    "slow":      0.85,
    "normal":    0.97,
    "fast":      1.05,
    "very_fast": 1.12,
}
_EMOTION_SPEED_DELTA: dict[str, float] = {
    "neutral":        0.00,
    "calm":          -0.04,
    "warm":           0.00,
    "authoritative": -0.04,
    "enthusiastic":   0.08,
    "dramatic":      -0.05,
}

# eleven_v3 only: maps channel_voice.v3_stability_preset → stability float.
# NULL / unset defaults to "natural" (0.65). Ignored for other models.
_V3_STABILITY_PRESETS: dict[str, float] = {
    "creative": 0.30,
    "natural":  0.65,
    "robust":   0.85,
}


# ── Private helpers ───────────────────────────────────────────────────────────

def _collapse_dash_run(match: re.Match) -> str:
    """Collapse a repeated-dash run to one dash, keeping a trailing space if present."""
    return "— " if match.group(0)[-1].isspace() else "—"


def _split_long_sentence(sentence: str) -> str:
    """Break a sentence exceeding _MAX_SENTENCE_WORDS at its first natural split point.

    Tries semicolons, em-dashes, then comma + conjunction in that order.
    If no split point is found, returns the sentence unchanged.

    Args:
        sentence: Single sentence string.

    Returns:
        Original sentence, or two shorter sentences separated by a period and space.
    """
    words = sentence.split()
    if len(words) <= _MAX_SENTENCE_WORDS:
        return sentence

    match = _SPLIT_CANDIDATES.search(sentence)
    if not match:
        return sentence

    sep    = match.group(0)
    before = sentence[: match.start()].strip()
    after  = sentence[match.end():].strip()

    if not before or not after:
        return sentence

    after_cap = after[0].upper() + after[1:] if after else after
    connector = "." if sep.strip() in (";",) else ","
    return before + connector + " " + after_cap


_MAX_PAUSE_INSERTIONS = 6   # hard cap — never insert more than 6 pause markers per script
_CHUNK_BOUNDARY_SILENCE_SECONDS = 0.08

_REVEAL_SENTENCE_RE = re.compile(
    r"([\.\!\?]\s+)((?:That|Then|But|And then|Until|Except|Until then|"
    r"What they found|What he found|What she found|"
    r"The answer|The truth|The result|It turned out)\b)",
    re.IGNORECASE,
)
_REVEAL_DISCOVERY_RE = re.compile(
    r"\b("
    r"found|discovered|uncovered|revealed|learned|realized|realised|noticed|"
    r"recognized|recognised|identified|opened|heard|saw|recorded"
    r")\b",
    re.IGNORECASE,
)
_REVEAL_SECRET_RE = re.compile(
    r"\b("
    r"truth|secret|answer|explanation|proof|evidence|report|recording|tape|"
    r"photo|photograph|letter|message|drawer|envelope|file|name|voice|body|"
    r"door|room|house|key|lock|police"
    r")\b",
    re.IGNORECASE,
)
_REVEAL_REVERSAL_RE = re.compile(
    r"\b("
    r"was mine|were mine|my name|had never|never left|never been|"
    r"had been locked|locked from the outside|from the outside|inside the|"
    r"had been a lie|was a lie|was not|wasn't|weren't|could not have|"
    r"had already|was already|all along|the whole story"
    r")\b",
    re.IGNORECASE,
)
_REVEAL_CONSEQUENCE_RE = re.compile(
    r"\b("
    r"missing|vanished|dead|buried|hidden|locked|impossible|impossibly|"
    r"wrong|same|someone else|no one|nobody|nothing|every night"
    r")\b",
    re.IGNORECASE,
)

_PAUSE_MARKER_REVIEW_PROMPT_VERSION = "1.0"
_PAUSE_MARKER_REVIEW_SYSTEM_PROMPT = f"""
PROMPT_VERSION {_PAUSE_MARKER_REVIEW_PROMPT_VERSION}

You are reviewing pause-marker placement only.

Hard rules:
- Keep every word exactly the same.
- Do not rewrite the narration.
- Do not improve style, rhythm, clarity, or emotion.
- Do not add, remove, replace, reorder, or paraphrase any narration words.
- Do not change sentence order.
- Do not change meaning.
- Only remove, move, or adjust punctuation/pause markers when clearly misplaced.
- Pause markers include ellipses ("..."), em-dashes, bracketed dramatic-pause tags,
  or similar punctuation-only pause cues.
- Return only the corrected narration text.
""".strip()


def _candidate_sentence_from_reveal_match(match: re.Match) -> str:
    """Return the full candidate sentence without consuming it in the regex."""
    remainder = match.string[match.start(2):]
    sentence_end = re.search(r"[.!?](?:\s|$)", remainder)
    if sentence_end:
        return remainder[: sentence_end.end()].strip()
    return remainder.strip()


def _is_reveal_beat_sentence(sentence: str) -> bool:
    """Return True when a trigger sentence contains concrete reveal evidence."""
    cleaned = sentence.strip()
    if not cleaned:
        return False

    score = 0
    if _REVEAL_DISCOVERY_RE.search(cleaned):
        score += 1
    if _REVEAL_SECRET_RE.search(cleaned):
        score += 1
    if _REVEAL_REVERSAL_RE.search(cleaned):
        score += 2
    if _REVEAL_CONSEQUENCE_RE.search(cleaned):
        score += 1

    return score >= 2


def _apply_pacing_markers(
    text: str,
    tone: str,
    tts_model: str = "sonic-2",
    is_short_episode: bool = False,
) -> str:
    """Add strategic pacing cues for optimal TTS delivery — model-aware.

    Model branching:
      - ``eleven_v3``:       inserts ``[dramatic pause]`` tags (native v3 syntax,
                             interpreted by ElevenLabs as an expressive pause).
      - all other models:    inserts ``"..."`` ellipsis (standard TTS pause cue,
                             works for ElevenLabs v2/flash and Cartesia sonic-2).

    Pause budget:
      - Long-form content: capped at ``_MAX_PAUSE_INSERTIONS`` (6).
      - Short episodes:    capped at 10 — denser content benefits from more pacing
        cues within a 60–90 s window.

    Applied to ALL tones:
      - A sentence opening with a narrative reveal phrase (That / Then / But /
        And then / The truth / It turned out …) is only a *candidate* — matching
        the opener alone is not sufficient (Phase 11.5). The candidate sentence
        is additionally scored by ``_is_reveal_beat_sentence()`` against four
        deterministic evidence categories — discovery verbs, secret/evidence
        nouns, reversal phrases (weighted x2), and consequence words — and a
        pause marker is inserted only when the combined score is >= 2. This
        gate exists because the opener alone over-triggered on ordinary
        sentences that merely continue the narrative ("Then she walked into
        the room") rather than deliver an actual reveal — the gate requires
        concrete reveal evidence in the same sentence, not just a transition word.

    Applied only to dramatic / suspense / horror / tense tones (long-form only):
      - ellipsis path: replace the first short sentence's terminal period with ``"..."``.
      - v3 path: prepend ``[dramatic pause]`` before the first content sentence.
      - Skipped for Short episodes — Shorts need immediate energy, not a slow open.

    Args:
        text:             Marker-stripped, punctuation-normalized narration text.
        tone:             Channel tone / emotion label (case-insensitive).
        tts_model:        TTS model ID — ``eleven_v3`` uses audio tags; all others use ellipsis.
        is_short_episode: When ``True``, raise the pause cap to 10 and skip the
                          slow-open dramatic pause on the first sentence.

    Returns:
        Text with model-appropriate pacing cues inserted.
    """
    max_pause = 10 if is_short_episode else _MAX_PAUSE_INSERTIONS
    use_v3 = tts_model == "eleven_v3"
    insertion_count = 0

    def _insert_pause(match: re.Match) -> str:
        nonlocal insertion_count
        if insertion_count >= max_pause:
            return match.group(0)
        if not _is_reveal_beat_sentence(_candidate_sentence_from_reveal_match(match)):
            return match.group(0)
        insertion_count += 1
        if use_v3:
            return match.group(1) + "[dramatic pause] " + match.group(2)
        return match.group(1) + "... " + match.group(2)

    text = _REVEAL_SENTENCE_RE.sub(_insert_pause, text)

    # Slow-open for high-drama tones — skipped for Short episodes (immediate energy needed)
    if (
        not is_short_episode
        and tone.lower() in {"horror", "suspense", "dramatic", "tense"}
        and insertion_count < max_pause
    ):
        if use_v3:
            text = "[dramatic pause] " + text.lstrip()
            insertion_count += 1
        else:
            first_end = re.search(r"(?<=[a-z])\.\s", text)
            if first_end and len(text[: first_end.start()].split()) <= 12:
                text = text[: first_end.start()] + "..." + text[first_end.end() - 1:]
                insertion_count += 1

    return text


def _narration_word_sequence(text: str) -> list[str]:
    """Return narration words only, ignoring pause-marker syntax."""
    without_pause_tags = _PAUSE_TAG_RE.sub(" ", text)
    return _WORD_TOKEN_RE.findall(without_pause_tags)


def _has_same_narration_words(before: str, after: str) -> bool:
    """True only when the reviewed text keeps the exact narration word sequence."""
    return _narration_word_sequence(before) == _narration_word_sequence(after)


def _review_pause_marker_placement(text: str) -> str:
    """Optionally let Haiku adjust pause punctuation, guarded by a word-sequence check."""
    if not text.strip():
        return text

    try:
        reviewed = call_claude(
            _PAUSE_MARKER_REVIEW_SYSTEM_PROMPT,
            text,
            max_tokens=2048,
            task="pause_marker_review",
        )
    except Exception as exc:
        logger.warning(
            "TTS_PAUSE_REVIEW_FALLBACK: Claude review failed; using deterministic text. error=%s",
            type(exc).__name__,
        )
        return text

    if not isinstance(reviewed, str) or not reviewed.strip():
        logger.warning(
            "TTS_PAUSE_REVIEW_FALLBACK: invalid Claude review output; using deterministic text."
        )
        return text

    reviewed = reviewed.strip()
    if not _has_same_narration_words(text, reviewed):
        logger.warning(
            "TTS_PAUSE_REVIEW_FALLBACK: Claude review changed narration words; using deterministic text."
        )
        return text

    if reviewed != text:
        logger.info(
            "TTS_PAUSE_REVIEW_ACCEPTED: punctuation-only pause-marker adjustment accepted."
        )
    return reviewed


def _normalize_voice_script(text: str) -> str:
    """Clean up narration text so it reads naturally when spoken by TTS.

    Collapses excessive ellipses and repeated dashes/exclamations into a single
    natural pause marker, removes duplicate blank lines, and trims trailing
    whitespace — all before the script reaches ElevenLabs.

    Args:
        text: Marker-stripped narrator text.

    Returns:
        Normalized narrator text, ready for TTS.
    """
    text = _ELLIPSIS_RUN_RE.sub("...", text)
    text = _DASH_RUN_RE.sub(_collapse_dash_run, text)
    text = _PUNCT_REPEAT_RE.sub(r"\1", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _TRAILING_SPACE_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _chunk_script_at_sections(text: str, max_chars: int) -> list[str]:
    """Split voice_script into chunks of ≤ max_chars at [SECTION N] boundaries.

    Never cuts inside a section — only at [INTRO] / [SECTION N] / [OUTRO] marker
    lines. Each chunk retains its opening marker so prepare_script_for_tts can
    strip it before sending to ElevenLabs.

    Returns [text] unchanged when the full text fits within max_chars.

    Args:
        text:      Raw voice_script (may contain section markers).
        max_chars: Maximum character count per chunk.

    Returns:
        List of one or more non-empty chunk strings.
    """
    if len(text) <= max_chars:
        return [text]

    segments = [s for s in _SECTION_SPLIT_RE.split(text) if s.strip()]
    if not segments:
        return [text]

    chunks:  list[str] = []
    current: str = ""
    for seg in segments:
        if current and len(current) + len(seg) > max_chars:
            chunks.append(current.rstrip())
            current = seg
        else:
            current += seg
    if current.strip():
        chunks.append(current.rstrip())

    return chunks or [text]


def _parse_section_context(section_text: str) -> dict:
    """Extract section metadata from a section-marked narration chunk."""
    match = _MARKER_RE.search(section_text)
    if not match:
        return {"section_type": None, "section_index": None, "section_label": None, "section_title": None}

    label = match.group(1).strip()
    label_upper = label.upper()
    if label_upper == "INTRO":
        return {"section_type": "intro", "section_index": None, "section_label": label, "section_title": None}
    if label_upper == "OUTRO":
        return {"section_type": "outro", "section_index": None, "section_label": label, "section_title": None}

    index_match = re.search(r"SECTION\s+(\d+)", label, re.IGNORECASE)
    title_match = re.search(r":\s*(.+)$", label)
    return {
        "section_type": "body",
        "section_index": int(index_match.group(1)) if index_match else None,
        "section_label": label,
        "section_title": title_match.group(1).strip() if title_match else None,
    }


def _split_script_into_section_units(text: str) -> list[dict]:
    """Return one TTS unit per script section when section markers exist."""
    segments = [s for s in _SECTION_SPLIT_RE.split(text) if s.strip()]
    if not segments:
        return [{"text": text, **_parse_section_context(text)}]
    return [{"text": segment.rstrip(), **_parse_section_context(segment)} for segment in segments]


def _select_section_delivery(section_context: dict, channel_emotion: str, channel_speed_profile: str, *, is_short_episode: bool) -> dict:
    """Choose deterministic section-level Cartesia delivery, with safe channel fallback."""
    fallback = {
        "emotion": channel_emotion,
        "speed_profile": channel_speed_profile,
        "source": "fallback",
        "reason": "section_metadata_missing",
    }
    section_type = section_context.get("section_type")
    if is_short_episode:
        fallback["reason"] = "short_episode_static_policy"
        return fallback
    if not section_type:
        return fallback

    if section_type == "intro":
        return {"emotion": "curious", "speed_profile": "slow", "source": "section", "reason": "intro"}
    if section_type == "outro":
        return {"emotion": "somber", "speed_profile": "slow", "source": "section", "reason": "outro"}
    if section_type == "body":
        title = (section_context.get("section_title") or "").lower()
        title_words = set(re.findall(r"[a-z]+", title))
        if title_words & _CLIMAX_SECTION_WORDS:
            return {"emotion": "scared", "speed_profile": "fast", "source": "section", "reason": "climax_title"}
        index = section_context.get("section_index")
        if index is not None and index <= 2:
            return {"emotion": "tense", "speed_profile": "normal", "source": "section", "reason": "early_buildup"}
        return {"emotion": "tense", "speed_profile": "fast", "source": "section", "reason": "late_buildup"}

    fallback["reason"] = "section_type_unknown"
    return fallback


def _log_section_delivery(section_context: dict, delivery: dict, channel_emotion: str, *, is_short_episode: bool) -> None:
    label = section_context.get("section_label") or "unmarked"
    if delivery["source"] == "section":
        logger.info(
            "TTS_SECTION_DELIVERY_SELECTED: section=%s reason=%s emotion=%s speed_profile=%s base_emotion=%s",
            label, delivery["reason"], delivery["emotion"], delivery["speed_profile"], channel_emotion,
        )
        return
    logger.warning(
        "TTS_SECTION_DELIVERY_FALLBACK: section=%s reason=%s emotion=%s speed_profile=%s is_short_episode=%s",
        label, delivery["reason"], delivery["emotion"], delivery["speed_profile"], is_short_episode,
    )


def _cartesia_model_generation(tts_model: str) -> str:
    """Return the supported Cartesia request generation for a model id."""
    if tts_model in _CARTESIA_LEGACY_MODELS:
        return "legacy"
    if tts_model in _CARTESIA_GENERATION_CONFIG_MODELS:
        return "generation_config"
    logger.error(
        "CARTESIA_UNSUPPORTED_MODEL: model=%s is not mapped to a known request format",
        tts_model,
    )
    raise ValueError(f"Unsupported Cartesia TTS model for request formatting: {tts_model}")


def _resolve_cartesia_numeric_speed(channel_voice, speed_profile: str | None = None) -> float:
    """Resolve Sonic 3/3.5 numeric speed, preserving explicit voice overrides."""
    if getattr(channel_voice, "speed_override", None) is not None:
        raw_speed = float(channel_voice.speed_override)
    else:
        profile = speed_profile or getattr(channel_voice, "speed_profile", None) or "normal"
        raw_speed = _SPEED_PROFILE_BASE.get(profile, _SPEED_PROFILE_BASE["normal"])
    return max(_CARTESIA_NUMERIC_SPEED_MIN, min(_CARTESIA_NUMERIC_SPEED_MAX, raw_speed))


def _resolve_cartesia_sonic3_emotion(emotion: str) -> str:
    """Map project emotion labels to Cartesia Sonic 3/3.5 single emotion values."""
    emotion_key = (emotion or _DEFAULT_EMOTION).lower()
    return _CARTESIA_SONIC3_EMOTION_MAP.get(emotion_key, "neutral")


def _resolve_cartesia_pronunciation_dict_id(channel_voice) -> str | None:
    """Return an optional configured Cartesia pronunciation dictionary id."""
    value = getattr(channel_voice, "cartesia_pronunciation_dict_id", None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _build_cartesia_tts_kwargs(
    *,
    tts_model: str,
    transcript: str,
    voice_id: str,
    channel_voice,
    emotion: str,
    speed_profile: str | None = None,
) -> dict:
    """Build Cartesia SDK kwargs with model-generation-aware request formatting."""
    generation = _cartesia_model_generation(tts_model)
    if generation == "legacy":
        profile = speed_profile or getattr(channel_voice, "speed_profile", None) or "normal"
        speed_str = _CARTESIA_SPEED_MAP.get(profile, "normal")
        emotion_key = (emotion or _DEFAULT_EMOTION).lower()
        if emotion_key not in _CARTESIA_EMOTION_MAP:
            emotion_key = _DEFAULT_EMOTION
        return {
            "model_id": tts_model,
            "transcript": transcript,
            "voice_id": voice_id,
            "output_format": _CARTESIA_OUTPUT_FORMAT,
            "_experimental_voice_controls": {
                "speed": speed_str,
                "emotion": _CARTESIA_EMOTION_MAP[emotion_key],
            },
        }

    payload = {
        "model_id": tts_model,
        "transcript": transcript,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": _CARTESIA_OUTPUT_FORMAT,
        "generation_config": {
            "speed": _resolve_cartesia_numeric_speed(channel_voice, speed_profile),
            "emotion": _resolve_cartesia_sonic3_emotion(emotion),
        },
    }
    pronunciation_dict_id = _resolve_cartesia_pronunciation_dict_id(channel_voice)
    if pronunciation_dict_id:
        payload["pronunciation_dict_id"] = pronunciation_dict_id
    return payload


def _resolve_voice_settings(channel_voice) -> dict:
    """Build ElevenLabs VoiceSettings kwargs from a ChannelVoice ORM object.

    Precedence (highest wins):
      1. Per-field overrides: stability_override, similarity_override, style_override,
         speed_override — stored on the channel_voice record.
      2. v3_stability_preset → stability float (eleven_v3 only; NULL → 0.65).
      3. speed_profile base + emotion speed delta, clamped to [0.7, 1.2].
      4. Emotion preset defaults from _EMOTION_SETTINGS.

    Args:
        channel_voice: ChannelVoice ORM instance.

    Returns:
        Dict with keys stability, similarity_boost, style, use_speaker_boost, speed
        — suitable for ``VoiceSettings(**result)``.
    """
    emotion = (getattr(channel_voice, "emotion", None) or _DEFAULT_EMOTION).lower()
    if emotion not in _EMOTION_SETTINGS:
        emotion = _DEFAULT_EMOTION

    settings = dict(_EMOTION_SETTINGS[emotion])

    # Stability
    if getattr(channel_voice, "stability_override", None) is not None:
        settings["stability"] = float(channel_voice.stability_override)
    elif (
        getattr(channel_voice, "tts_model", "") == "eleven_v3"
        and getattr(channel_voice, "v3_stability_preset", None)
    ):
        settings["stability"] = _V3_STABILITY_PRESETS.get(
            channel_voice.v3_stability_preset, 0.65
        )

    # Similarity boost
    if getattr(channel_voice, "similarity_override", None) is not None:
        settings["similarity_boost"] = float(channel_voice.similarity_override)

    # Style
    if getattr(channel_voice, "style_override", None) is not None:
        settings["style"] = float(channel_voice.style_override)

    # Speaker boost
    settings["use_speaker_boost"] = bool(getattr(channel_voice, "use_speaker_boost", True))

    # Speed — speed_override takes full precedence; otherwise profile + emotion delta
    if getattr(channel_voice, "speed_override", None) is not None:
        settings["speed"] = max(0.7, min(1.2, float(channel_voice.speed_override)))
    else:
        profile = getattr(channel_voice, "speed_profile", None) or "normal"
        base    = _SPEED_PROFILE_BASE.get(profile, 0.97)
        delta   = _EMOTION_SPEED_DELTA.get(emotion, 0.0)
        settings["speed"] = max(0.7, min(1.2, base + delta))

    return settings


# ── Public functions ──────────────────────────────────────────────────────────

def prepare_script_for_tts(
    voice_script: str,
    language: str,
    tone: str,
    tts_model: str = "sonic-2",
    is_short_episode: bool = False,
) -> str:
    """Prepare a voice script for optimal TTS delivery without altering stored content.

    Strips section markers, normalizes punctuation, splits unnaturally long sentences,
    and applies model-aware pacing cues — all before the text reaches the TTS provider.
    The stored voice_script in the DB is never modified.

    Args:
        voice_script:     Full narrator text (may include [INTRO]/[SECTION N]/[OUTRO] markers).
        language:         BCP-47 language code (e.g. "fr", "en") — reserved for future
                          language-specific normalization rules.
        tone:             Emotion label from channel_voices (e.g. "dramatic", "calm").
        tts_model:        TTS model ID — ``eleven_v3`` uses ``[dramatic pause]`` audio tags;
                          all other models (including Cartesia sonic-2) use ``"..."`` ellipsis.
        is_short_episode: When ``True``, applies TikTok-optimised pacing: raises the pause
                          insertion cap to 10 and skips the slow-open dramatic pause so the
                          narration starts with immediate energy.

    Returns:
        TTS-ready text with markers stripped, punctuation normalized, long sentences
        split, and model-appropriate pacing cues applied.
    """
    # 1. Strip timing markers (they are read aloud if kept)
    text = _MARKER_RE.sub("", voice_script)

    # 2. Normalize excessive punctuation runs
    text = _normalize_voice_script(text)

    # 3. Split long sentences at natural breakpoints
    sentences = _LONG_SENTENCE_RE.split(text)
    fixed     = 0
    shortened = []
    for sent in sentences:
        result = _split_long_sentence(sent.strip())
        if result != sent.strip():
            fixed += 1
        shortened.append(result)
    text = " ".join(shortened)

    # 4. Pacing cues — reveal pauses for all tones, slow-open for long-form dramatic tones
    text = _apply_pacing_markers(text, tone=tone or "", tts_model=tts_model, is_short_episode=is_short_episode)

    # 5. Optional Haiku review; Python accepts only punctuation/pause-marker-only changes
    text = _review_pause_marker_placement(text)

    # 6. Diagnostic log
    original_words = len(voice_script.split())
    prepared_words = len(text.split())
    sentences_out  = _LONG_SENTENCE_RE.split(text)
    avg_sent_len   = (
        sum(len(s.split()) for s in sentences_out) / len(sentences_out)
        if sentences_out else 0.0
    )
    pause_points = text.count("...")
    logger.debug(
        "TTS prepare: language=%s tone=%s is_short_episode=%s "
        "original_words=%d prepared_words=%d "
        "avg_sentence_len=%.1f long_sentences_fixed=%d pause_points=%d",
        language, tone or "none", is_short_episode,
        original_words, prepared_words,
        avg_sent_len, fixed, pause_points,
    )

    return text


def _concat_mp3_chunks(chunk_bytes_list: list[bytes]) -> bytes:
    """Concatenate multiple mp3 byte blobs into one normalized mp3 stream via ffmpeg.

    Uses ffmpeg's concat demuxer with re-encoding at 192 kbps to avoid click
    artifacts from raw byte concatenation. Inserts a tiny deterministic silence
    pad between chunks to avoid abrupt section-to-section joins.
    Falls back to raw concat if ffmpeg is unavailable (no Remotion environment).

    Args:
        chunk_bytes_list: One or more raw mp3 byte blobs (one per TTS chunk).

    Returns:
        Single mp3 blob at 192 kbps.
    """
    if len(chunk_bytes_list) == 1:
        return chunk_bytes_list[0]

    import subprocess
    import tempfile
    import os

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.warning(
            "ffmpeg not found — using raw byte concat (may have click artifacts)."
        )
        return b"".join(chunk_bytes_list)

    with tempfile.TemporaryDirectory() as tmp:
        # Write each chunk to a temp file
        input_paths: list[str] = []
        for i, blob in enumerate(chunk_bytes_list):
            p = os.path.join(tmp, f"chunk_{i:03d}.mp3")
            with open(p, "wb") as f:
                f.write(blob)
            input_paths.append(p)

        # Build ffmpeg concat list file with a short generated pause between chunks.
        silence_path = os.path.join(tmp, "boundary_silence.mp3")
        silence_cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=mono",
            "-t", f"{_CHUNK_BOUNDARY_SILENCE_SECONDS:.3f}",
            "-c:a", "libmp3lame", "-b:a", "192k",
            silence_path,
        ]
        silence_proc = subprocess.run(silence_cmd, capture_output=True)
        if silence_proc.returncode != 0:
            logger.warning(
                "ffmpeg silence pad generation failed (rc=%d) — using raw byte concat. stderr: %s",
                silence_proc.returncode, silence_proc.stderr[-200:].decode(errors="replace"),
            )
            return b"".join(chunk_bytes_list)

        list_file = os.path.join(tmp, "concat.txt")
        with open(list_file, "w") as f:
            for i, p in enumerate(input_paths):
                f.write(f"file '{p}'\n")
                if i < len(input_paths) - 1:
                    f.write(f"file '{silence_path}'\n")

        out_file = os.path.join(tmp, "combined.mp3")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c:a", "libmp3lame", "-b:a", "192k",
            out_file,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            logger.warning(
                "ffmpeg concat failed (rc=%d) — using raw byte concat. stderr: %s",
                proc.returncode, proc.stderr[-200:].decode(errors="replace"),
            )
            return b"".join(chunk_bytes_list)

        result = open(out_file, "rb").read()
        logger.debug(
            "Concat: ffmpeg re-encode %d chunks with %.0f ms boundary silence → %d bytes (%.1f KB)",
            len(chunk_bytes_list), _CHUNK_BOUNDARY_SILENCE_SECONDS * 1000,
            len(result), len(result) / 1024,
        )
        return result


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Convert raw WAV bytes to MP3 bytes at 128 kbps using ffmpeg.

    Args:
        wav_bytes: Raw WAV audio bytes (pcm_s16le expected from Cartesia).

    Returns:
        MP3 bytes at 128 kbps.

    Raises:
        RuntimeError: If ffmpeg is not available or encoding fails.
    """
    import subprocess
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "audio.wav")
        mp3_path = os.path.join(tmp, "audio.mp3")
        with open(wav_path, "wb") as f:
            f.write(wav_bytes)
        cmd = ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libmp3lame", "-b:a", "128k", mp3_path]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg WAV→MP3 failed (rc={proc.returncode}): "
                + proc.stderr[-300:].decode(errors="replace")
            )
        with open(mp3_path, "rb") as f:
            return f.read()


def _generate_cartesia_audio(voice_script: str, channel_voice, is_short_episode: bool = False) -> bytes:
    """Generate MP3 audio via Cartesia TTS (provider='cartesia').

    Sends long-form section-marked scripts as one Cartesia request per section
    so deterministic section delivery can vary across the narrative arc. Flat
    narration, including child shorts, remains a single static-delivery unit.
    Converts each WAV to MP3 via ffmpeg, then concatenates.

    Args:
        voice_script:     Full narrator text (may include section markers).
        channel_voice:    ChannelVoice ORM object — provides voice_id, tts_model,
                          speed_profile.
        is_short_episode: Passed through to ``prepare_script_for_tts`` for
                          TikTok-optimised pacing.

    Returns:
        Raw MP3 bytes ready to be written to disk.

    Raises:
        RuntimeError: If CARTESIA_API_KEY is not configured or a chunk fails.
    """
    from cartesia import Cartesia
    from app.config import settings

    tts_model = getattr(channel_voice, "tts_model", None) or "sonic-2"
    voice_id  = channel_voice.voice_id
    emotion   = (getattr(channel_voice, "emotion", None) or _DEFAULT_EMOTION).lower()
    _cartesia_model_generation(tts_model)

    section_units = _split_script_into_section_units(voice_script)
    client    = Cartesia(api_key=settings.cartesia_api_key)
    channel_speed_profile = getattr(channel_voice, "speed_profile", None) or "normal"

    prepared_units: list[dict] = []
    for unit in section_units:
        delivery = _select_section_delivery(
            unit, emotion, channel_speed_profile, is_short_episode=is_short_episode
        )
        _log_section_delivery(unit, delivery, emotion, is_short_episode=is_short_episode)
        p = prepare_script_for_tts(
            unit["text"], language="", tone=delivery["emotion"],
            tts_model=tts_model, is_short_episode=is_short_episode,
        )
        prepared_units.append({**unit, "prepared": p, "delivery": delivery})

    logger.debug(
        "Cartesia TTS: voice_id=%s model=%s request_generation=%s base_emotion=%s units=%d is_short_episode=%s pronunciation_dict=%s",
        voice_id, tts_model, _cartesia_model_generation(tts_model), emotion, len(prepared_units),
        is_short_episode, bool(_resolve_cartesia_pronunciation_dict_id(channel_voice)),
    )

    all_bytes: list[bytes] = []
    for i, unit in enumerate(prepared_units):
        prepared = unit["prepared"]
        if not prepared.strip():
            continue

        logger.debug(
            "Cartesia chunk %d/%d: section=%s words=%d chars=%d emotion=%s speed_profile=%s",
            i + 1, len(prepared_units), unit.get("section_label") or "unmarked",
            len(prepared.split()), len(prepared), unit["delivery"]["emotion"], unit["delivery"]["speed_profile"],
        )

        request_kwargs = _build_cartesia_tts_kwargs(
            tts_model=tts_model,
            transcript=prepared,
            voice_id=voice_id,
            channel_voice=channel_voice,
            emotion=unit["delivery"]["emotion"],
            speed_profile=unit["delivery"]["speed_profile"],
        )
        try:
            wav_bytes = client.tts.bytes(**request_kwargs)
        except TypeError:
            logger.exception(
                "CARTESIA_REQUEST_FORMAT_UNSUPPORTED: model=%s request_generation=%s",
                tts_model, _cartesia_model_generation(tts_model),
            )
            raise
        all_bytes.append(_wav_to_mp3(wav_bytes))

    audio_bytes = _concat_mp3_chunks(all_bytes) if all_bytes else b""
    logger.debug(
        "Cartesia TTS complete: %d chunk(s) → %d bytes (%.1f KB)",
        len(prepared_units), len(audio_bytes), len(audio_bytes) / 1024,
    )
    return audio_bytes


def generate_audio(voice_script: str, channel_voice, is_short_episode: bool = False) -> bytes:
    """Convert a voice script to MP3 audio via the configured TTS provider.

    Routes to Cartesia or ElevenLabs based on ``channel_voice.provider``.

    Cartesia path (provider="cartesia") — Phase 11.2-11.6:
      Long-form, section-marked scripts are split into one TTS unit per
      ``[INTRO]``/``[SECTION N]``/``[OUTRO]`` section (flat narration, including
      child shorts, is a single unit). Each unit's deterministic delivery
      (emotion + speed profile) is selected from section metadata alone —
      INTRO/early-body/climax-titled/OUTRO sections get different delivery;
      missing metadata falls back to the channel-level emotion/speed_profile.
      Each unit's text is run through ``prepare_script_for_tts()`` — which
      itself applies deterministic pacing (gated by reveal-beat evidence,
      Phase 11.5) followed by one optional Haiku pause-marker review pass
      that Python accepts only if it changes no narration words (Phase 11.2)
      — before being sent as its own Cartesia request. The request payload
      shape is chosen per ``tts_model``: the legacy shape for ``sonic-2``, or
      the ``generation_config`` shape (numeric speed, single emotion value,
      optional pronunciation dictionary id) for ``sonic-3``/``sonic-3.5``
      (Phase 11.3). WAV bytes from each unit are converted to MP3 via ffmpeg,
      then all units are concatenated with a short deterministic silence pad
      between them to avoid abrupt section-boundary joins (Phase 11.6).

    ElevenLabs path (provider="elevenlabs"):
      Model, VoiceSettings, and chunking (char-limit-based, not section-unit)
      derived from ``channel_voice``. Each chunk still passes through
      ``prepare_script_for_tts()``, so Phase 11.5's reveal-gated pacing and
      Phase 11.2's optional Haiku pause review apply here too — Phase 11.3's
      Cartesia request-shape branching and Phase 11.4's section-aware delivery
      selection do not, since this path has no Cartesia request to build and
      uses ElevenLabs's own native text-conditioning (``previous_text`` /
      ``next_text``) for cross-chunk continuity instead (skipped for
      ``eleven_v3`` — not supported). Final concatenation still goes through
      the same ffmpeg re-encode + silence-pad step as the Cartesia path
      (Phase 11.6) when more than one chunk exists.

    Args:
        voice_script:     Full narrator text (may include [INTRO]/[SECTION N]/[OUTRO]
                          markers — stripped per chunk before sending to the provider).
        channel_voice:    ChannelVoice ORM object — provides voice_id, provider,
                          tts_model, emotion, speed_profile, v3_stability_preset,
                          and per-field VoiceSettings overrides.
        is_short_episode: When ``True``, applies TikTok-optimised pacing in
                          ``prepare_script_for_tts`` (pause cap=10, no slow-open).

    Returns:
        Raw MP3 bytes ready to be written to disk.

    Raises:
        RuntimeError: If the required API key is not configured or a chunk fails.
    """
    provider = (getattr(channel_voice, "provider", None) or "cartesia").lower()

    if provider == "cartesia":
        return _generate_cartesia_audio(voice_script, channel_voice, is_short_episode=is_short_episode)

    # ── ElevenLabs path ───────────────────────────────────────────────────────
    model_id = getattr(channel_voice, "tts_model", None) or "eleven_multilingual_v2"
    voice_id = channel_voice.voice_id
    emotion  = (getattr(channel_voice, "emotion", None) or _DEFAULT_EMOTION).lower()
    if emotion not in _EMOTION_SETTINGS:
        emotion = _DEFAULT_EMOTION

    vs_dict        = _resolve_voice_settings(channel_voice)
    voice_settings = VoiceSettings(**vs_dict)

    max_chars = _MODEL_CHAR_LIMITS.get(model_id, 9_500)
    chunks    = _chunk_script_at_sections(voice_script, max_chars)
    client    = get_client()

    prepared_chunks: list[str] = []
    for chunk in chunks:
        p = prepare_script_for_tts(chunk, language="", tone=emotion, tts_model=model_id, is_short_episode=is_short_episode)
        prepared_chunks.append(p)

    # Detect text-conditioning support (available since SDK >= 2.x).
    # eleven_v3 does not accept previous_text / next_text — standalone chunks only.
    import inspect
    _convert_sig = inspect.signature(client.text_to_speech.convert)
    _supports_text_ctx = "previous_text" in _convert_sig.parameters
    _use_text_conditioning = _supports_text_ctx and model_id != "eleven_v3"
    if model_id == "eleven_v3":
        stitching_path = "none (eleven_v3 — standalone chunks)"
    elif _supports_text_ctx:
        stitching_path = "text-conditioning"
    else:
        stitching_path = "none (SDK too old)"
    logger.debug("ElevenLabs TTS stitching path: %s (%d chunk(s))", stitching_path, len(prepared_chunks))

    all_bytes: list[bytes] = []

    for i, prepared in enumerate(prepared_chunks):
        if not prepared.strip():
            continue

        logger.debug(
            "ElevenLabs TTS chunk %d/%d: voice_id=%s emotion=%s words=%d model=%s chars=%d",
            i + 1, len(prepared_chunks), voice_id, emotion,
            len(prepared.split()), model_id, len(prepared),
        )

        kwargs: dict = dict(
            text=prepared,
            voice_id=voice_id,
            model_id=model_id,
            output_format=_ELEVENLABS_OUTPUT_FORMAT,
            voice_settings=voice_settings,
        )

        if _use_text_conditioning and len(prepared_chunks) > 1:
            if i > 0 and prepared_chunks[i - 1].strip():
                kwargs["previous_text"] = prepared_chunks[i - 1][-300:]
            if i < len(prepared_chunks) - 1 and prepared_chunks[i + 1].strip():
                kwargs["next_text"] = prepared_chunks[i + 1][:300]

        audio_iter = client.text_to_speech.convert(**kwargs)
        all_bytes.append(b"".join(audio_iter))

    audio_bytes = _concat_mp3_chunks(all_bytes) if all_bytes else b""
    logger.debug(
        "ElevenLabs TTS complete: %d chunk(s) %d bytes (%.1f KB)",
        len(prepared_chunks), len(audio_bytes), len(audio_bytes) / 1024,
    )
    return audio_bytes
