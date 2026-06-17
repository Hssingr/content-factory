"""Backfill elevenlabs_model to eleven_v3 for all existing channel_voices rows

Revision ID: 4c2f8a1e6b93
Revises: 3b7d4e9f1a28
Create Date: 2026-06-15

The 2a1c8e3f9d05 migration set server_default='eleven_v3' for new rows but did
not update existing rows that still carry 'eleven_multilingual_v2'.  This
migration sets all rows to 'eleven_v3'.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = '4c2f8a1e6b93'
down_revision: Union[str, None] = '3b7d4e9f1a28'
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE channel_voices SET elevenlabs_model = 'eleven_v3' "
        "WHERE elevenlabs_model != 'eleven_v3'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE channel_voices SET elevenlabs_model = 'eleven_multilingual_v2' "
        "WHERE elevenlabs_model = 'eleven_v3'"
    )
