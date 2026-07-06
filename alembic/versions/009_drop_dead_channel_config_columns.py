"""Drop dead channel_config columns (post-roadmap deep audit follow-up).

Eight columns were persisted but read by nothing on any live runtime path:

- shorts_rule            — never read; Shorts on/off is owned by output_mode
                           ("youtube_long_only" skips the planner). The UI
                           dropdown was removed by the deep audit.
- subtitle_style_main    — subtitle styles are hardcoded by format (main =
- subtitle_style_shorts    standard, Shorts = karaoke) in agent5 video.py;
                           the columns were never consulted.
- shorts_part_label_style — the Shorts "Part N of M" label is controlled by
                           the env-level settings.short_part_label_enabled
                           flag, never by this column.
- runway_enabled         — no Runway integration exists anywhere in the
                           codebase.
- strict_quality_gate    — configured the AI render quality gate deleted by
                           the Elimination Mandate; nothing reads it.
- video_style_type       — duplicate of visual_style (CLAUDE.md §8.1 flagged
                           the pair for reconciliation). Its only consumer
                           chain (video.py channel_style → props
                           config.style) ended in a Remotion prop no
                           component ever read. visual_style (the real
                           Agent 2/4 prompt input) is the single style
                           column now.
- video_color_grade      — same dead-end chain (props config.color_grade,
                           unread); the render-active color grade is the
                           per-beat VideoSection color_grade (§11A.6).

Additive-only reversal: downgrade re-adds every column with the same
defaults migrations 001/008 last gave them, backfilled for existing rows.
"""

from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

_DROPPED = (
    "shorts_rule",
    "subtitle_style_main",
    "subtitle_style_shorts",
    "shorts_part_label_style",
    "runway_enabled",
    "strict_quality_gate",
    "video_style_type",
    "video_color_grade",
)


def upgrade() -> None:
    for column in _DROPPED:
        op.drop_column("channel_config", column)


def downgrade() -> None:
    op.add_column("channel_config", sa.Column(
        "shorts_rule", sa.String(length=16), nullable=False, server_default="auto",
    ))
    op.add_column("channel_config", sa.Column(
        "subtitle_style_main", sa.String(length=32), nullable=False, server_default="standard",
    ))
    op.add_column("channel_config", sa.Column(
        "subtitle_style_shorts", sa.String(length=32), nullable=False, server_default="karaoke",
    ))
    op.add_column("channel_config", sa.Column(
        "shorts_part_label_style", sa.String(length=32), nullable=False, server_default="default",
    ))
    op.add_column("channel_config", sa.Column(
        "runway_enabled", sa.Boolean(), nullable=False, server_default="false",
    ))
    op.add_column("channel_config", sa.Column(
        "strict_quality_gate", sa.Boolean(), nullable=False, server_default="false",
    ))
    op.add_column("channel_config", sa.Column(
        # 008 changed this column's default to story_driven — restore that state.
        "video_style_type", sa.String(length=64), nullable=False, server_default="story_driven",
    ))
    op.add_column("channel_config", sa.Column(
        "video_color_grade", sa.String(length=64), nullable=True,
    ))
