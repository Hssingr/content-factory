import functools
import inspect
import logging
import re
import time

from elevenlabs.types import VoiceSettings

from app.services.elevenlabs_client import get_client

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
_WORD_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_PAUSE_TAG_RE = re.compile(r"\[\s*dramatic\s+pause\s*\]", re.IGNORECASE)
# Any ElevenLabs v3 audio tag ([whispers], [dramatic pause], [gasps], …).
# Used to keep the three independent tag sources from stacking on one
# sentence (fresh full-system audit §2.6 P-6): script-embedded tags
# (AUDIO_TAGS_INSTRUCTION, Agent 2), the per-section delivery prefix, and
# the deterministic pacing inserter each check for an existing tag before
# adding their own — the v3 contract is max one tag per sentence.
_ANY_V3_TAG_RE = re.compile(r"\[[a-z][a-z ]{2,24}\]", re.IGNORECASE)

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
_CLIMAX_SECTION_WORDS = {
    "reveal", "reveals", "revealed", "revealing",
    "expose", "exposes", "exposed", "exposing",
    "uncover", "uncovers", "uncovered", "uncovering",
    "climax", "truth", "answer", "found", "discovered", "discovery",
    "horror", "vanished", "missing",
}

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
    "curious":       {"stability": 0.55, "similarity_boost": 0.78, "style": 0.35, "use_speaker_boost": True, "speed": 0.93},
    "tense":         {"stability": 0.38, "similarity_boost": 0.74, "style": 0.58, "use_speaker_boost": True, "speed": 1.02},
    "scared":        {"stability": 0.28, "similarity_boost": 0.70, "style": 0.70, "use_speaker_boost": True, "speed": 1.08},
    "somber":        {"stability": 0.78, "similarity_boost": 0.82, "style": 0.22, "use_speaker_boost": True, "speed": 0.88},
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
    "curious":       -0.04,
    "tense":          0.04,
    "scared":         0.08,
    "somber":        -0.08,
}

# eleven_v3 only: maps channel_voice.v3_stability_preset → stability float.
# NULL / unset defaults to "natural" (0.65). Ignored for other models.
_V3_STABILITY_PRESETS: dict[str, float] = {
    "creative": 0.30,
    "natural":  0.65,
    "robust":   0.85,
}

