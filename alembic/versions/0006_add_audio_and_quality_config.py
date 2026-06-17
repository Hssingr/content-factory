"""add audio and quality config

Revision ID: 0006_add_audio_and_quality_config
Revises: a3f7e9c12b45
Create Date: 2026-06-11 00:00:00.000000

Adds:
  channel_voices  — ElevenLabs model selection + per-voice VoiceSettings overrides
                    + speed_profile + v3_stability_preset
  channel_config  — audio_tags_enabled (ElevenLabs v3 audio tags opt-in)
  audio_files     — short_rehook_paths, short_bridge_paths (JSONB lists, one path
                    per Short indexed by part number; None where clip is not needed)
                    (added in Block 5 — listed here so a single migration covers
                    all audio-related schema additions)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: Union[str, None] = "a3f7e9c12b45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── channel_voices: ElevenLabs model + VoiceSettings overrides ───────────
    op.add_column(
        "channel_voices",
        sa.Column(
            "elevenlabs_model",
            sa.String(64),
            nullable=False,
            server_default="eleven_multilingual_v2",
        ),
    )
    op.add_column(
        "channel_voices",
        sa.Column("stability_override", sa.Float(), nullable=True),
    )
    op.add_column(
        "channel_voices",
        sa.Column("similarity_override", sa.Float(), nullable=True),
    )
    op.add_column(
        "channel_voices",
        sa.Column("style_override", sa.Float(), nullable=True),
    )
    op.add_column(
        "channel_voices",
        sa.Column("speed_override", sa.Float(), nullable=True),
    )
    op.add_column(
        "channel_voices",
        sa.Column(
            "use_speaker_boost",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "channel_voices",
        sa.Column("v3_stability_preset", sa.String(16), nullable=True),
    )
    op.add_column(
        "channel_voices",
        sa.Column(
            "speed_profile",
            sa.String(16),
            nullable=False,
            server_default="normal",
        ),
    )

    # ── channel_config: audio tags opt-in ────────────────────────────────────
    op.add_column(
        "channel_config",
        sa.Column(
            "audio_tags_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── audio_files: per-Short bookend path lists (added in Block 5) ────────
    op.add_column(
        "audio_files",
        sa.Column("short_rehook_paths", JSONB(), nullable=True),
    )
    op.add_column(
        "audio_files",
        sa.Column("short_bridge_paths", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audio_files", "short_bridge_paths")
    op.drop_column("audio_files", "short_rehook_paths")
    op.drop_column("channel_config", "audio_tags_enabled")
    op.drop_column("channel_voices", "speed_profile")
    op.drop_column("channel_voices", "v3_stability_preset")
    op.drop_column("channel_voices", "use_speaker_boost")
    op.drop_column("channel_voices", "speed_override")
    op.drop_column("channel_voices", "style_override")
    op.drop_column("channel_voices", "similarity_override")
    op.drop_column("channel_voices", "stability_override")
    op.drop_column("channel_voices", "elevenlabs_model")
