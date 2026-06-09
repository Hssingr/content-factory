"""add allow_legacy_fallback to channel_config

Revision ID: f989b4b22380
Revises: 5f9aee82d895
Create Date: 2026-06-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f989b4b22380'
down_revision: Union[str, None] = '5f9aee82d895'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'channel_config',
        sa.Column('allow_legacy_fallback', sa.Boolean(), server_default='false', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('channel_config', 'allow_legacy_fallback')
