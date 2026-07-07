"""Add content.updated_at for stuck-state recovery (fresh full-system audit §1.2).

The Beat pickups poll only stable statuses, so content whose worker died
mid-stage (GENERATING_SCRIPTS / GENERATING_AUDIO / GENERATING_VISUALS /
RENDERING) was stranded forever — nothing re-dispatched it. Recovery needs a
staleness signal: `updated_at` is touched by every ORM UPDATE (status
transitions included), so a row whose in-progress status has not moved for
longer than its stage's recovery threshold can be safely re-dispatched.

Backfill: server_default=now() stamps existing rows at migration time, which
is exactly the conservative baseline the sweep needs (nothing looks stale
immediately after upgrading).
"""

from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content", sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ))


def downgrade() -> None:
    op.drop_column("content", "updated_at")
