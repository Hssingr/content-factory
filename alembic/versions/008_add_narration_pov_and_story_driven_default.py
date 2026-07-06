"""Add channel_config.narration_pov; update visual_style/video_style_type defaults.

Roadmap 4a / audit P1-9 (code_report/forensic_output_audit_borrasca_run.md):
the pipeline's register was configured AND hardcoded to "documentary" — this
migration is the schema half of the fix.

1. Adds `narration_pov` ('third_person' | 'first_person_storytime') — a new,
   additive, server-defaulted column threaded into Agent 2 script prompts
   alongside visual_style/image_style. NOT NULL with a server_default —
   existing rows are backfilled by Postgres at ALTER TABLE time with the
   same default every new row gets, so this is safe against a table with
   existing data.
2. Updates the server_default of `visual_style` and `video_style_type` from
   "documentary" to "story_driven" (matching the model/schema defaults
   changed in the same phase). This only changes the default applied to a
   column when no value is supplied — it does not rewrite any existing row's
   already-persisted value, exactly like every prior server_default-only
   migration in this history (e.g. 004).

Revision ID: 008
Revises: 007
"""

from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_config",
        sa.Column(
            "narration_pov", sa.String(length=32), nullable=False,
            server_default="third_person",
        ),
    )
    op.alter_column(
        "channel_config", "visual_style",
        server_default="story_driven",
        existing_type=sa.Text,
        existing_nullable=False,
    )
    op.alter_column(
        "channel_config", "video_style_type",
        server_default="story_driven",
        existing_type=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "channel_config", "video_style_type",
        server_default="documentary",
        existing_type=sa.String(64),
        existing_nullable=False,
    )
    op.alter_column(
        "channel_config", "visual_style",
        server_default="documentary",
        existing_type=sa.Text,
        existing_nullable=False,
    )
    op.drop_column("channel_config", "narration_pov")
