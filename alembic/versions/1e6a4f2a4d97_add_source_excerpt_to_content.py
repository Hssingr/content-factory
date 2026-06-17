"""add_source_excerpt_to_content

Revision ID: 1e6a4f2a4d97
Revises: 0006
Create Date: 2026-06-12 22:57:56.986376

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '1e6a4f2a4d97'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("content", sa.Column("source_excerpt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("content", "source_excerpt")
