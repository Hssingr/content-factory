"""Initial schema.

Revision ID: 001
Revises:
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'proxy_config',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('region', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('proxy_url_encrypted', sa.Text(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
    )

    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('telegram_chat_id', sa.String(length=64), nullable=False, unique=True),
        sa.Column('primary_language', sa.String(length=10), nullable=False),
        sa.Column('pipeline_run_hour', sa.Integer(), nullable=False),
        sa.Column('pipeline_timezone', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=False), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        'channels',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('niche', sa.String(length=128), nullable=False),
        sa.Column('tone', sa.String(length=64), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
    )

    op.create_table(
        'channel_config',
        sa.Column('channel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('channels.id', ondelete='CASCADE'), primary_key=True, nullable=False),
        sa.Column('videos_per_week', sa.Integer(), nullable=False),
        sa.Column('shorts_rule', sa.String(length=16), nullable=False),
        sa.Column('validation_timeout_hours', sa.Integer(), nullable=False),
        sa.Column('validation_max_revisions', sa.Integer(), nullable=False),
        sa.Column('validation_on_limit_reached', sa.String(length=16), nullable=False),
        sa.Column('subtitle_style_main', sa.String(length=32), nullable=False),
        sa.Column('subtitle_style_shorts', sa.String(length=32), nullable=False),
        sa.Column('subtitle_karaoke_active_color', sa.String(length=16), nullable=False),
        sa.Column('shorts_part_label_style', sa.String(length=32), nullable=False),
        sa.Column('video_style_type', sa.String(length=64), nullable=False),
        sa.Column('video_color_grade', sa.String(length=64)),
        sa.Column('runway_enabled', sa.Boolean(), nullable=False),
        sa.Column('script_format', sa.String(length=32), nullable=False, server_default='youtube_long'),
        sa.Column('allow_legacy_fallback', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('strict_quality_gate', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('audio_tags_enabled', sa.Boolean(), nullable=False, server_default='false'),
    )

    op.create_table(
        'channel_languages',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('channel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('channels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('channel_name', sa.String(length=255), nullable=False),
        sa.UniqueConstraint('channel_id', 'language', name='uq_channel_language'),
    )

    op.create_table(
        'channel_platforms',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('channel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('channels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('platform_channel_id', sa.String(length=256)),
        sa.Column('credentials_encrypted', sa.Text()),
        sa.Column('verified', sa.Boolean(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('channel_id', 'language', 'platform', name='uq_channel_platform_language'),
    )

    op.create_table(
        'channel_publish_timing',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('channel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('channels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('timezone', sa.String(length=64), nullable=False),
        sa.Column('optimal_days', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('optimal_hour_start', sa.Integer(), nullable=False),
        sa.Column('optimal_hour_end', sa.Integer(), nullable=False),
        sa.Column('shorts_spread_hours', sa.Integer(), nullable=False),
        sa.UniqueConstraint('channel_id', 'platform', 'language', name='uq_channel_timing_platform_language'),
    )

    op.create_table(
        'channel_sources',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('channel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('channels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_type', sa.String(length=32), nullable=False),
        sa.Column('source_value', sa.String(length=1024), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('trust_score', sa.Float(), nullable=False),
    )

    op.create_table(
        'channel_voices',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('channel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('channels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('voice_id', sa.String(length=128), nullable=False),
        sa.Column('emotion', sa.String(length=64)),
        sa.Column('music_style', sa.String(length=64)),
        sa.Column('use_case', sa.String(length=64)),
        sa.Column('tts_model', sa.String(length=64), nullable=False, server_default='sonic-2'),
        sa.Column('stability_override', sa.Float()),
        sa.Column('similarity_override', sa.Float()),
        sa.Column('style_override', sa.Float()),
        sa.Column('speed_override', sa.Float()),
        sa.Column('use_speaker_boost', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('v3_stability_preset', sa.String(length=16)),
        sa.Column('speed_profile', sa.String(length=16), nullable=False, server_default='normal'),
        sa.UniqueConstraint('channel_id', 'language', name='uq_channel_voice_language'),
    )

    op.create_table(
        'content',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('channel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('channels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('source_language', sa.String(length=10), nullable=False),
        sa.Column('content_hash', sa.String(length=255), nullable=False, unique=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('source_excerpt', sa.Text()),
        sa.Column('story_blueprint', postgresql.JSONB(astext_type=sa.Text())),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('published_at', sa.DateTime(timezone=True)),
        sa.Column('is_short_episode', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('parent_content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='SET NULL')),
        sa.Column('short_part_number', sa.Integer()),
        sa.Column('short_total_parts', sa.Integer()),
    )

    op.create_table(
        'analytics_anomalies',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('metric', sa.String(length=64), nullable=False),
        sa.Column('expected_value', sa.Float(), nullable=False),
        sa.Column('actual_value', sa.Float(), nullable=False),
        sa.Column('notified_user', sa.Boolean(), nullable=False),
    )

    op.create_table(
        'audio_files',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('file_path', sa.Text(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('whisper_transcript', postgresql.JSONB(astext_type=sa.Text())),
        sa.UniqueConstraint('content_id', 'language', name='uq_audio_content_language'),
    )

    op.create_table(
        'content_validations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('telegram_message_id', sa.String(length=64)),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('revision_count', sa.Integer(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('timeout_at', sa.DateTime(timezone=True)),
        sa.Column('script_validation_status', sa.String(length=16)),
        sa.Column('script_issues_log', postgresql.JSONB(astext_type=sa.Text())),
        sa.Column('self_correction_attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        'publish_schedule',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True)),
        sa.Column('proxy_region', sa.String(length=64)),
        sa.Column('platform_video_id', sa.String(length=256)),
        sa.Column('platform_title', sa.String(length=512)),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('failure_reason', sa.Text()),
    )

    op.create_table(
        'scripts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('video_script', sa.Text(), nullable=False),
        sa.Column('voice_script', sa.Text(), nullable=False),
        sa.Column('estimated_duration_sec', sa.Float()),
        sa.Column('validated', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.UniqueConstraint('content_id', 'language', 'version', name='uq_script_content_language_version'),
    )

    op.create_table(
        'video_analytics',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('polled_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column('poll_type', sa.String(length=4), nullable=False),
        sa.Column('views', sa.Integer(), nullable=False),
        sa.Column('likes', sa.Integer(), nullable=False),
        sa.Column('watch_time_seconds', sa.Integer(), nullable=False),
        sa.Column('avg_view_duration_pct', sa.Float()),
        sa.Column('ctr', sa.Float()),
        sa.Column('revenue_usd', sa.Float()),
    )

    op.create_table(
        'video_metadata',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('hashtags', postgresql.JSONB(astext_type=sa.Text())),
        sa.Column('thumbnail_file_path', sa.Text()),
        sa.Column('thumbnail_uploaded', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint('content_id', 'language', 'platform', name='uq_video_metadata_content_lang_platform'),
    )

    op.create_table(
        'video_renders',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('format', sa.String(length=8), nullable=False),
        sa.Column('short_order', sa.Integer()),
        sa.Column('duration_seconds', sa.Float(), nullable=False),
        sa.Column('render_time_seconds', sa.Float()),
    )

    op.create_table(
        'video_sections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('content_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content.id', ondelete='CASCADE'), nullable=False),
        sa.Column('language', sa.String(length=16), nullable=False),
        sa.Column('section_order', sa.Integer(), nullable=False),
        sa.Column('script_text', sa.Text(), nullable=False),
        sa.Column('audio_start_ms', sa.Integer(), nullable=False),
        sa.Column('audio_end_ms', sa.Integer(), nullable=False),
        sa.Column('flux_prompt', sa.Text()),
        sa.Column('effect', sa.String(length=64)),
        sa.Column('color_grade', sa.String(length=64)),
        sa.Column('generation_prompt', sa.Text()),
        sa.Column('beat_intensity', sa.String(length=16)),
        sa.Column('suggested_duration_sec', sa.Float()),
        sa.Column('media_strategy', sa.String(length=32)),
        sa.Column('text_card_style', sa.String(length=32)),
    )

    op.create_index('ix_content_hash', 'content', ['content_hash'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_content_hash', table_name='content')
    op.drop_table('video_sections')
    op.drop_table('video_renders')
    op.drop_table('video_metadata')
    op.drop_table('video_analytics')
    op.drop_table('scripts')
    op.drop_table('publish_schedule')
    op.drop_table('content_validations')
    op.drop_table('audio_files')
    op.drop_table('analytics_anomalies')
    op.drop_table('content')
    op.drop_table('channel_voices')
    op.drop_table('channel_sources')
    op.drop_table('channel_publish_timing')
    op.drop_table('channel_platforms')
    op.drop_table('channel_languages')
    op.drop_table('channel_config')
    op.drop_table('channels')
    op.drop_table('users')
    op.drop_table('proxy_config')
