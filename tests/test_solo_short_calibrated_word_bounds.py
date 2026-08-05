"""Runtime proof: Solo Short word floor/ceiling self-correct to a channel's
real measured Short narration rate instead of trusting the one-time static
175 wpm assumption forever.

Root cause this closes: content f71183e3 (2026-08-04 production run) shipped
a 308-word script (already above the 270-word static cap) that rendered at
127.4s — but even a script that HAD respected the 270-word cap would still
have landed around 114s for this channel's actual configured voice, which
this run's own AudioFile row measured at ~142 wpm against the ~175 wpm the
static cap assumes. See code_report/f711_video_quality_audit.md Finding 4
and app/agents/agent2_discovery/services/scripts.py's
_resolve_short_word_bounds().
"""

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery.services.scripts import (
    _MAX_SHORT_WORDS,
    _MIN_SHORT_WORDS,
    _resolve_short_word_bounds,
)
from app.agents.agent2_discovery.services import script_workflow
from app.models import AudioFile, Channel, ChannelConfig, ChannelVoice, Content


class _Query:
    def __init__(self, rows): self.rows = rows
    def filter(self, *args, **kwargs): return self
    def join(self, *args, **kwargs): return self
    def order_by(self, *args, **kwargs): return self
    def limit(self, count): return _Query(self.rows[:count])
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows


class _Db:
    def __init__(self): self.tables = {}
    def add(self, row): self.tables.setdefault(type(row), []).append(row)
    def query(self, model): return _Query(self.tables.get(model, []))
    def get(self, model, key):
        return next((row for row in self.tables.get(model, [])
                     if getattr(row, "id", None) == key or getattr(row, "channel_id", None) == key), None)
    def flush(self): pass
    def refresh(self, row): pass
    def commit(self): pass


def _audio_row(words: int, duration_ms: int) -> AudioFile:
    return AudioFile(
        id=uuid.uuid4(), content_id=uuid.uuid4(), language="en",
        file_path="audio/x.mp3", duration_ms=duration_ms,
        whisper_transcript=[{"word": "w", "start": 0.0, "end": 0.1}] * words,
    )


def test_no_real_samples_returns_static_fallback_unchanged():
    db = _Db()
    floor, ceiling = _resolve_short_word_bounds(db, "en", uuid.uuid4())
    assert (floor, ceiling) == (_MIN_SHORT_WORDS, _MAX_SHORT_WORDS) == (190, 270)


def test_none_db_returns_static_fallback_unchanged():
    floor, ceiling = _resolve_short_word_bounds(None, "en", uuid.uuid4())
    assert (floor, ceiling) == (190, 270)


def test_slow_voice_calibration_tightens_the_ceiling_below_static_270():
    # 302 words / 127.392s == ~142.24 wpm — the exact f711 production
    # measurement (OpenAI Whisper word count / real ffprobe duration).
    db = _Db()
    for _ in range(3):
        db.add(_audio_row(words=302, duration_ms=127_392))
    floor, ceiling = _resolve_short_word_bounds(db, "en", uuid.uuid4())
    # 142.24 wpm * 90s/60 ~= 213 words - well under the static 270 assumption.
    assert ceiling < 270
    assert 205 <= ceiling <= 220
    # 142.24 wpm * 65s/60 ~= 154 words - above the function's 150 absolute
    # floor, so the computed value (not the safety rail) applies here.
    assert floor == 154
    # A ceiling never below floor+20 by construction.
    assert ceiling >= floor + 20


def test_pathologically_slow_measured_rate_hits_the_absolute_floor_rail():
    # A corrupted/implausible measurement (60 wpm) must not drive the word
    # floor below the absolute safety minimum this function hardcodes.
    db = _Db()
    for _ in range(3):
        db.add(_audio_row(words=61, duration_ms=61_000))  # 61 words / 61s == 60 wpm
    floor, _ceiling = _resolve_short_word_bounds(db, "en", uuid.uuid4())
    assert floor == 150


def test_fast_voice_calibration_raises_the_ceiling_above_static_270():
    # A hypothetical fast voice: 300 words in 72s == 250 wpm.
    db = _Db()
    for _ in range(3):
        db.add(_audio_row(words=300, duration_ms=72_000))
    floor, ceiling = _resolve_short_word_bounds(db, "en", uuid.uuid4())
    assert ceiling > 270
    assert floor > 190


def test_two_samples_is_still_below_the_calibration_threshold():
    # _MIN_SAMPLES_FOR_CALIBRATION == 3 — two real rows must not yet override
    # the static fallback (avoids one/two-sample noise swinging the target).
    db = _Db()
    for _ in range(2):
        db.add(_audio_row(words=302, duration_ms=127_392))
    floor, ceiling = _resolve_short_word_bounds(db, "en", uuid.uuid4())
    assert (floor, ceiling) == (190, 270)


def test_solo_short_workflow_ceiling_regen_uses_the_calibrated_word_target():
    """End-to-end: a channel with 3 completed slow-voice Shorts already on
    file gets a ceiling-regen note quoting the CALIBRATED number, not the
    static 270 — proving the wiring reaches the real regeneration path, not
    just the helper function in isolation."""
    channel_id, content_id = uuid.uuid4(), uuid.uuid4()
    db = _Db()
    db.add(Channel(id=channel_id, niche="history", tone="dramatic"))
    db.add(ChannelConfig(channel_id=channel_id, script_format="youtube_long",
                         output_mode="shorts_only", script_source="reddit",
                         audio_tags_enabled=False, visual_style="story_driven",
                         image_style="cinematic_cartoon"))
    db.add(ChannelVoice(id=uuid.uuid4(), channel_id=channel_id, language="en",
                        provider="elevenlabs", voice_id="v1", tts_model="eleven_v3"))
    for _ in range(3):
        db.add(_audio_row(words=302, duration_ms=127_392))
    content = Content(id=content_id, channel_id=channel_id, is_short_episode=True,
                      parent_content_id=None, source_language="en", status="APPROVED",
                      title="T", source_url="https://example.com/x",
                      source_excerpt=" ".join(["source"] * 450))
    db.add(content)

    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs)
        # First draft: 260 words - over the calibrated ~213-word ceiling,
        # but would have been ACCEPTED outright under the old static 270 cap.
        # Second (regenerated) draft: complies with the calibrated ceiling.
        return {"title": "T", "voice_script": " ".join(["word"] * (260 if len(calls) == 1 else 210))}

    with (
        patch.object(script_workflow, "generate_story_blueprint", return_value={
            "hook": "x", "major_turns": [], "final_payoff": "y", "comment_trigger": "z",
            "suggested_section_count": 3, "suggested_title": "T",
            "character_descriptors": [], "era_setting": "", "protagonist_gender": "unspecified",
            "midpoint_retention_trap": "", "central_question": "",
        }),
        patch.object(script_workflow, "generate_solo_short_script", side_effect=generate),
        patch("app.agents.agent2_discovery.services.scripts.generate_native_script",
              return_value={"voice_script": ""}),
    ):
        script_workflow.run_script_workflow(content, db)

    assert len(calls) == 2, "260 words must trigger a regen once calibration tightens the ceiling"
    note = calls[1]["word_ceiling_note"]
    assert "270-word ceiling" not in note
    assert any(f"{n}-word ceiling" in note for n in range(206, 221)), note
