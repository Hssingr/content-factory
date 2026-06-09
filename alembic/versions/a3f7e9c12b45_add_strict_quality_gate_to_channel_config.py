"""add strict_quality_gate to channel_config

Revision ID: a3f7e9c12b45
Revises: f989b4b22380
Create Date: 2026-06-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3f7e9c12b45'
down_revision: Union[str, None] = 'f989b4b22380'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'channel_config',
        sa.Column('strict_quality_gate', sa.Boolean(), server_default='false', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('channel_config', 'strict_quality_gate')
