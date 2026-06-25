"""Drop scripts.video_script column.

video_script was always assembled identically to voice_script by
assemble_script() in the current (v4.0, blueprint-first) script generation
architecture — Agent 4 visual generation reads only voice_script and the
Whisper transcript, never video_script. Confirmed zero downstream consumers
across generation, persistence, validation, and rendering (Phase 9E-0).

Revision ID: 002
Revises: 001
Create Date: 2026-06-25
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('scripts', 'video_script')


def downgrade() -> None:
    op.add_column('scripts', sa.Column('video_script', sa.Text(), nullable=True))
