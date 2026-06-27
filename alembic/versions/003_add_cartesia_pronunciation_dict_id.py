"""Add Cartesia pronunciation dictionary id to channel voices.

Revision ID: 003
Revises: 002
Create Date: 2026-06-27
"""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_voices",
        sa.Column("cartesia_pronunciation_dict_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_voices", "cartesia_pronunciation_dict_id")
