"""Runtime proof for B2's one-shot Solo Short word-ceiling regeneration."""

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))

from app.agents.agent2_discovery.services import script_workflow
from app.agents.agent2_discovery.services import scripts as agent2_scripts
from app.models import Channel, ChannelConfig, ChannelVoice, Content, Script


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


def _fixtures():
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
    return db, content


def _run(drafts):
    db, content = _fixtures()
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs)
        return drafts[len(calls) - 1]
    with (
        patch.object(script_workflow, "generate_story_blueprint", return_value={
            "hook": "x", "major_turns": [], "final_payoff": "y", "comment_trigger": "z",
            "suggested_section_count": 3, "suggested_title": "T",
            "character_descriptors": [], "era_setting": "", "protagonist_gender": "unspecified",
            "midpoint_retention_trap": "", "central_question": "",
        }),
        patch.object(script_workflow, "generate_solo_short_script", side_effect=generate),
        patch.object(agent2_scripts, "generate_native_script", return_value={"voice_script": ""}),
    ):
        script_workflow.run_script_workflow(content, db)
    source = next(row for row in db.tables.get(Script, []) if row.language == "en")
    return calls, source


def test_over_110_percent_triggers_once_and_keeps_shorter_draft():
    calls, source = _run([
        {"title": "T", "voice_script": " ".join(["word"] * 300)},
        {"title": "T", "voice_script": " ".join(["word"] * 260)},
    ])
    assert len(calls) == 2
    assert len(source.voice_script.split()) == 260
    assert "270-word ceiling" in calls[1]["word_ceiling_note"]


def test_at_110_percent_does_not_regenerate_and_longer_retry_is_rejected():
    calls, source = _run([{"title": "T", "voice_script": " ".join(["word"] * 297)}])
    assert len(calls) == 1
    assert len(source.voice_script.split()) == 297

    calls, source = _run([
        {"title": "T", "voice_script": " ".join(["word"] * 300)},
        {"title": "T", "voice_script": " ".join(["word"] * 310)},
    ])
    assert len(calls) == 2
    assert len(source.voice_script.split()) == 300
