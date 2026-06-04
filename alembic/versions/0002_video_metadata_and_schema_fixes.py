"""Add video_metadata table, drop video_renders.file_path, add publish_schedule.platform_title

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("hashtags", postgresql.JSONB, nullable=True),
        sa.Column("thumbnail_file_path", sa.Text, nullable=True),
        sa.Column("thumbnail_uploaded", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_video_metadata_content_lang_platform",
        "video_metadata",
        ["content_id", "language", "platform"],
    )
    op.create_index("ix_video_metadata_content_id", "video_metadata", ["content_id"])

    op.drop_column("video_renders", "file_path")

    op.add_column("publish_schedule", sa.Column("platform_title", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("publish_schedule", "platform_title")

    # Restoring file_path as nullable — cannot recover original NOT NULL constraint without data
    op.add_column("video_renders", sa.Column("file_path", sa.Text, nullable=True))

    op.drop_index("ix_video_metadata_content_id", table_name="video_metadata")
    op.drop_constraint("uq_video_metadata_content_lang_platform", "video_metadata", type_="unique")
    op.drop_table("video_metadata")
