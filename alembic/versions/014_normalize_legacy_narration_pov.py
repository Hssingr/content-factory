"""Normalize the legacy ``first_person`` narration POV token.

The supported runtime/schema value has always been
``first_person_storytime``, but one production channel row carried the older
``first_person`` token. Agent 2 now also normalizes at read time; this migration
repairs persisted data so all later readers see the canonical value directly.

Revision ID: 014
Revises: 013
Create Date: 2026-07-15
"""

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE channel_config "
        "SET narration_pov = 'first_person_storytime' "
        "WHERE lower(trim(narration_pov)) = 'first_person'"
    )


def downgrade() -> None:
    # Intentional no-op: the canonical token is valid before and after this
    # data repair, and provenance is unavailable to distinguish repaired rows
    # from rows legitimately created as first_person_storytime afterward.
    pass
