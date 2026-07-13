"""Add audio_files.section_boundaries (roadmap Phase B1 — per-language visual
timing for parent renders).

The whole-video proportional stretch (`_remap_beats_timing()`) that maps
shared parent visual beats onto a non-source language's own audio duration
assumes narration pacing is uniform across the whole video — it isn't, so
non-source-language visuals drift out of sync with narration over the
video's length (measured up to ±20s in a real production run). Each
language's real per-section ([INTRO]/[SECTION N]/[OUTRO]) audio boundaries,
captured deterministically at TTS-generation time from real per-section
chunk durations, let beats be remapped section-by-section instead of
video-wide, bounding drift to within one section (~2-3s).

Nullable, additive, no backfill: existing AudioFile rows simply have
section_boundaries=NULL, which the Agent 4 remap function already treats as
"no section data available" and falls back to the pre-existing whole-video
stretch for — no regression for already-generated content.

Revision ID: 011
Revises: 010
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audio_files",
        sa.Column("section_boundaries", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audio_files", "section_boundaries")
