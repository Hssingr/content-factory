"""Add Agent 6 metadata-pipeline fields.

Agent 6 roadmap Phase B (code_report/agent6_metadata_roadmap.md) — additive
scaffolding only, no runtime code reads or writes these columns yet.

content.metadata_generated_at: set once Agent 6 finishes generating metadata
for a content item (the moment Content.status moves to
METADATA_PENDING_APPROVAL) — the timestamp check_metadata_auto_approve()
(Phase E) compares against settings.metadata_auto_approve_seconds. NULL for
every existing row and for any row that hasn't reached that stage yet.

video_metadata.generated_at: optional telemetry parity with
AudioFile.generated_at (migration 013) — not load-bearing for any decision.

video_metadata.thumbnail_text: the short overlay phrase Pillow burns onto the
platform="youtube" row's thumbnail (Check 1 v2 decision — distinct from
`title`, which is never rendered onto the image). NULL on every non-youtube
platform row and on any youtube row whose thumbnail generation was
skipped/failed.

Revision ID: 017
Revises: 016
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content",
        sa.Column("metadata_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "video_metadata",
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "video_metadata",
        sa.Column("thumbnail_text", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("video_metadata", "thumbnail_text")
    op.drop_column("video_metadata", "generated_at")
    op.drop_column("content", "metadata_generated_at")
