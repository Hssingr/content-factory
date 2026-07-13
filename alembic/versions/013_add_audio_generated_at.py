"""Add audio_files.generated_at for chronological WPM calibration.

UUIDv4 audio_files.id values are random, so ordering them descending did not
produce the rolling "most recent" sample window promised by script_estimator.

Revision ID: 013
Revises: 012
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audio_files",
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("audio_files", "generated_at")
