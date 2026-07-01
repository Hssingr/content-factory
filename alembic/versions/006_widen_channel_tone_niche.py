"""Widen channels.tone from VARCHAR(64) to VARCHAR(255).

Research Ideas can return descriptive tone strings longer than 64 chars,
causing StringDataRightTruncation on channel update. Also widens niche
from VARCHAR(128) to VARCHAR(255) for the same reason — research results
routinely produce compound niche descriptions.

Revision ID: 006
Revises: 005
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "channels", "tone",
        type_=sa.String(255),
        existing_type=sa.String(64),
        existing_nullable=False,
    )
    op.alter_column(
        "channels", "niche",
        type_=sa.String(255),
        existing_type=sa.String(128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "channels", "niche",
        type_=sa.String(128),
        existing_type=sa.String(255),
        existing_nullable=False,
    )
    op.alter_column(
        "channels", "tone",
        type_=sa.String(64),
        existing_type=sa.String(255),
        existing_nullable=False,
    )
