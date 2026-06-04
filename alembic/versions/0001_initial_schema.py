"""Initial schema — all tables

Revision ID: 0001
Revises:
Create Date: 2026-06-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("telegram_chat_id", sa.String(64), nullable=False),
        sa.Column("primary_language", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_telegram_chat_id", "users", ["telegram_chat_id"], unique=True)

    # ------------------------------------------------------------ proxy_config
    op.create_table(
        "proxy_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("proxy_url_encrypted", sa.Text, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    )

    # --------------------------------------------------------------- channels
    op.create_table(
        "channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("niche", sa.String(128), nullable=False),
        sa.Column("tone", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_channels_user_id", "channels", ["user_id"])

    # --------------------------------------------------------- channel_config
    op.create_table(
        "channel_config",
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("videos_per_week", sa.Integer, nullable=False, server_default="3"),
        sa.Column("shorts_rule", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("validation_timeout_hours", sa.Integer, nullable=False, server_default="24"),
        sa.Column("validation_max_revisions", sa.Integer, nullable=False, server_default="3"),
        sa.Column("validation_on_limit_reached", sa.String(16), nullable=False, server_default="auto_approve"),
        sa.Column("subtitle_style_main", sa.String(32), nullable=False, server_default="standard"),
        sa.Column("subtitle_style_shorts", sa.String(32), nullable=False, server_default="karaoke"),
        sa.Column("subtitle_karaoke_active_color", sa.String(16), nullable=False, server_default="#FFD700"),
        sa.Column("shorts_part_label_style", sa.String(32), nullable=False, server_default="default"),
        sa.Column("video_style_type", sa.String(64), nullable=False, server_default="documentary"),
        sa.Column("video_color_grade", sa.String(64), nullable=True),
        sa.Column("runway_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    # ------------------------------------------------------ channel_languages
    op.create_table(
        "channel_languages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("channel_name", sa.String(255), nullable=False),
    )
    op.create_unique_constraint("uq_channel_language", "channel_languages", ["channel_id", "language"])

    # -------------------------------------------------------- channel_voices
    op.create_table(
        "channel_voices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="elevenlabs"),
        sa.Column("voice_id", sa.String(128), nullable=False),
        sa.Column("emotion", sa.String(64), nullable=True),
        sa.Column("music_style", sa.String(64), nullable=True),
    )
    op.create_unique_constraint("uq_channel_voice_language", "channel_voices", ["channel_id", "language"])

    # ------------------------------------------------------- channel_sources
    op.create_table(
        "channel_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_value", sa.String(1024), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("trust_score", sa.Float, nullable=False, server_default="1.0"),
    )
    op.create_index("ix_channel_sources_channel_id", "channel_sources", ["channel_id"])

    # ----------------------------------------------------- channel_platforms
    op.create_table(
        "channel_platforms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("platform_channel_id", sa.String(256), nullable=True),
        sa.Column("credentials_encrypted", sa.Text, nullable=True),
        sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_unique_constraint("uq_channel_platform_language", "channel_platforms", ["channel_id", "language", "platform"])

    # ----------------------------------------------- channel_publish_timing
    op.create_table(
        "channel_publish_timing",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("optimal_days", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("optimal_hour_start", sa.Integer, nullable=False, server_default="9"),
        sa.Column("optimal_hour_end", sa.Integer, nullable=False, server_default="21"),
        sa.Column("shorts_spread_hours", sa.Integer, nullable=False, server_default="6"),
    )
    op.create_unique_constraint("uq_channel_timing_platform_language", "channel_publish_timing", ["channel_id", "platform", "language"])

    # ---------------------------------------------------------------- content
    op.create_table(
        "content",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("source_language", sa.String(10), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_content_hash", "content", ["content_hash"], unique=True)
    op.create_index("ix_content_channel_id", "content", ["channel_id"])
    op.create_index("ix_content_status", "content", ["status"])

    # --------------------------------------------------------------- scripts
    op.create_table(
        "scripts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("video_script", sa.Text, nullable=False),
        sa.Column("voice_script", sa.Text, nullable=False),
        sa.Column("estimated_duration_sec", sa.Float, nullable=True),
        sa.Column("shorts_breakpoints", postgresql.JSONB, nullable=True),
        sa.Column("validated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_unique_constraint("uq_script_content_language_version", "scripts", ["content_id", "language", "version"])

    # --------------------------------------------------- content_validations
    op.create_table(
        "content_validations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_message_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("revision_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("script_validation_status", sa.String(16), nullable=True),
        sa.Column("script_issues_log", postgresql.JSONB, nullable=True),
        sa.Column("self_correction_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint("uq_content_validation_content_id", "content_validations", ["content_id"])

    # ------------------------------------------------------------- audio_files
    op.create_table(
        "audio_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("shorts_breakpoints", postgresql.JSONB, nullable=True),
        sa.Column("whisper_transcript", postgresql.JSONB, nullable=True),
    )
    op.create_unique_constraint("uq_audio_content_language", "audio_files", ["content_id", "language"])

    # ---------------------------------------------------------- video_sections
    op.create_table(
        "video_sections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("section_order", sa.Integer, nullable=False),
        sa.Column("script_text", sa.Text, nullable=False),
        sa.Column("audio_start_ms", sa.Integer, nullable=False),
        sa.Column("audio_end_ms", sa.Integer, nullable=False),
        sa.Column("visual_source", sa.String(32), nullable=False),
        sa.Column("search_query", sa.Text, nullable=True),
        sa.Column("generation_prompt", sa.Text, nullable=True),
        sa.Column("effect", sa.String(64), nullable=True),
        sa.Column("color_grade", sa.String(64), nullable=True),
        sa.Column("runway_used", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("subagent_rounds", sa.Integer, nullable=False, server_default="1"),
        sa.Column("best_attempt_used", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_video_sections_content_id_language", "video_sections", ["content_id", "language"])

    # ---------------------------------------------------------- video_renders
    op.create_table(
        "video_renders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("short_order", sa.Integer, nullable=True),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("duration_seconds", sa.Float, nullable=False),
        sa.Column("hook_modified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("render_time_seconds", sa.Float, nullable=True),
    )
    op.create_index("ix_video_renders_content_id_language", "video_renders", ["content_id", "language"])

    # ------------------------------------------------------- publish_schedule
    op.create_table(
        "publish_schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proxy_region", sa.String(64), nullable=True),
        sa.Column("platform_video_id", sa.String(256), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="SCHEDULED"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text, nullable=True),
    )
    op.create_index("ix_publish_schedule_content_id", "publish_schedule", ["content_id"])
    op.create_index("ix_publish_schedule_status", "publish_schedule", ["status"])
    op.create_index("ix_publish_schedule_scheduled_at", "publish_schedule", ["scheduled_at"])

    # ------------------------------------------------------ video_analytics
    op.create_table(
        "video_analytics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("poll_type", sa.String(4), nullable=False),
        sa.Column("views", sa.Integer, nullable=False, server_default="0"),
        sa.Column("likes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("watch_time_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("avg_view_duration_pct", sa.Float, nullable=True),
        sa.Column("ctr", sa.Float, nullable=True),
        sa.Column("revenue_usd", sa.Float, nullable=True),
    )
    op.create_index("ix_video_analytics_content_id", "video_analytics", ["content_id"])
    op.create_index("ix_video_analytics_polled_at", "video_analytics", ["polled_at"])

    # --------------------------------------------------- analytics_anomalies
    op.create_table(
        "analytics_anomalies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("expected_value", sa.Float, nullable=False),
        sa.Column("actual_value", sa.Float, nullable=False),
        sa.Column("notified_user", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_analytics_anomalies_content_id", "analytics_anomalies", ["content_id"])


def downgrade() -> None:
    op.drop_table("analytics_anomalies")
    op.drop_table("video_analytics")
    op.drop_table("publish_schedule")
    op.drop_table("video_renders")
    op.drop_table("video_sections")
    op.drop_table("audio_files")
    op.drop_table("content_validations")
    op.drop_table("scripts")
    op.drop_table("content")
    op.drop_table("channel_publish_timing")
    op.drop_table("channel_platforms")
    op.drop_table("channel_sources")
    op.drop_table("channel_voices")
    op.drop_table("channel_languages")
    op.drop_table("channel_config")
    op.drop_table("channels")
    op.drop_table("proxy_config")
    op.drop_table("users")
