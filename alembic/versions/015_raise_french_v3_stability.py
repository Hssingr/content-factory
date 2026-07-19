"""Raise French ElevenLabs v3 voices to the robust stability preset.

Run e7782518 measured substantially more prosodic fragmentation in French than
English despite equivalent sentence structure. The operator selected the
existing per-voice stability control as the first experiment.

Revision ID: 015
Revises: 014
Create Date: 2026-07-16
"""

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE channel_voices "
        "SET v3_stability_preset = 'robust' "
        "WHERE lower(language) = 'fr' "
        "AND provider = 'elevenlabs' "
        "AND tts_model = 'eleven_v3' "
        "AND stability_override IS NULL"
    )


def downgrade() -> None:
    # Intentional no-op: prior per-row preset values are not recoverable, and
    # robust remains a valid setting on either side of this data migration.
    pass
