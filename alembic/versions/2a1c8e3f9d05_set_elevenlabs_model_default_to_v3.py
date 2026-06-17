"""Set elevenlabs_model default to eleven_v3

Revision ID: 2a1c8e3f9d05
Revises: f989b4b22380
Create Date: 2026-06-15

Changes the server_default on channel_voices.elevenlabs_model from
"eleven_multilingual_v2" to "eleven_v3". Existing rows are NOT updated —
only new rows get the new default. Channels on the old model continue working;
tasks.py logs a WARNING when a non-v3 voice is detected.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '2a1c8e3f9d05'
down_revision: Union[str, None] = 'f989b4b22380'
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "channel_voices",
        "elevenlabs_model",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default="eleven_v3",
    )


def downgrade() -> None:
    op.alter_column(
        "channel_voices",
        "elevenlabs_model",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default="eleven_multilingual_v2",
    )
