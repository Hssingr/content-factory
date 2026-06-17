"""Switch TTS provider to Cartesia: rename elevenlabs_model→tts_model, set provider=cartesia

Revision ID: 8a5f2c9e4b17
Revises: 7e3d5b1f9a04
Create Date: 2026-06-15

Three changes in one pass:
  1. Rename column elevenlabs_model → tts_model (keeps all existing values intact).
  2. UPDATE channel_voices SET provider = 'cartesia' for all rows.
  3. UPDATE channel_voices SET tts_model = 'sonic-2' for all rows.
  4. Update the column server_default to 'sonic-2'.

Downgrade reverses these: rename back, reset provider to 'elevenlabs', reset
tts_model to 'eleven_v3', restore server_default.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '8a5f2c9e4b17'
down_revision: Union[str, None] = '7e3d5b1f9a04'
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename column
    op.alter_column("channel_voices", "elevenlabs_model", new_column_name="tts_model")

    # 2. Switch server_default
    op.alter_column(
        "channel_voices",
        "tts_model",
        server_default=sa.text("'sonic-2'"),
        existing_type=sa.String(64),
        existing_nullable=False,
    )

    # 3. Migrate all existing rows to Cartesia
    op.execute("UPDATE channel_voices SET provider = 'cartesia'")
    op.execute("UPDATE channel_voices SET tts_model = 'sonic-2'")


def downgrade() -> None:
    # Restore ElevenLabs values
    op.execute("UPDATE channel_voices SET provider = 'elevenlabs'")
    op.execute("UPDATE channel_voices SET tts_model = 'eleven_v3'")

    # Restore server_default
    op.alter_column(
        "channel_voices",
        "tts_model",
        server_default=sa.text("'eleven_v3'"),
        existing_type=sa.String(64),
        existing_nullable=False,
    )

    # Rename column back
    op.alter_column("channel_voices", "tts_model", new_column_name="elevenlabs_model")
