"""Add pipeline_run_hour and pipeline_timezone to users

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Hour (0-23) in local time at which to trigger story generation on D-1
    op.add_column("users", sa.Column("pipeline_run_hour", sa.Integer, nullable=False, server_default="18"))
    # IANA timezone for interpreting pipeline_run_hour (e.g. "Europe/Paris")
    op.add_column("users", sa.Column("pipeline_timezone", sa.String(64), nullable=False, server_default="UTC"))


def downgrade() -> None:
    op.drop_column("users", "pipeline_timezone")
    op.drop_column("users", "pipeline_run_hour")
