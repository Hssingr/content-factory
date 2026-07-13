"""Add channel_voices.gender (roadmap Phase D1 — gender-aware voice
configuration).

The operator's channels normally want a feminine narrating voice, but some
stories are centrally about (or narrated as) a male figure and should use a
masculine voice instead — automatically, not as a manual step. Today
`ChannelVoice` allows exactly one voice per (channel, language), enforced by
`uq_channel_voice_language`, which physically forbids a second row for the
same language. This migration widens the uniqueness key to
(channel_id, language, gender) so both a feminine and a masculine voice can
be configured per language; Agent 3 selects between them per content based
on the story blueprint's `protagonist_gender`.

Additive, backwards-compatible: every existing row backfills to
`gender='feminine'` via server_default — identical to what a brand-new row
gets — preserving current behavior for every already-configured channel
with zero manual data migration. The operator adds real masculine-voice
rows through the Agent 1 UI at their own pace; this migration does not
attempt to infer or assign any existing row's real gender.

Revision ID: 012
Revises: 011
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_voices",
        sa.Column("gender", sa.String(length=16), nullable=False, server_default="masculine"),
    )
    op.drop_constraint("uq_channel_voice_language", "channel_voices", type_="unique")
    op.create_unique_constraint(
        "uq_channel_voice_language_gender", "channel_voices", ["channel_id", "language", "gender"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_channel_voice_language_gender", "channel_voices", type_="unique")
    op.create_unique_constraint(
        "uq_channel_voice_language", "channel_voices", ["channel_id", "language"],
    )
    op.drop_column("channel_voices", "gender")