_ELEVENLABS_V3_DELIVERY_TAGS: dict[str, str] = {
    "intro": "[whispers]",
    "early_buildup": "[whispers]",
    "late_buildup": "[dramatic pause]",
    "climax_title": "[gasps]",
    "outro": "[sighs]",
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
    words = _WORD_TOKEN_RE.findall(sentence)
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

# Loudness normalization targets (roadmap Phase A3, operator video-output
# audit): a real production run measured a parent's EN narration at -22.4
# LUFS against the SAME content's FR narration at -18.3 LUFS — no
# normalization existed anywhere in this pipeline, so loudness varied by
# provider/voice/language with nothing correcting it. -15 LUFS matches
# short-form platform norms (TikTok/Reels/Shorts loudness-normalize close
# to this) — independent review measurement of real output at the prior
# -17.0 target (code_report/8abd7fea_independent_video_output_review.md,
# finding 8) confirmed the target was hit accurately but felt quiet for
# short-form delivery; -1.0 dBTP still leaves real headroom against
# inter-sample peaks after mp3 re-encoding.
_LOUDNORM_TARGET_I_LUFS  = -15.0
_LOUDNORM_TARGET_TP_DBTP = -1.0
_LOUDNORM_TARGET_LRA_LU  = 11.0  # ffmpeg loudnorm's own default LRA target

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
      - v3 path: prepend ``[dramatic pause]`` before the first content sentence —
        UNLESS the section text already carries an authored v3 tag anywhere
        (pre-next-test roadmap Tier 1 R3): a section whose script embeds its own
        AUDIO_TAGS_INSTRUCTION tag already has intentional pacing, and the generic
        slow-open must not stack a third tag source onto it (a real run measured
        31% of the runtime as silence with authored tags + slow-opens + the
        per-section delivery prefix all contributing pauses to the same section).
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
    # Tier 1 R3: captured at entry, before any pacing insertion — only tags
    # authored into the script itself (AUDIO_TAGS_INSTRUCTION) count, never
    # tags this function inserts below.
    has_authored_v3_tag = use_v3 and bool(_ANY_V3_TAG_RE.search(text))
    insertion_count = 0

    def _insert_pause(match: re.Match) -> str:
        nonlocal insertion_count
        if insertion_count >= max_pause:
            return match.group(0)
        candidate_sentence = _candidate_sentence_from_reveal_match(match)
        if not _is_reveal_beat_sentence(candidate_sentence):
            return match.group(0)
        # Never stack a second v3 tag onto a sentence that already carries one
        # (a script-embedded AUDIO_TAGS_INSTRUCTION tag, or the per-section
        # delivery prefix) — v3's own contract is max one tag per sentence.
        if use_v3 and _ANY_V3_TAG_RE.search(candidate_sentence):
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
            # Skip the slow-open tag when the section already carries an
            # authored v3 tag anywhere (Tier 1 R3 — its pacing is already
            # intentional), or when the text opens on a v3 tag
            # (script-embedded or section-delivery) — never two tags together.
            if not has_authored_v3_tag and not _ANY_V3_TAG_RE.match(text.lstrip()):
                text = "[dramatic pause] " + text.lstrip()
                insertion_count += 1
        else:
            first_end = re.search(r"(?<=[^\W_])\.\s", text, re.UNICODE)
            if first_end and len(_WORD_TOKEN_RE.findall(text[: first_end.start()])) <= 12:
                text = text[: first_end.start()] + "..." + text[first_end.end() - 1:]
                insertion_count += 1

    return text

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
    """Choose deterministic section-level TTS delivery, with safe channel fallback."""
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
        title = (section_context.get("section_title") or "").casefold()
        title_words = set(_WORD_TOKEN_RE.findall(title))
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


# ── Hotfix A — installed Cartesia SDK compatibility (no live API call) ────────
# Phase 11.3 added the generation_config request shape (`voice={...}`,
# `generation_config={...}`) for Sonic 3/3.5, alongside the pre-existing
# legacy shape (`voice_id=...`, `_experimental_voice_controls={...}`) for
# Sonic 2. That assumed the installed `cartesia` Python package's
# `TTS.bytes()` accepts both shapes. A real pipeline run surfaced
# ``TypeError: TTS.bytes() got an unexpected keyword argument 'voice'`` —
# the installed SDK version (1.3.1) only implements the OLDER signature
# (`model_id, transcript, output_format, voice_id, voice_embedding,
# duration, language, _experimental_voice_controls` — confirmed directly via
# `inspect.signature(TTS.bytes)`, no live call). `generation_config` and
# `voice=` do not exist on this SDK version at all, for any model.
#
# This check is pure local introspection of the already-imported package —
# it never makes a network/API call, so it can run safely even with no
# CARTESIA_API_KEY configured.
@functools.lru_cache(maxsize=1)
def _cartesia_sdk_supports_generation_config() -> bool:
    """True if the installed Cartesia SDK's ``TTS.bytes()`` accepts the
    Sonic 3/3.5 request shape (``voice=`` + ``generation_config=``).

    Cached for the process lifetime — the installed SDK version cannot
    change mid-process, so introspecting it more than once is wasted work.
    """
    try:
        from cartesia.tts import TTS
        params = inspect.signature(TTS.bytes).parameters
        return "voice" in params and "generation_config" in params
    except Exception:
        logger.warning(
            "CARTESIA_SDK_INTROSPECTION_FAILED — could not inspect the installed "
            "cartesia package's TTS.bytes() signature; assuming generation_config "
            "is NOT supported (fail-safe default)",
            exc_info=True,
        )
        return False


def _check_cartesia_sdk_compatibility(tts_model: str) -> str:
    """Validate the installed Cartesia SDK supports this model's request shape
    before any TTS work is attempted.

    Returns the request generation (``"legacy"`` or ``"generation_config"``)
    on success — the same value ``_cartesia_model_generation()`` returns, so
    callers can use this as a drop-in replacement for that call.

    Raises:
        RuntimeError: If ``tts_model`` requires the ``generation_config``
            request shape but the installed SDK does not support it. This
            replaces the previous failure mode — a raw, unactionable
            ``TypeError: TTS.bytes() got an unexpected keyword argument
            'voice'`` surfacing from deep inside the SDK call — with an
            explicit, actionable error raised BEFORE any chunk is processed
            or any API call is attempted. There is no safe legacy-shape
            fallback for a generation_config model: Cartesia's documented
            API contract for Sonic 3/3.5 requires the newer shape, and
            silently downgrading the request format for a model that may
            not accept the legacy shape cannot be verified without a live
            API call, which is not permitted here (CLAUDE.md §19.1) — failing
            loud is the safe choice, not a silent reformat.
    """
    generation = _cartesia_model_generation(tts_model)
    sdk_supports_generation_config = _cartesia_sdk_supports_generation_config()
    logger.info(
        "CARTESIA_SDK_COMPATIBILITY model=%s request_generation=%s "
        "sdk_supports_generation_config=%s",
        tts_model, generation, sdk_supports_generation_config,
    )

    if generation == "generation_config" and not sdk_supports_generation_config:
        logger.error(
            "CARTESIA_SDK_INCOMPATIBLE model=%s required_request_generation=generation_config "
            "sdk_supports_generation_config=False reason=installed_cartesia_sdk_predates_sonic3 "
            "action=upgrade_cartesia_package_or_use_a_sonic-2_voice",
            tts_model,
        )
        raise RuntimeError(
            f"Installed Cartesia SDK does not support the voice=/generation_config= "
            f"request shape required by model {tts_model!r}. This is a local SDK "
            f"version mismatch, not a Cartesia API/account issue. Fix by either: "
            f"(1) upgrading the 'cartesia' Python package (pip install -U cartesia) "
            f"so client.tts.bytes() accepts voice=/generation_config=, or "
            f"(2) switching this channel voice's tts_model to 'sonic-2' "
            f"(channel_voices.tts_model), which uses the legacy request shape this "
            f"SDK version already supports. Refusing to call the Cartesia API with a "
            f"request shape the installed SDK cannot send."
        )

    return generation


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


def _with_elevenlabs_v3_delivery_tag(text: str, delivery: dict) -> str:
    """Prefix one deterministic ElevenLabs v3 audio tag for section delivery.

    ``prepare_script_for_tts()`` already inserts reveal-gated ``[dramatic pause]``
    tags. This helper adds the per-section performance tag that used to be
    Cartesia-only: intro/early buildup whisper, late buildup pause, climax gasp,
    outro sigh. It never duplicates a tag when the prepared text already starts
    with that tag.
    """
    prepared = text.strip()
    if not prepared:
        return prepared
    tag = _ELEVENLABS_V3_DELIVERY_TAGS.get(str(delivery.get("reason") or ""))
    if not tag:
        return prepared
    # Never stack two tags on the opening sentence (v3 is max one tag per
    # sentence). A leading [dramatic pause] is the generic pacing slow-open —
    # the section delivery tag is the more intentional choice, so it replaces
    # it. Any OTHER leading tag (a script-embedded AUDIO_TAGS_INSTRUCTION tag)
    # wins and the delivery tag is skipped.
    leading_pause = _PAUSE_TAG_RE.match(prepared)
    if leading_pause:
        prepared = prepared[leading_pause.end():].lstrip()
    elif _ANY_V3_TAG_RE.match(prepared):
        return prepared
    return f"{tag} {prepared}"


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


def _resolve_voice_settings(
    channel_voice,
    emotion_override: str | None = None,
    speed_profile_override: str | None = None,
) -> dict:
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
    emotion = (emotion_override or getattr(channel_voice, "emotion", None) or _DEFAULT_EMOTION).lower()
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
        profile = speed_profile_override or getattr(channel_voice, "speed_profile", None) or "normal"
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

    # 5. Diagnostic log
    original_words = len(_WORD_TOKEN_RE.findall(voice_script))
    prepared_words = len(_WORD_TOKEN_RE.findall(text))
    sentences_out  = _LONG_SENTENCE_RE.split(text)
    avg_sent_len   = (
        sum(len(_WORD_TOKEN_RE.findall(s)) for s in sentences_out) / len(sentences_out)
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

    ffmpeg re-encoding is mandatory when there is more than one chunk (roadmap
    2a / audit P0-1, code_report/forensic_output_audit_borrasca_run.md): a
    prior raw-byte-concat fallback (used when ffmpeg was missing) produced an
    mp3 with multiple concatenated headers, which a header-reading duration
    measurement (mutagen) silently read as just the first chunk's length —
    161.7s stored for a real 616.8s file, corrupting every downstream
    timeline. A failed run is recoverable; a silently corrupting fallback is
    not — so every ffmpeg failure point here raises instead of falling back
    to raw concat.

    Args:
        chunk_bytes_list: One or more raw mp3 byte blobs (one per TTS chunk).

    Returns:
        Single mp3 blob at 192 kbps.

    Raises:
        RuntimeError: If ffmpeg is unavailable or any re-encode step fails.
    """
    if len(chunk_bytes_list) == 1:
        return chunk_bytes_list[0]

    import subprocess
    import tempfile
    import os

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "ffmpeg is not available — cannot re-encode "
            f"{len(chunk_bytes_list)} audio chunks. A raw byte-concat "
            "fallback is not used: it silently corrupts the resulting mp3's "
            "duration (roadmap 2a / audit P0-1)."
        ) from exc

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
            raise RuntimeError(
                f"ffmpeg silence pad generation failed (rc={silence_proc.returncode}): "
                f"{silence_proc.stderr[-200:].decode(errors='replace')}"
            )

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
            raise RuntimeError(
                f"ffmpeg concat failed (rc={proc.returncode}): "
                f"{proc.stderr[-200:].decode(errors='replace')}"
            )

        with open(out_file, "rb") as f:
            result = f.read()
        logger.debug(
            "Concat: ffmpeg re-encode %d chunks with %.0f ms boundary silence → %d bytes (%.1f KB)",
            len(chunk_bytes_list), _CHUNK_BOUNDARY_SILENCE_SECONDS * 1000,
            len(result), len(result) / 1024,
        )
        return result


def _normalize_loudness(mp3_bytes: bytes) -> bytes:
    """Normalize final narration audio to a consistent loudness via ffmpeg.

    A real production run measured a parent's EN narration at -22.4 LUFS
    against the SAME content's FR narration at -18.3 LUFS — no loudness
    normalization existed anywhere in this pipeline, so perceived volume
    varied by provider/voice/language with nothing correcting it.

    Single-pass ``loudnorm`` (one ffmpeg call) rather than the more precise
    two-pass measure-then-apply variant — a deliberate simplicity tradeoff:
    the goal is "consistent and reasonable across languages/providers," not
    broadcast-spec-exact loudness. ffmpeg is already a hard dependency of
    this pipeline (``_concat_mp3_chunks()``, ``_wav_to_mp3()``,
    ``measure_audio_duration_ms()``), so its absence here raises the same
    way those do rather than silently skipping normalization.

    Applied once, unconditionally, at the single point every provider path
    funnels through (``generate_audio()``'s return points below) — covers
    both single-chunk and multi-chunk audio, Cartesia and ElevenLabs alike.
    Never runs on the skip-on-disk resume path, since that path never reads
    the audio bytes back into memory at all (CLAUDE.md §10.3's
    ``RESUME_TRANSCRIPT_REUSED`` contract) — an already-generated file on
    disk was normalized once, at generation time, and is trusted thereafter.

    Args:
        mp3_bytes: Raw MP3 bytes for one language's complete narration.

    Returns:
        Loudness-normalized MP3 bytes at 192 kbps. Empty input returns empty
        output unchanged (nothing to normalize).

    Raises:
        RuntimeError: If ffmpeg is unavailable or the normalization pass fails.
    """
    if not mp3_bytes:
        return mp3_bytes

    import subprocess
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        in_path  = os.path.join(tmp, "in.mp3")
        out_path = os.path.join(tmp, "out.mp3")
        with open(in_path, "wb") as f:
            f.write(mp3_bytes)

        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-af", (
                f"loudnorm=I={_LOUDNORM_TARGET_I_LUFS}:"
                f"TP={_LOUDNORM_TARGET_TP_DBTP}:"
                f"LRA={_LOUDNORM_TARGET_LRA_LU}"
            ),
            "-c:a", "libmp3lame", "-b:a", "192k",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg loudnorm failed (rc={proc.returncode}): "
                f"{proc.stderr[-300:].decode(errors='replace')}"
            )

        with open(out_path, "rb") as f:
            result = f.read()

    logger.info(
        "AUDIO_LOUDNESS_NORMALIZED target_i=%.1fLUFS target_tp=%.1fdBTP "
        "input_bytes=%d output_bytes=%d",
        _LOUDNORM_TARGET_I_LUFS, _LOUDNORM_TARGET_TP_DBTP,
        len(mp3_bytes), len(result),
    )
    return result


_SILENCE_COMPRESS_THRESHOLD_DB = -40
_DRAMATIC_SILENCE_KEEP_COUNT = 2
_DRAMATIC_SILENCE_MAX_MS = 900
# TTS inter-utterance silence is near-digital (well below -60 dB); -40 dB
# detects it reliably while never clipping quiet speech or breath tails.

# Fraction of a non-dramatic gap's excess (above the cap) retained instead of
# trimmed away. Flattening every ordinary gap to exactly `cap_s` (the prior
# behavior) makes every capped pause bit-for-bit identical — an independent
# review of real output measured this as a mechanical, "assembled from
# chunks" rhythm (code_report/8abd7fea_independent_video_output_review.md,
# finding 1). Retaining a fraction of each gap's own original excess
# preserves natural variation between pauses while still bounding worst-case
# length; result is still clamped to the dramatic-tier ceiling so ordinary
# gaps never out-last the two deliberately-longer "dramatic" pauses.
_ORDINARY_SILENCE_EXCESS_RETENTION = 0.3


def _compress_interior_silence(mp3_bytes: bytes) -> bytes:
    """Cap interior narration silences at ``settings.audio_max_interior_silence_ms``
    (pre-next-test roadmap Tier 1 R1).

    A real production run measured 31% of a 12:40 video as near-digital
    silence — 219 gaps ≥0.5s, median 1.1s — the single largest viewer-facing
    audio defect ("narration assembled from isolated TTS sentences"). The
    stack that produces it (paragraph-per-sentence scripts × ElevenLabs v3
    per-utterance pauses × pacing-tag insertion) had no pause bound anywhere.

    Deterministic two-pass ffmpeg, no AI call:

    1. ``silencedetect`` pre-pass (read-only) counts silences at least as
       long as the cap. When none exist the input bytes are returned
       **unchanged** — no re-encode, no quality-generation loss, which is
       the common case for short chunks.
    2. A deterministic ``atrim``/``concat`` splice preserves the two longest
       non-leading gaps at up to 900 ms; every other qualifying gap retains
       the configured limit plus ``_ORDINARY_SILENCE_EXCESS_RETENTION`` of
       its own excess above that limit (bounded by the 900 ms dramatic
       ceiling) rather than being flattened to one identical duration — only
       the excess beyond that retained amount is removed.

    Applied per TTS chunk in ``generate_audio()``'s provider paths — BEFORE
    ``_concat_mp3_chunks()`` and BEFORE ``_compute_section_boundaries()`` —
    so the chunk durations that section boundaries are measured from are the
    compressed ones, and every downstream timeline (Whisper, beats,
    captions) derives from the same compressed audio. Never runs on the
    skip-on-disk resume path (same contract as ``_normalize_loudness()``):
    an on-disk file was compressed once, at generation time.

    Leading silence of a chunk is never trimmed; a chunk's trailing silence
    is treated as one more interior run
    and capped — after concatenation it sits between sections anyway.

    Args:
        mp3_bytes: Raw MP3 bytes for one TTS chunk.

    Returns:
        MP3 bytes with every interior silence capped, or the input object
        unchanged when compression is disabled (cap <= 0), the input is
        empty, or no qualifying silence exists.

    Raises:
        RuntimeError: If ffmpeg is unavailable or either pass fails.
    """
    if not mp3_bytes:
        return mp3_bytes

    from app.config import settings

    cap_ms = settings.audio_max_interior_silence_ms
    if cap_ms <= 0:
        return mp3_bytes
    cap_s = cap_ms / 1000.0

    import os
    import re as _re
    import subprocess
    import tempfile

    from app.agents.agent3_audio.services.storage import measure_audio_duration_ms
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "in.mp3")
        out_path = os.path.join(tmp, "out.mp3")
        with open(in_path, "wb") as f:
            f.write(mp3_bytes)

        # Pass 1 — read-only detection: is there anything to compress?
        detect_cmd = [
            "ffmpeg", "-i", in_path,
            "-af", (
                f"silencedetect=noise={_SILENCE_COMPRESS_THRESHOLD_DB}dB:"
                f"d={cap_s:.3f}"
            ),
            "-f", "null", "-",
        ]
        detect_proc = subprocess.run(detect_cmd, capture_output=True)
        if detect_proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg silencedetect failed (rc={detect_proc.returncode}): "
                f"{detect_proc.stderr[-300:].decode(errors='replace')}"
            )
        interval_matches = list(_re.finditer(
            rb"silence_start:\s*([0-9.]+).*?"
            rb"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)",
            detect_proc.stderr,
            _re.DOTALL,
        ))
        qualifying = len(interval_matches)
        if qualifying == 0:
            return mp3_bytes

        in_ms = measure_audio_duration_ms(Path(in_path))
        intervals = [
            (float(match.group(1)), float(match.group(2)), float(match.group(3)))
            for match in interval_matches
        ]
        # Preserve leading silence exactly. Of the remaining runs, the two
        # longest get a deliberate 900 ms ceiling; every other run keeps the
        # configured continuity cap.
        eligible = [i for i, (start, _end, _duration) in enumerate(intervals) if start > 0.001]
        dramatic = set(sorted(
            eligible, key=lambda i: intervals[i][2], reverse=True,
        )[:_DRAMATIC_SILENCE_KEEP_COUNT])

        removals: list[tuple[float, float]] = []
        for i in eligible:
            start, end, duration = intervals[i]
            if i in dramatic:
                keep_s = min(duration, _DRAMATIC_SILENCE_MAX_MS / 1000.0)
            else:
                excess = max(0.0, duration - cap_s)
                keep_s = min(
                    duration,
                    cap_s + excess * _ORDINARY_SILENCE_EXCESS_RETENTION,
                    _DRAMATIC_SILENCE_MAX_MS / 1000.0,
                )
            remove_start = start + keep_s
            if end - remove_start > 0.001:
                removals.append((remove_start, end))
        if not removals:
            return mp3_bytes

        total_s = in_ms / 1000.0
        keep_ranges: list[tuple[float, float]] = []
        cursor = 0.0
        for remove_start, remove_end in removals:
            if remove_start > cursor:
                keep_ranges.append((cursor, min(remove_start, total_s)))
            cursor = max(cursor, remove_end)
        if cursor < total_s:
            keep_ranges.append((cursor, total_s))

        filters = [
            f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]"
            for i, (start, end) in enumerate(keep_ranges)
            if end > start
        ]
        if not filters:
            return mp3_bytes
        labels = "".join(f"[a{i}]" for i in range(len(filters)))
        filter_graph = ";".join(filters)
        if len(filters) == 1:
            filter_graph += ";[a0]anull[out]"
        else:
            filter_graph += f";{labels}concat=n={len(filters)}:v=0:a=1[out]"

        # Pass 2 — splice out only the excess tail of each qualifying run.
        compress_cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-filter_complex", filter_graph,
            "-map", "[out]",
            "-c:a", "libmp3lame", "-b:a", "192k",
            out_path,
        ]
        proc = subprocess.run(compress_cmd, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg silence splice failed (rc={proc.returncode}): "
                f"{proc.stderr[-300:].decode(errors='replace')}"
            )

        out_ms = measure_audio_duration_ms(Path(out_path))
        with open(out_path, "rb") as f:
            result = f.read()

    logger.info(
        "AUDIO_SILENCE_COMPRESSED n=%d normal_cap_ms=%d dramatic_n=%d "
        "dramatic_cap_ms=%d in_ms=%d out_ms=%d removed_ms=%d",
        qualifying, cap_ms, len(dramatic), _DRAMATIC_SILENCE_MAX_MS,
        in_ms, out_ms, in_ms - out_ms,
    )
    return result


def _measure_mp3_bytes_duration_ms(mp3_bytes: bytes) -> int:
    """ffprobe-measure the duration of in-memory MP3 bytes.

    Writes to a temp file and reuses ``storage.measure_audio_duration_ms()``
    — the single duration-measurement point (CLAUDE.md §10.3) — rather than
    a second ffprobe-invocation implementation, so this measurement and
    ``audio.py``'s later measurement of the same bytes once written to their
    final on-disk path can never disagree in method.
    """
    if not mp3_bytes:
        return 0

    import tempfile
    from pathlib import Path
    from app.agents.agent3_audio.services.storage import measure_audio_duration_ms

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "measure.mp3"
        path.write_bytes(mp3_bytes)
        return measure_audio_duration_ms(path)


def _compute_section_boundaries(
    section_meta: list[dict],
    chunk_bytes_list: list[bytes],
    final_duration_ms: int,
) -> list[dict]:
    """Derive each section's real [start_ms, end_ms) span in the final audio
    (roadmap Phase B1 — per-language visual timing for parent renders).

    Deterministic, no AI call: measures each pre-concatenation chunk's own
    duration via ffprobe and accumulates offsets using the same fixed
    silence pad ``_concat_mp3_chunks()`` inserts between chunks, then
    rescales the whole sequence so it sums exactly to ``final_duration_ms``
    — the real, independently-measured duration of the actual persisted
    (concatenated + loudness-normalized) audio. Rescaling corrects for the
    small encoder-rounding drift that always exists between "sum of
    independently measured chunks" and "duration of the single re-encoded,
    concatenated file" — without it, boundaries would very slightly overrun
    or underrun the real audio length.

    Args:
        section_meta:      One ``{"section_type", "section_index"}`` dict per
                            chunk, in the same order as ``chunk_bytes_list``.
        chunk_bytes_list:  Raw MP3 bytes for each TTS chunk, pre-concatenation.
        final_duration_ms: Real measured duration of the final persisted audio.

    Returns:
        List of ``{"section_type", "section_index", "start_ms", "end_ms"}``
        dicts in playback order, or ``[]`` when there is nothing usable to
        compute from (mismatched/empty inputs, or a degenerate duration) —
        callers must treat an empty list as "no section data available",
        never as "one section covering nothing".
    """
    if not section_meta or not chunk_bytes_list or len(section_meta) != len(chunk_bytes_list):
        return []
    if final_duration_ms <= 0:
        return []

    pad_ms = _CHUNK_BOUNDARY_SILENCE_SECONDS * 1000
    raw_spans = [_measure_mp3_bytes_duration_ms(chunk) for chunk in chunk_bytes_list]

    # Raw (unscaled) cumulative boundary POINTS — N+1 of them for N chunks:
    # point[0] = 0, point[i+1] = point[i] + span_i (+ pad after every chunk
    # except the last, matching _concat_mp3_chunks()'s own pad placement).
    # Building shared boundary points (rather than each section computing
    # its own start/end independently) is what guarantees section i's
    # end_ms always equals section i+1's start_ms exactly — a beat can
    # never fall into an unmapped gap between two sections.
    raw_points = [0.0]
    cursor = 0.0
    for i, span in enumerate(raw_spans):
        cursor += span
        if i < len(raw_spans) - 1:
            cursor += pad_ms
        raw_points.append(cursor)

    raw_total = raw_points[-1]
    if raw_total <= 0:
        return []

    scale = final_duration_ms / raw_total
    scaled_points = [int(round(p * scale)) for p in raw_points]
    # Exact clamp at both ends: avoids leaving a few ms of rounding drift
    # at the very start/tail, matching _remap_beats_timing()'s own
    # last-beat clamp convention.
    scaled_points[0]  = 0
    scaled_points[-1] = final_duration_ms

    return [
        {
            "section_type": meta.get("section_type"),
            "section_index": meta.get("section_index"),
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        for meta, start_ms, end_ms in zip(section_meta, scaled_points[:-1], scaled_points[1:])
    ]


def _finalize_audio_with_boundaries(
    raw_bytes: bytes,
    chunk_bytes_list: list[bytes],
    section_meta: list[dict],
) -> tuple[bytes, list[dict]]:
    """Normalize loudness and compute real per-section boundaries — the one
    place both provider branches of ``generate_audio()`` funnel through
    (roadmap Phase A3 + Phase B1), so neither concern needs its own opt-in
    per call site.
    """
    normalized = _normalize_loudness(raw_bytes)
    if not section_meta:
        return normalized, []

    final_duration_ms = _measure_mp3_bytes_duration_ms(normalized)
    boundaries = _compute_section_boundaries(section_meta, chunk_bytes_list, final_duration_ms)
    if boundaries:
        normalized = _apply_section_loudness_arc(normalized, boundaries, section_meta)
        final_duration_ms = _measure_mp3_bytes_duration_ms(normalized)
        boundaries = _compute_section_boundaries(
            section_meta, chunk_bytes_list, final_duration_ms,
        )
    if boundaries:
        logger.info(
            "AUDIO_SECTION_BOUNDARIES_COMPUTED sections=%d final_duration_ms=%d",
            len(boundaries), final_duration_ms,
        )
    return normalized, boundaries


_SECTION_LOUDNESS_GAIN_DB: dict[str, float] = {
    "intro": -1.0,
    "early_buildup": 0.0,
    "late_buildup": 1.0,
    "climax_title": 1.5,
    "outro": -1.5,
}


def _apply_section_loudness_arc(
    mp3_bytes: bytes,
    boundaries: list[dict],
    section_meta: list[dict],
) -> bytes:
    """Apply a conservative deterministic hush-build-peak-settle level arc."""
    windows: list[tuple[float, float, float]] = []
    for boundary, meta in zip(boundaries, section_meta):
        gain_db = _SECTION_LOUDNESS_GAIN_DB.get(meta.get("delivery_reason"), 0.0)
        if gain_db:
            windows.append((
                boundary["start_ms"] / 1000.0,
                boundary["end_ms"] / 1000.0,
                gain_db,
            ))
    if not mp3_bytes or not windows:
        return mp3_bytes

    import math
    import os
    import subprocess
    import tempfile

    filters: list[str] = []
    input_label = "0:a"
    for i, (start_s, end_s, gain_db) in enumerate(windows):
        output_label = f"arc{i}"
        multiplier = math.pow(10.0, gain_db / 20.0)
        filters.append(
            f"[{input_label}]volume={multiplier:.8f}:"
            f"enable='between(t\\,{start_s:.6f}\\,{end_s:.6f})'"
            f"[{output_label}]"
        )
        input_label = output_label

    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "in.mp3")
        out_path = os.path.join(tmp, "out.mp3")
        with open(in_path, "wb") as f:
            f.write(mp3_bytes)
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", in_path,
                "-filter_complex", ";".join(filters),
                "-map", f"[{input_label}]",
                "-c:a", "libmp3lame", "-b:a", "192k",
                out_path,
            ],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg section loudness arc failed (rc={proc.returncode}): "
                f"{proc.stderr[-300:].decode(errors='replace')}"
            )
        with open(out_path, "rb") as f:
            result = f.read()

    logger.info("AUDIO_SECTION_LOUDNESS_ARC_APPLIED windows=%s", windows)
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


_TTS_TRANSPORT_ATTEMPTS = 3
_TTS_TRANSPORT_BACKOFF_SEC = (2.0, 5.0)


def _call_tts_with_transport_retry(request_fn, *, provider: str, unit_label: str):
    """Bounded transport retry around ONE provider TTS request.

    A real production run lost an entire language to a single read timeout
    on section 6/6 — the 5 already-generated sections were discarded with
    it. Transport failures (timeouts, connection resets, transient 5xx) get
    up to ``_TTS_TRANSPORT_ATTEMPTS`` total attempts with a short backoff —
    the same transport-not-quality retry class the Claude client uses
    (Elimination Mandate-compatible: this never re-rolls a *successful*
    generation, it only retries a request that returned no audio at all).

    ``TypeError`` propagates immediately: it is the Cartesia SDK
    request-shape incompatibility signal (`CARTESIA_REQUEST_FORMAT_UNSUPPORTED`)
    and retrying an identical malformed call can never succeed.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _TTS_TRANSPORT_ATTEMPTS + 1):
        try:
            return request_fn()
        except TypeError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == _TTS_TRANSPORT_ATTEMPTS:
                raise
            wait = _TTS_TRANSPORT_BACKOFF_SEC[
                min(attempt - 1, len(_TTS_TRANSPORT_BACKOFF_SEC) - 1)
            ]
            logger.warning(
                "TTS_TRANSPORT_RETRY provider=%s unit=%s attempt=%d/%d error=%s "
                "— retrying in %.0fs",
                provider, unit_label, attempt, _TTS_TRANSPORT_ATTEMPTS, exc, wait,
            )
            time.sleep(wait)
    raise last_exc  # pragma: no cover — unreachable


def _generate_cartesia_audio(
    voice_script: str, channel_voice, is_short_episode: bool = False,
) -> tuple[bytes, list[bytes], list[dict]]:
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
        Tuple of ``(concatenated MP3 bytes, per-chunk MP3 bytes list,
        per-chunk section metadata list)`` — the latter two are consumed by
        ``generate_audio()``'s ``_finalize_audio_with_boundaries()`` call to
        derive real per-section audio boundaries (roadmap Phase B1); the
        first is not yet loudness-normalized (the caller normalizes it).

    Raises:
        RuntimeError: If CARTESIA_API_KEY is not configured or a chunk fails.
    """
    from cartesia import Cartesia
    from app.config import settings

    tts_model = getattr(channel_voice, "tts_model", None) or "sonic-2"
    voice_id  = channel_voice.voice_id
    emotion   = (getattr(channel_voice, "emotion", None) or _DEFAULT_EMOTION).lower()
    # Hotfix A: validate the installed Cartesia SDK can actually send this
    # model's request shape BEFORE any section is prepared or any API call
    # is attempted — fails loud with an actionable message instead of a raw
    # TypeError surfacing mid-run from inside client.tts.bytes().
    _check_cartesia_sdk_compatibility(tts_model)

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
    section_meta: list[dict] = []
    for i, unit in enumerate(prepared_units):
        prepared = unit["prepared"]
        if not prepared.strip():
            continue

        logger.debug(
            "Cartesia chunk %d/%d: section=%s words=%d chars=%d emotion=%s speed_profile=%s",
            i + 1, len(prepared_units), unit.get("section_label") or "unmarked",
            len(_WORD_TOKEN_RE.findall(prepared)), len(prepared), unit["delivery"]["emotion"], unit["delivery"]["speed_profile"],
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
            wav_bytes = _call_tts_with_transport_retry(
                lambda: client.tts.bytes(**request_kwargs),
                provider="cartesia",
                unit_label=unit.get("section_label") or "unmarked",
            )
        except TypeError:
            # _check_cartesia_sdk_compatibility() already validated the SDK
            # supports this model's request shape before this loop started —
            # a TypeError reaching here means something unexpected (e.g. a
            # different kwarg mismatch this hotfix did not anticipate), not
            # the original "unexpected keyword argument 'voice'" failure mode.
            logger.exception(
                "CARTESIA_REQUEST_FORMAT_UNSUPPORTED: model=%s request_generation=%s "
                "(unexpected — SDK compatibility was already pre-checked for this model)",
                tts_model, _cartesia_model_generation(tts_model),
            )
            raise
        all_bytes.append(_wav_to_mp3(wav_bytes))
        section_meta.append({
            "section_type": unit.get("section_type"),
            "section_index": unit.get("section_index"),
            "delivery_reason": unit["delivery"]["reason"],
        })

    # Tier 1 R1: cap interior silences per chunk BEFORE concat — the same
    # (compressed) chunk list is returned for section-boundary computation,
    # so boundaries always measure the audio that actually ships.
    all_bytes = [_compress_interior_silence(b) for b in all_bytes]
    audio_bytes = _concat_mp3_chunks(all_bytes) if all_bytes else b""
    logger.debug(
        "Cartesia TTS complete: %d chunk(s) → %d bytes (%.1f KB)",
        len(prepared_units), len(audio_bytes), len(audio_bytes) / 1024,
    )
    return audio_bytes, all_bytes, section_meta


def generate_audio(voice_script: str, channel_voice, is_short_episode: bool = False) -> tuple[bytes, list[dict]]:
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
      itself applies deterministic pacing gated by reveal-beat evidence before
      being sent as its own Cartesia request. The request payload
      shape is chosen per ``tts_model``: the legacy shape for ``sonic-2``, or
      the ``generation_config`` shape (numeric speed, single emotion value,
      optional pronunciation dictionary id) for ``sonic-3``/``sonic-3.5``
      (Phase 11.3). WAV bytes from each unit are converted to MP3 via ffmpeg,
      then all units are concatenated with a short deterministic silence pad
      between them to avoid abrupt section-boundary joins (Phase 11.6).

    ElevenLabs path (provider="elevenlabs"):
      ``eleven_v3`` long-form scripts are split into one TTS request per marked
      section, use the same deterministic section delivery selector as Cartesia,
      resolve VoiceSettings per section, and prefix one v3 audio tag per section
      (for example ``[whispers]`` on the intro/early buildup, ``[gasps]`` on a
      climax-titled section, ``[sighs]`` on the outro). Non-v3 ElevenLabs models
      keep the legacy char-limit chunking path and channel-level delivery.
      Every chunk still passes through ``prepare_script_for_tts()``, so Phase
      11.5's reveal-gated pacing applies here too. ElevenLabs text-conditioning
      (``previous_text`` / ``next_text``) is still used only for non-v3 models
      whose SDK supports it; ``eleven_v3`` stays standalone per section.
      Final concatenation still goes through the same ffmpeg re-encode +
      silence-pad step as the Cartesia path (Phase 11.6) when more than one
      chunk exists.

    Args:
        voice_script:     Full narrator text (may include [INTRO]/[SECTION N]/[OUTRO]
                          markers — stripped per chunk before sending to the provider).
        channel_voice:    ChannelVoice ORM object — provides voice_id, provider,
                          tts_model, emotion, speed_profile, v3_stability_preset,
                          and per-field VoiceSettings overrides.
        is_short_episode: When ``True``, applies TikTok-optimised pacing in
                          ``prepare_script_for_tts`` (pause cap=10, no slow-open).

    Both paths cap interior narration silences per chunk via
    ``_compress_interior_silence()`` (pre-next-test roadmap Tier 1 R1) before
    concatenation — so section boundaries, Whisper, beats, and captions all
    derive from the compressed audio. Both paths' final audio then passes
    through ``_normalize_loudness()`` (roadmap Phase A3) before returning —
    a single ffmpeg loudnorm pass applied uniformly regardless of provider
    or chunk count. Both paths also compute
    real per-section audio boundaries (roadmap Phase B1) via
    ``_finalize_audio_with_boundaries()`` whenever the script produced one
    TTS chunk per ``[INTRO]``/``[SECTION N]``/``[OUTRO]`` section (always
    true for Cartesia; true for ElevenLabs only when ``tts_model="eleven_v3"``
    — the legacy non-v3 char-limit chunking path can merge multiple sections
    into one chunk, so no boundary data is computed for it).

    Returns:
        Tuple of ``(loudness-normalized MP3 bytes ready to be written to
        disk, section_boundaries)``. ``section_boundaries`` is a list of
        ``{"section_type", "section_index", "start_ms", "end_ms"}`` dicts in
        playback order, or ``[]`` when the script had no section markers or
        the provider/model path can't guarantee one chunk per section.

    Raises:
        RuntimeError: If the required API key is not configured, a chunk
                      fails, or loudness normalization fails (ffmpeg missing
                      or the loudnorm pass itself errors).
    """
    provider = (getattr(channel_voice, "provider", None) or "cartesia").lower()

    if provider == "cartesia":
        raw_bytes, chunk_bytes_list, section_meta = _generate_cartesia_audio(
            voice_script, channel_voice, is_short_episode=is_short_episode
        )
        return _finalize_audio_with_boundaries(raw_bytes, chunk_bytes_list, section_meta)

    # ── ElevenLabs path ───────────────────────────────────────────────────────
    model_id = getattr(channel_voice, "tts_model", None) or "eleven_multilingual_v2"
    voice_id = channel_voice.voice_id
    emotion  = (getattr(channel_voice, "emotion", None) or _DEFAULT_EMOTION).lower()
    if emotion not in _EMOTION_SETTINGS:
        emotion = _DEFAULT_EMOTION

    max_chars = _MODEL_CHAR_LIMITS.get(model_id, 9_500)
    client    = get_client()
    channel_speed_profile = getattr(channel_voice, "speed_profile", None) or "normal"

    prepared_chunks: list[dict] = []
    if model_id == "eleven_v3":
        section_units = _split_script_into_section_units(voice_script)
        for unit in section_units:
            delivery = _select_section_delivery(
                unit, emotion, channel_speed_profile, is_short_episode=is_short_episode
            )
            _log_section_delivery(unit, delivery, emotion, is_short_episode=is_short_episode)
            prepared = prepare_script_for_tts(
                unit["text"], language="", tone=delivery["emotion"],
                tts_model=model_id, is_short_episode=is_short_episode,
            )
            prepared = _with_elevenlabs_v3_delivery_tag(prepared, delivery)
            prepared_chunks.append({
                **unit,
                "prepared": prepared,
                "delivery": delivery,
                "voice_settings": VoiceSettings(**_resolve_voice_settings(
                    channel_voice,
                    emotion_override=delivery["emotion"],
                    speed_profile_override=delivery["speed_profile"],
                )),
            })
    else:
        for chunk in _chunk_script_at_sections(voice_script, max_chars):
            prepared = prepare_script_for_tts(
                chunk, language="", tone=emotion, tts_model=model_id,
                is_short_episode=is_short_episode,
            )
            prepared_chunks.append({
                "text": chunk,
                "prepared": prepared,
                "delivery": {
                    "emotion": emotion,
                    "speed_profile": channel_speed_profile,
                    "source": "fallback",
                    "reason": "elevenlabs_non_v3_static_policy",
                },
                "voice_settings": VoiceSettings(**_resolve_voice_settings(channel_voice)),
                **_parse_section_context(chunk),
            })

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

    # Section boundaries (roadmap Phase B1) can only be trusted when each
    # chunk is guaranteed to be exactly one script section — true for
    # eleven_v3 (one TTS request per section, above), never guaranteed for
    # the legacy non-v3 char-limit chunking path (_chunk_script_at_sections()
    # can merge several sections into one chunk to stay under max_chars).
    track_sections = model_id == "eleven_v3"

    all_bytes: list[bytes] = []
    section_meta: list[dict] = []

    for i, chunk in enumerate(prepared_chunks):
        prepared = chunk["prepared"]
        if not prepared.strip():
            continue

        delivery = chunk["delivery"]
        logger.debug(
            "ElevenLabs TTS chunk %d/%d: section=%s voice_id=%s emotion=%s speed_profile=%s words=%d model=%s chars=%d",
            i + 1, len(prepared_chunks), chunk.get("section_label") or "unmarked",
            voice_id, delivery["emotion"], delivery["speed_profile"],
            len(_WORD_TOKEN_RE.findall(prepared)), model_id, len(prepared),
        )

        kwargs: dict = dict(
            text=prepared,
            voice_id=voice_id,
            model_id=model_id,
            output_format=_ELEVENLABS_OUTPUT_FORMAT,
            voice_settings=chunk["voice_settings"],
        )

        if _use_text_conditioning and len(prepared_chunks) > 1:
            previous_prepared = prepared_chunks[i - 1]["prepared"] if i > 0 else ""
            next_prepared = prepared_chunks[i + 1]["prepared"] if i < len(prepared_chunks) - 1 else ""
            if previous_prepared.strip():
                kwargs["previous_text"] = previous_prepared[-300:]
            if next_prepared.strip():
                kwargs["next_text"] = next_prepared[:300]

        # The convert() iterator is consumed inside the retry closure — read
        # timeouts surface during iteration, not at call time, so joining
        # inside is what makes the retry actually cover them.
        blob = _call_tts_with_transport_retry(
            lambda: b"".join(client.text_to_speech.convert(**kwargs)),
            provider="elevenlabs",
            unit_label=chunk.get("section_label") or "unmarked",
        )
        all_bytes.append(blob)
        if track_sections:
            section_meta.append({
                "section_type": chunk.get("section_type"),
                "section_index": chunk.get("section_index"),
                "delivery_reason": delivery["reason"],
            })

    # Tier 1 R1: cap interior silences per chunk BEFORE concat and BEFORE
    # _finalize_audio_with_boundaries() measures the chunk list for section
    # boundaries — every downstream timeline sees the compressed audio.
    all_bytes = [_compress_interior_silence(b) for b in all_bytes]
    audio_bytes = _concat_mp3_chunks(all_bytes) if all_bytes else b""
    logger.debug(
        "ElevenLabs TTS complete: %d chunk(s) %d bytes (%.1f KB)",
        len(prepared_chunks), len(audio_bytes), len(audio_bytes) / 1024,
    )
    return _finalize_audio_with_boundaries(audio_bytes, all_bytes, section_meta)
