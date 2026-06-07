"""add script_format to channel_config

Revision ID: 5f9aee82d895
Revises: 0005
Create Date: 2026-06-07 13:23:45.389243

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '5f9aee82d895'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'channel_config',
        sa.Column('script_format', sa.String(length=32), server_default='youtube_long', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('channel_config', 'script_format')
