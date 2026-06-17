import logging
import re

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

def _apply_pacing_markers(
    text: str,
    tone: str,
    tts_model: str = "sonic-2",
) -> str:
    """Add strategic pacing cues for optimal TTS delivery — model-aware.

    Model branching:
      - ``eleven_v3``:       inserts ``[dramatic pause]`` tags (native v3 syntax,
                             interpreted by ElevenLabs as an expressive pause).
      - all other models:    inserts ``"..."`` ellipsis (standard TTS pause cue,
                             works for ElevenLabs v2/flash and Cartesia sonic-2).
    Both paths cap total insertions at ``_MAX_PAUSE_INSERTIONS`` (6).

    Applied to ALL tones:
      - Insert a pause marker before sentences that open with a narrative reveal
        phrase (That / Then / But / And then / The truth / It turned out …).

    Applied only to dramatic / suspense / horror / tense tones:
      - ellipsis path: replace the first short sentence's terminal period with ``"..."``.
      - v3 path: prepend ``[dramatic pause]`` before the first content sentence.

    Args:
        text:      Marker-stripped, punctuation-normalized narration text.
        tone:      Channel tone / emotion label (case-insensitive).
        tts_model: TTS model ID — ``eleven_v3`` uses audio tags; all others use ellipsis.

    Returns:
        Text with model-appropriate pacing cues inserted.
    """
    use_v3 = tts_model == "eleven_v3"
    insertion_count = 0

    reveal_re = re.compile(
        r"([\.\!\?]\s+)((?:That|Then|But|And then|Until|Except|Until then|"
        r"What they found|What he found|What she found|"
        r"The answer|The truth|The result|It turned out)\b)",
        re.IGNORECASE,
    )

    def _insert_pause(match: re.Match) -> str:
        nonlocal insertion_count
        if insertion_count >= _MAX_PAUSE_INSERTIONS:
            return match.group(0)
        insertion_count += 1
        if use_v3:
            return match.group(1) + "[dramatic pause] " + match.group(2)
        return match.group(1) + "... " + match.group(2)

    text = reveal_re.sub(_insert_pause, text)

    # Slow-open for high-drama tones (only if budget remains)
    if tone.lower() in {"horror", "suspense", "dramatic", "tense"} and insertion_count < _MAX_PAUSE_INSERTIONS:
        if use_v3:
            text = "[dramatic pause] " + text.lstrip()
            insertion_count += 1
        else:
            first_end = re.search(r"(?<=[a-z])\.\s", text)
            if first_end and len(text[: first_end.start()].split()) <= 12:
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
) -> str:
    """Prepare a voice script for optimal TTS delivery without altering stored content.

    Strips section markers, normalizes punctuation, splits unnaturally long sentences,
    and applies model-aware pacing cues — all before the text reaches the TTS provider.
    The stored voice_script in the DB is never modified.

    Args:
        voice_script: Full narrator text (may include [INTRO]/[SECTION N]/[OUTRO] markers).
        language:     BCP-47 language code (e.g. "fr", "en") — reserved for future
                      language-specific normalization rules.
        tone:         Emotion label from channel_voices (e.g. "dramatic", "calm").
        tts_model:    TTS model ID — ``eleven_v3`` uses ``[dramatic pause]`` audio tags;
                      all other models (including Cartesia sonic-2) use ``"..."`` ellipsis.

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

    # 4. Pacing cues — reveal pauses for all tones, slow-open for dramatic tones
    text = _apply_pacing_markers(text, tone=tone or "", tts_model=tts_model)

    # 5. Diagnostic log
    original_words = len(voice_script.split())
    prepared_words = len(text.split())
    sentences_out  = _LONG_SENTENCE_RE.split(text)
    avg_sent_len   = (
        sum(len(s.split()) for s in sentences_out) / len(sentences_out)
        if sentences_out else 0.0
    )
    pause_points = text.count("...")
    logger.info(
        "TTS prepare: language=%s tone=%s "
        "original_words=%d prepared_words=%d "
        "avg_sentence_len=%.1f long_sentences_fixed=%d pause_points=%d",
        language, tone or "none",
        original_words, prepared_words,
        avg_sent_len, fixed, pause_points,
    )

    return text


def _concat_mp3_chunks(chunk_bytes_list: list[bytes]) -> bytes:
    """Concatenate multiple mp3 byte blobs into one gapless mp3 stream via ffmpeg.

    Uses ffmpeg's concat demuxer with re-encoding at 192 kbps to avoid click
    artifacts and prosody discontinuities from raw byte concatenation.
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

        # Build ffmpeg concat list file
        list_file = os.path.join(tmp, "concat.txt")
        with open(list_file, "w") as f:
            for p in input_paths:
                f.write(f"file '{p}'\n")

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
        logger.info(
            "Concat: ffmpeg re-encode %d chunks → %d bytes (%.1f KB)",
            len(chunk_bytes_list), len(result), len(result) / 1024,
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


def _generate_cartesia_audio(voice_script: str, channel_voice) -> bytes:
    """Generate MP3 audio via Cartesia TTS (provider='cartesia').

    Chunks the script at [SECTION N] boundaries, calls client.tts.bytes() for
    each chunk (WAV format), converts each WAV to MP3 via ffmpeg, then
    concatenates. No text-conditioning stitching — each chunk is standalone.

    Args:
        voice_script:  Full narrator text (may include section markers).
        channel_voice: ChannelVoice ORM object — provides voice_id, tts_model,
                       speed_profile.

    Returns:
        Raw MP3 bytes ready to be written to disk.

    Raises:
        RuntimeError: If CARTESIA_API_KEY is not configured or a chunk fails.
    """
    from cartesia import Cartesia
    from app.config import settings

    tts_model = getattr(channel_voice, "tts_model", None) or "sonic-2"
    voice_id  = channel_voice.voice_id
    profile   = getattr(channel_voice, "speed_profile", None) or "normal"
    speed_str = _CARTESIA_SPEED_MAP.get(profile, "normal")
    emotion   = (getattr(channel_voice, "emotion", None) or _DEFAULT_EMOTION).lower()
    if emotion not in _CARTESIA_EMOTION_MAP:
        emotion = _DEFAULT_EMOTION
    emotion_list = _CARTESIA_EMOTION_MAP[emotion]

    max_chars = _MODEL_CHAR_LIMITS.get(tts_model, 9_500)
    chunks    = _chunk_script_at_sections(voice_script, max_chars)
    client    = Cartesia(api_key=settings.cartesia_api_key)

    prepared_chunks: list[str] = []
    for chunk in chunks:
        p = prepare_script_for_tts(chunk, language="", tone=emotion, tts_model=tts_model)
        prepared_chunks.append(p)

    logger.info(
        "Cartesia TTS: voice_id=%s model=%s speed=%s emotion=%s cartesia_emotion=%s chunks=%d",
        voice_id, tts_model, speed_str, emotion, emotion_list, len(prepared_chunks),
    )

    all_bytes: list[bytes] = []
    for i, prepared in enumerate(prepared_chunks):
        if not prepared.strip():
            continue

        logger.info(
            "Cartesia chunk %d/%d: words=%d chars=%d",
            i + 1, len(prepared_chunks), len(prepared.split()), len(prepared),
        )

        wav_bytes = client.tts.bytes(
            model_id=tts_model,
            transcript=prepared,
            voice_id=voice_id,
            output_format=_CARTESIA_OUTPUT_FORMAT,
            _experimental_voice_controls={"speed": speed_str, "emotion": emotion_list},
        )
        all_bytes.append(_wav_to_mp3(wav_bytes))

    audio_bytes = _concat_mp3_chunks(all_bytes) if all_bytes else b""
    logger.info(
        "Cartesia TTS complete: %d chunk(s) → %d bytes (%.1f KB)",
        len(prepared_chunks), len(audio_bytes), len(audio_bytes) / 1024,
    )
    return audio_bytes


def generate_audio(voice_script: str, channel_voice) -> bytes:
    """Convert a voice script to MP3 audio via the configured TTS provider.

    Routes to Cartesia or ElevenLabs based on ``channel_voice.provider``.

    Cartesia path (provider="cartesia"):
      Each chunk is a standalone call — no text-conditioning stitching.
      WAV bytes from Cartesia are converted to MP3 per chunk via ffmpeg.

    ElevenLabs path (provider="elevenlabs"):
      Model, VoiceSettings, and chunking derived from ``channel_voice``.
      Text-conditioning (``previous_text`` / ``next_text``) applied for
      v2/flash models; skipped for ``eleven_v3`` (not supported).

    Args:
        voice_script:  Full narrator text (may include [INTRO]/[SECTION N]/[OUTRO]
                       markers — stripped per chunk before sending to the provider).
        channel_voice: ChannelVoice ORM object — provides voice_id, provider,
                       tts_model, emotion, speed_profile, v3_stability_preset,
                       and per-field VoiceSettings overrides.

    Returns:
        Raw MP3 bytes ready to be written to disk.

    Raises:
        RuntimeError: If the required API key is not configured or a chunk fails.
    """
    provider = (getattr(channel_voice, "provider", None) or "cartesia").lower()

    if provider == "cartesia":
        return _generate_cartesia_audio(voice_script, channel_voice)

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
        p = prepare_script_for_tts(chunk, language="", tone=emotion, tts_model=model_id)
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
    logger.info("ElevenLabs TTS stitching path: %s (%d chunk(s))", stitching_path, len(prepared_chunks))

    all_bytes: list[bytes] = []

    for i, prepared in enumerate(prepared_chunks):
        if not prepared.strip():
            continue

        logger.info(
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
    logger.info(
        "ElevenLabs TTS complete: %d chunk(s) %d bytes (%.1f KB)",
        len(prepared_chunks), len(audio_bytes), len(audio_bytes) / 1024,
    )
    return audio_bytes
