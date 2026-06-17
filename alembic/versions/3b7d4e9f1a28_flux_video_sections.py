"""Replace stock-fetcher columns with flux_prompt in video_sections

Revision ID: 3b7d4e9f1a28
Revises: 2a1c8e3f9d05
Create Date: 2026-06-15

Drops the stock-media columns that are no longer used (search_query, visual_source,
runway_used, subagent_rounds, best_attempt_used) and adds flux_prompt TEXT for the
Flux Schnell image generation prompt per beat. The language column is widened to 16
chars to accommodate the "__visual__" sentinel used for the shared visual pass.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '3b7d4e9f1a28'
down_revision: Union[str, None] = '2a1c8e3f9d05'
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop removed columns
    op.drop_column("video_sections", "search_query")
    op.drop_column("video_sections", "visual_source")
    op.drop_column("video_sections", "runway_used")
    op.drop_column("video_sections", "subagent_rounds")
    op.drop_column("video_sections", "best_attempt_used")

    # Add Flux prompt column
    op.add_column(
        "video_sections",
        sa.Column("flux_prompt", sa.Text, nullable=True),
    )

    # Widen language column to accommodate "__visual__" sentinel
    op.alter_column(
        "video_sections",
        "language",
        existing_type=sa.String(length=10),
        type_=sa.String(length=16),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "video_sections",
        "language",
        existing_type=sa.String(length=16),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.drop_column("video_sections", "flux_prompt")
    op.add_column(
        "video_sections",
        sa.Column("best_attempt_used", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "video_sections",
        sa.Column("subagent_rounds", sa.Integer, nullable=False, server_default="1"),
    )
    op.add_column(
        "video_sections",
        sa.Column("runway_used", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "video_sections",
        sa.Column("search_query", sa.Text, nullable=True),
    )
    op.add_column(
        "video_sections",
        sa.Column("visual_source", sa.String(32), nullable=False, server_default="pexels"),
    )
