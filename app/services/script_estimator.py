"""Duration estimation helpers.

Pure Python helpers used before real audio exists. Agent 3 later records the
measured duration from generated audio; parent-short breakpoint estimation is
not part of the V2 standalone child-short architecture.

Roadmap 3.8 / audit G-7: the static ``SPEECH_RATES`` below were never
calibrated against real TTS output — a real run measured 135 wpm for parent
narration (assumed 150) and 120 wpm for child Shorts (planner math assumed
180 / "3 words per second"), producing a 13.3-minute video instead of ~9-10
and 110 s Shorts instead of the 60-90 s target band. ``AudioFile`` already
has the ground truth for every completed generation
(``duration_ms`` + ``whisper_transcript`` word count) — ``compute_measured_wpm()``
below computes a rolling per-language average from the most recent real
generations, and ``get_calibrated_wpm()`` is the single call site every
caller should use instead of reading ``SPEECH_RATES`` directly, so the
static table is only ever the cold-start fallback before enough real data
exists for a given language.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Average narration speed in words per minute per language (conservative
# estimates — cold-start fallback only, used until compute_measured_wpm()
# has enough real samples for a language; see get_calibrated_wpm()).
SPEECH_RATES: dict[str, float] = {
    "en": 150.0,
    "fr": 140.0,
    "de": 130.0,
    "es": 160.0,
    "it": 150.0,
    "pt": 145.0,
}
_DEFAULT_RATE = 140.0   # used when language is unknown

# Rolling window: average measured wpm over the most recent N real
# generations for a language. Requires at least _MIN_SAMPLES_FOR_CALIBRATION
# rows before the measured average is trusted over the static fallback —
# a single outlier (a very short/long AudioFile row) must not swing the
# calibration for every subsequent script in that language.
_ROLLING_WINDOW_SIZE = 20
_MIN_SAMPLES_FOR_CALIBRATION = 3


def compute_measured_wpm(
    db: "Session",
    language: str,
    is_short_episode: bool | None = None,
    channel_id: object | None = None,
) -> float | None:
    """Compute the rolling average measured wpm for a language from real audio.

    Queries the most recent ``_ROLLING_WINDOW_SIZE`` ``AudioFile`` rows for
    ``language`` that have a real Whisper transcript, and averages each row's
    measured rate — ``len(whisper_transcript) / (duration_ms / 60_000)``.
    Whisper's own word-level token count is used (not a re-split of the
    script text) since it reflects what the TTS engine actually spoke,
    including any words it dropped or added.

    Args:
        is_short_episode: When set, restrict the calibration window to parent
            long-form rows (``False``) or child Short rows (``True``) via a
            join on ``Content`` — the two run at measurably different rates
            (~135 vs ~120 wpm; fresh full-system audit §3.4), and a blended
            average drifts with whichever kind dominates the recent window.
            ``None`` preserves the original blended behavior for callers that
            do not know the content kind.
        channel_id: When set, restrict the window to content generated for this
            channel. TTS voice/provider configuration is channel-owned, so
            cross-channel language averages can mix incompatible speaking rates.
            ``None`` preserves unscoped behavior for callers lacking a channel.

    Returns:
        The rolling average wpm, or ``None`` if fewer than
        ``_MIN_SAMPLES_FOR_CALIBRATION`` usable rows exist yet (fail-open —
        callers fall back to the static ``SPEECH_RATES`` table).
    """
    from sqlalchemy import desc

    from app.models import AudioFile, Content

    query = db.query(AudioFile).filter(
        AudioFile.language == language, AudioFile.duration_ms > 0,
    )
    if is_short_episode is not None or channel_id is not None:
        query = query.join(Content, Content.id == AudioFile.content_id)
    if is_short_episode is not None:
        query = query.filter(Content.is_short_episode.is_(is_short_episode))
    if channel_id is not None:
        query = query.filter(Content.channel_id == channel_id)
    rows: list[AudioFile] = (
        query
        .order_by(desc(AudioFile.generated_at), desc(AudioFile.id))
        .limit(_ROLLING_WINDOW_SIZE)
        .all()
    )

    samples: list[float] = []
    for row in rows:
        transcript = row.whisper_transcript
        if not transcript:
            continue
        minutes = row.duration_ms / 60_000.0
        if minutes <= 0:
            continue
        samples.append(len(transcript) / minutes)

    if len(samples) < _MIN_SAMPLES_FOR_CALIBRATION:
        return None
    return sum(samples) / len(samples)


def get_calibrated_wpm(
    db: "Session | None",
    language: str,
    is_short_episode: bool | None = None,
    channel_id: object | None = None,
) -> float:
    """Return the best-known narration speed (wpm) for a language.

    Uses the rolling measured average (``compute_measured_wpm()``) when a
    database session is provided and enough real samples exist; otherwise
    falls back to the static ``SPEECH_RATES`` table. This is the single
    place wpm-dependent callers (Agent 2 script/Short word bands, Agent 4
    storyboard beat-pacing diagnostics) should read from, rather than
    reading ``SPEECH_RATES`` directly, so a future caller with a db session
    automatically benefits from real calibration data as it accumulates.

    ``is_short_episode`` scopes the calibration window to one content kind
    (see ``compute_measured_wpm()``) — pass it wherever the caller knows
    whether it is estimating for a parent long-form or a child Short.
    """
    if db is not None:
        measured = compute_measured_wpm(
            db, language, is_short_episode=is_short_episode, channel_id=channel_id,
        )
        if measured is not None:
            return measured
    return SPEECH_RATES.get(language, _DEFAULT_RATE)


def estimate_duration_sec(
    voice_script: str,
    language: str,
    db: "Session | None" = None,
    is_short_episode: bool | None = None,
    channel_id: object | None = None,
) -> float:
    """Estimate narration duration from a voice script's word count.

    Uses ``get_calibrated_wpm()`` — the rolling measured rate when ``db`` is
    provided and enough real samples exist for ``language``, otherwise the
    static ``SPEECH_RATES`` fallback (identical to pre-roadmap-3.8 behavior
    when ``db`` is omitted). The result feeds ``scripts.estimated_duration_sec``
    before real audio exists.

    Args:
        voice_script: The narrator text (no stage directions).
        language:     BCP-47 language code (e.g. "fr", "en").
        db:           Optional session; when provided, calibrates against
                      real measured audio for this language (roadmap 3.8).
        is_short_episode: Optional content-kind filter for the calibration
                      window (fresh full-system audit §3.4) — parents and
                      Shorts run at measurably different rates.
        channel_id: Optional channel filter that excludes speaking-rate
                      samples generated for unrelated channel voice settings.

    Returns:
        Estimated duration in seconds, rounded to 1 decimal place.
    """
    rate = get_calibrated_wpm(
        db, language, is_short_episode=is_short_episode, channel_id=channel_id,
    )
    word_count = len(voice_script.split())
    result = round((word_count / rate) * 60.0, 1)
    return result
