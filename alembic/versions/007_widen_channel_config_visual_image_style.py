"""Widen channel_config.visual_style and image_style from VARCHAR(64) to TEXT.

Research Ideas returns multi-word descriptive strings for both fields that
routinely exceed 64 characters, causing StringDataRightTruncation on config
upsert.

Revision ID: 007
Revises: 006
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "channel_config", "visual_style",
        type_=sa.Text,
        existing_type=sa.String(64),
        existing_nullable=False,
        existing_server_default="documentary",
    )
    op.alter_column(
        "channel_config", "image_style",
        type_=sa.Text,
        existing_type=sa.String(64),
        existing_nullable=False,
        existing_server_default="photorealistic",
    )


def downgrade() -> None:
    op.alter_column(
        "channel_config", "image_style",
        type_=sa.String(64),
        existing_type=sa.Text,
        existing_nullable=False,
        existing_server_default="photorealistic",
    )
    op.alter_column(
        "channel_config", "visual_style",
        type_=sa.String(64),
        existing_type=sa.Text,
        existing_nullable=False,
        existing_server_default="documentary",
    )
