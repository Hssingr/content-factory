"""Add use_case column to channel_voices

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("channel_voices", sa.Column("use_case", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("channel_voices", "use_case")
