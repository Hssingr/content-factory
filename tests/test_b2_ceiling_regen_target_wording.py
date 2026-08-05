"""Runtime proof for Task 2d (code_report/TODO, 2026-08-05): the B2
ceiling-regen call's injected instruction states an explicit numeric target
BELOW the hard cap (255 words at the static 270-word default — leaving real
margin, not just skimming under the ceiling again), with explicit-consequence
framing (why landing right at the ceiling is risky), not merely "no more
than {cap} words".
"""

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery.services import script_workflow
from app.agents.agent2_discovery.services.script_workflow import _CEILING_REGEN_TARGET_MARGIN
from app.models import Channel, ChannelConfig, ChannelVoice, Content


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


def test_ceiling_regen_target_margin_constant():
    # 270 (static _MAX_SHORT_WORDS) - 15 = 255, matching Task 2d's example.
    assert _CEILING_REGEN_TARGET_MARGIN == 15


def test_word_ceiling_note_states_explicit_target_below_cap_with_consequence():
    channel_id, content_id = uuid.uuid4(), uuid.uuid4()
    db = _Db()
    db.add(Channel(id=channel_id, niche="history", tone="dramatic"))
    db.add(ChannelConfig(channel_id=channel_id, script_format="youtube_long",
                         output_mode="shorts_only", script_source="reddit",
                         audio_tags_enabled=False, visual_style="story_driven",
                         image_style="cinematic_cartoon"))
    db.add(ChannelVoice(id=uuid.uuid4(), channel_id=channel_id, language="en",
                        provider="cartesia", voice_id="v1", tts_model="sonic-3.5"))
    content = Content(id=content_id, channel_id=channel_id, is_short_episode=True,
                      parent_content_id=None, source_language="en", status="APPROVED",
                      title="T", source_url="https://example.com/x",
                      source_excerpt=" ".join(["source"] * 450))
    db.add(content)

    calls = []

    def generate(*args, **kwargs):
        calls.append(kwargs)
        return {"title": "T", "voice_script": " ".join(["word"] * (300 if len(calls) == 1 else 250))}

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

    assert len(calls) == 2
    note = calls[1]["word_ceiling_note"]

    # Explicit numeric target below the cap — at the static default, 255,
    # not just "no more than 270".
    assert "at most 255 words" in note
    assert "270-word ceiling" in note

    # Explicit-consequence framing: states WHY a draft right at the ceiling
    # is risky, matching the convention used elsewhere in this codebase
    # (e.g. the beat-count/hint-length prompt tightening, CLAUDE.md §11.4).
    assert "no margin" in note or "risks tipping" in note
