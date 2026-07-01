"""Add Content Factory V3 groundwork fields to channel_config.

Adds five additive, server-defaulted columns to channel_config:
content_mode, script_source, output_mode, visual_style, image_style.

NOT NULL with a server_default — existing rows are backfilled by Postgres
at ALTER TABLE time with the same default every new row gets, so this is
safe to run against a table with existing data with no manual backfill
step and no row left in an inconsistent state.

None of these columns are read by Agent 2/3/4/5 yet — see
code_report/agent1_v3_2_schema_model_groundwork.md and CLAUDE.md §8.1.

Revision ID: 004
Revises: 003
Create Date: 2026-06-28
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_config",
        sa.Column("content_mode", sa.String(length=32), nullable=False, server_default="single_story"),
    )
    op.add_column(
        "channel_config",
        sa.Column("script_source", sa.String(length=32), nullable=False, server_default="reddit"),
    )
    op.add_column(
        "channel_config",
        sa.Column("output_mode", sa.String(length=32), nullable=False, server_default="youtube_and_shorts"),
    )
    op.add_column(
        "channel_config",
        sa.Column("visual_style", sa.String(length=64), nullable=False, server_default="documentary"),
    )
    op.add_column(
        "channel_config",
        sa.Column("image_style", sa.String(length=64), nullable=False, server_default="photorealistic"),
    )


def downgrade() -> None:
    op.drop_column("channel_config", "image_style")
    op.drop_column("channel_config", "visual_style")
    op.drop_column("channel_config", "output_mode")
    op.drop_column("channel_config", "script_source")
    op.drop_column("channel_config", "content_mode")
