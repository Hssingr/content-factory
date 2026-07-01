"""Add channel_config_snapshot JSONB column to content.

Adds one additive, nullable JSONB column to `content`:
channel_config_snapshot.

Nullable, no server_default, no backfill — existing rows simply get NULL,
which is the correct "no snapshot was ever captured for this row" state.
No application code writes this column yet (Phase Agent1-V3.6 is the
foundation only — the builder/validator exist, but no generation-start
hook calls them) — see code_report/agent1_v3_6_config_snapshot_foundation.md
and CLAUDE.md §8.5.

Revision ID: 005
Revises: 004
Create Date: 2026-06-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content",
        sa.Column("channel_config_snapshot", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("content", "channel_config_snapshot")
