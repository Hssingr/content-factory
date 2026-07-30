"""Fix channel_voices.gender's server_default (inverted in migration 012).

Migration 012 added `gender` with `server_default="masculine"`, contradicting
its own docstring ("every existing row backfills to gender='feminine'") and
every other place in the codebase that assumes an unset/legacy voice is
feminine (app/schemas/channel.py's VoiceEntry default, the setup UI's
restore-on-load fallback, Agent 3's protagonist-gender normalization). See
code_report/agent1_frontend_backend_audit_2026_07_30.md for the full
analysis.

This migration only corrects the COLUMN DEFAULT going forward — it does not
rewrite any existing row's `gender` value. Every row inserted through the
application's real code path (`replace_voices()`) always sets `gender`
explicitly per entry, so this default is only ever exercised by the initial
`ALTER TABLE ... ADD COLUMN` backfill migration 012 itself performed once.
Whether any given already-migrated database has rows that were backfilled to
'masculine' by that bug (and are actually feminine-intended) cannot be
determined here without risking silently overwriting a row an operator
genuinely and deliberately set to masculine after the fact (e.g. by
re-saving the Voices step once the mislabeled row was already showing under
the "Masculine Voice" card). That data question is left for manual review by
whoever operates the affected database, not decided automatically by this
migration.

Revision ID: 016
Revises: 015
Create Date: 2026-07-30
"""

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE channel_voices ALTER COLUMN gender SET DEFAULT 'feminine'")


def downgrade() -> None:
    op.execute("ALTER TABLE channel_voices ALTER COLUMN gender SET DEFAULT 'masculine'")
