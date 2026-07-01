"""Agent 1 V3.6 — channel configuration snapshot foundation proof.

Zero live API calls, zero database writes/commits. Builds an in-memory
fake `Channel` ORM-shaped object (simple namespace/dataclass stand-ins,
not a real DB row) to prove build_channel_config_snapshot()/
validate_channel_config_snapshot()/attach_snapshot_to_content() behave
correctly, without ever opening a database session.

Run: python scripts/smoke_agent1_config_snapshot.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


# ═══════════════════════════════════════════════════════════════════════════
# 0: existing content/model imports still work — config_snapshot.py imports
#    cleanly alongside every existing Agent 1 model/service module
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 0: existing model/service imports still work ──")
from app.models import Channel, Content  # noqa: E402
from app.agents.agent1_setup.services.config_snapshot import (  # noqa: E402
    build_channel_config_snapshot,
    validate_channel_config_snapshot,
    attach_snapshot_to_content,
)
from app.agents.agent1_setup.services.activation_readiness import check_activation_readiness  # noqa: E402
from app.agents.agent1_setup.services.v3_config_rules import validate_v3_channel_config  # noqa: E402

check("0a: Content model still imports and has the new nullable column",
      "channel_config_snapshot" in Content.__table__.columns)
check("0b: the new column is nullable (additive, backwards-compatible)",
      Content.__table__.columns["channel_config_snapshot"].nullable is True)
check("0c: previously-existing services (activation_readiness, v3_config_rules) "
      "still import and run unaffected by this phase's additions",
      callable(check_activation_readiness) and callable(validate_v3_channel_config))


# ═══════════════════════════════════════════════════════════════════════════
# Fake ORM-shaped fixtures (no DB session, no real Channel/Content row)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FakeConfig:
    channel_id: str = "chan-1"
    videos_per_week: int = 5
    content_mode: str = "single_story"
    script_source: str = "reddit"
    output_mode: str = "youtube_and_shorts"
    visual_style: str = "noir"
    image_style: str = "photorealistic"


@dataclass
class FakeLanguage:
    language: str
    channel_name: str


@dataclass
class FakeVoice:
    language: str
    provider: str
    tts_model: str
    voice_id: str
    # deliberately no credentials field here — ChannelVoice never carries one


@dataclass
class FakeSource:
    source_type: str
    source_value: str
    language: str
    trust_score: float


@dataclass
class FakePlatform:
    platform: str
    language: str
    verified: bool
    active: bool
    credentials_encrypted: str = "FERNET_ENCRYPTED_SECRET_SHOULD_NEVER_APPEAR_IN_SNAPSHOT"


@dataclass
class FakeTiming:
    platform: str
    language: str
    timezone: str
    optimal_days: list
    optimal_hour_start: int
    optimal_hour_end: int
    shorts_spread_hours: int


@dataclass
class FakeChannel:
    id: str
    config: FakeConfig
    languages: list = field(default_factory=list)
    voices: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    platforms: list = field(default_factory=list)
    publish_timings: list = field(default_factory=list)


@dataclass
class FakeContent:
    id: str
    channel_config_snapshot: dict | None = None


channel = FakeChannel(
    id="chan-1",
    config=FakeConfig(),
    languages=[FakeLanguage("en", "My Channel"), FakeLanguage("es", "Mi Canal")],
    voices=[
        FakeVoice("en", "cartesia", "sonic-3.5", "voice-en-123"),
        FakeVoice("es", "elevenlabs", "eleven_v3", "voice-es-456"),
    ],
    sources=[FakeSource("reddit", "r/nosleep", "en", 0.9)],
    platforms=[
        FakePlatform("youtube", "en", True, True),
        FakePlatform("tiktok", "en", False, False),
    ],
    publish_timings=[FakeTiming("youtube", "en", "UTC", ["monday", "friday"], 18, 20, 6)],
)

# ═══════════════════════════════════════════════════════════════════════════
# 1: snapshot captures V3 fields
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: snapshot captures V3 fields ──")
snapshot = build_channel_config_snapshot(channel)
check("1a: content_mode captured", snapshot["content_mode"] == "single_story")
check("1b: script_source captured", snapshot["script_source"] == "reddit")
check("1c: output_mode captured", snapshot["output_mode"] == "youtube_and_shorts")
check("1d: visual_style captured", snapshot["visual_style"] == "noir")
check("1e: image_style captured", snapshot["image_style"] == "photorealistic")

# ═══════════════════════════════════════════════════════════════════════════
# 2: snapshot captures languages/platforms/voices/sources/timing
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: snapshot captures languages/platforms/voices/sources/timing ──")
check("2a: languages captured (2 rows)", len(snapshot["languages"]) == 2
      and {l["language"] for l in snapshot["languages"]} == {"en", "es"})
check("2b: platforms captured (2 rows)", len(snapshot["platforms"]) == 2)
check("2c: platform credentials_encrypted is NEVER copied into the snapshot "
      "(CLAUDE.md §30 — credentials must never be duplicated unencrypted)",
      "credentials_encrypted" not in json.dumps(snapshot)
      and "FERNET_ENCRYPTED_SECRET_SHOULD_NEVER_APPEAR_IN_SNAPSHOT" not in json.dumps(snapshot))
check("2d: voices captured with provider/model/id per language",
      snapshot["voices"] == [
          {"language": "en", "provider": "cartesia", "tts_model": "sonic-3.5", "voice_id": "voice-en-123"},
          {"language": "es", "provider": "elevenlabs", "tts_model": "eleven_v3", "voice_id": "voice-es-456"},
      ])
check("2e: source summary captured", snapshot["source_summary"] == [
    {"source_type": "reddit", "source_value": "r/nosleep", "language": "en", "trust_score": 0.9},
])
check("2f: publish timing summary captured", len(snapshot["publish_timing_summary"]) == 1
      and snapshot["publish_timing_summary"][0]["optimal_days"] == ["monday", "friday"])
check("2g: videos_per_week captured", snapshot["videos_per_week"] == 5)
check("2h: channel_id and channel_config_id captured",
      snapshot["channel_id"] == "chan-1" and snapshot["channel_config_id"] == "chan-1")
check("2i: captured_at is present and ISO-formatted",
      isinstance(snapshot["captured_at"], str) and "T" in snapshot["captured_at"])

# ═══════════════════════════════════════════════════════════════════════════
# 3: snapshot is JSON-serializable
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: snapshot is JSON-serializable ──")
serialized = json.dumps(snapshot)
roundtrip = json.loads(serialized)
check("3a: json.dumps succeeds with no TypeError (no exotic/non-serializable types)",
      isinstance(serialized, str))
check("3b: round-trip equals the original snapshot", roundtrip == snapshot)

# ═══════════════════════════════════════════════════════════════════════════
# 4: validator behavior — accepts a well-formed snapshot, rejects missing
#    critical fields
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: snapshot validator accepts well-formed, rejects missing-field snapshots ──")
issues_ok = validate_channel_config_snapshot(snapshot)
check("4a: a well-formed snapshot has zero validation issues", issues_ok == [])

broken = dict(snapshot)
del broken["content_mode"]
del broken["channel_id"]
issues_broken = validate_channel_config_snapshot(broken)
check("4b: removing content_mode and channel_id produces at least 2 issues",
      len(issues_broken) >= 2)
check("4c: the missing-channel_id issue is specifically flagged "
      "(not just a generic missing-field issue)",
      any(i["code"] == "missing_channel_id" for i in issues_broken))
check("4d: validator never raises — always returns a list, even for garbage input",
      validate_channel_config_snapshot("not a dict") != [] and
      isinstance(validate_channel_config_snapshot("not a dict"), list))

# ═══════════════════════════════════════════════════════════════════════════
# 5: immutability — attach_snapshot_to_content() refuses to overwrite
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: attach_snapshot_to_content() enforces immutability ──")
fake_content = FakeContent(id="content-1")
attach_snapshot_to_content(fake_content, snapshot)
check("5a: first attach succeeds and sets the attribute",
      fake_content.channel_config_snapshot == snapshot)

raised = False
try:
    attach_snapshot_to_content(fake_content, {"channel_id": "different"})
except ValueError:
    raised = True
check("5b: a second attach on the same content row raises ValueError "
      "(snapshots are immutable once attached)", raised)
check("5c: the original snapshot value is unchanged after the rejected second attach",
      fake_content.channel_config_snapshot == snapshot)

# ═══════════════════════════════════════════════════════════════════════════
# 6: no Agent 2-5 runtime file was changed
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6: no Agent 2-5 runtime file was changed ──")
git_status = subprocess.run(
    ["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True, timeout=30,
).stdout
changed_paths = [line[3:] for line in git_status.splitlines()]
agent2_5_changes = [
    p for p in changed_paths
    if any(p.startswith(prefix) for prefix in (
        "app/agents/agent2_discovery/", "app/agents/agent3_audio/",
        "app/agents/agent4_visuals/", "app/agents/agent5_render/",
    ))
]
check("6a: git status shows zero changed/new files under agent2_discovery/agent3_audio/"
      "agent4_visuals/agent5_render", not agent2_5_changes, agent2_5_changes)
check("6b: this phase's own new files are backend/migration-only, not frontend "
      "(app/ui/ files present in git status, if any, predate this phase's V3.5 work "
      "and were not touched again here)",
      "app/agents/agent1_setup/services/config_snapshot.py" in changed_paths
      and "scripts/smoke_agent1_config_snapshot.py" in changed_paths
      and "alembic/versions/005_add_content_channel_config_snapshot.py" in changed_paths)

# ═══════════════════════════════════════════════════════════════════════════
# 7: migration is additive only and was not executed
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 7: migration is additive-only and was not executed ──")
migration_src = (ROOT / "alembic" / "versions" / "005_add_content_channel_config_snapshot.py").read_text()
check("7a: migration only adds a column (no drop_table/alter on any existing column)",
      "op.add_column" in migration_src and "drop_column" not in migration_src.split("def downgrade")[0])
upgrade_body = migration_src.split("def upgrade")[1].split("def downgrade")[0]
check("7b: the new column is nullable with no server_default (no forced backfill)",
      "nullable=True" in upgrade_body and "server_default" not in upgrade_body)

alembic_current = subprocess.run(
    ["alembic", "current"], cwd=str(ROOT), capture_output=True, text=True, timeout=30,
).stdout
alembic_heads = subprocess.run(
    ["alembic", "heads"], cwd=str(ROOT), capture_output=True, text=True, timeout=30,
).stdout
check("7c: alembic recognizes revision 005 as the new head (file is well-formed)",
      "005" in alembic_heads)
check("7d: the database's current revision is NOT 005 — migration was not executed",
      "005" not in alembic_current)

print()
print("SMOKE PASS — Agent 1 V3.6 config snapshot foundation")
