"""Merge the source_excerpt branch with the flux+backfill branch

Revision ID: 7e3d5b1f9a04
Revises: 1e6a4f2a4d97, 4c2f8a1e6b93
Create Date: 2026-06-15
"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = '7e3d5b1f9a04'
down_revision: Union[str, tuple[str, ...]] = ('1e6a4f2a4d97', '4c2f8a1e6b93')
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
