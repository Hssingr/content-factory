import uuid
from sqlalchemy import String, Integer, Boolean, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ChannelConfig(Base):
    __tablename__ = "channel_config"

    # One-to-one with channels; channel_id is both FK and PK
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    videos_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    validation_timeout_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    validation_max_revisions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # 'auto_approve' | 'needs_review'
    validation_on_limit_reached: Mapped[str] = mapped_column(String(16), nullable=False, default="auto_approve")
    # Karaoke active-word color for Shorts subtitles (main video uses standard
    # subtitles, Shorts use karaoke — hardcoded by format in agent5 video.py).
    subtitle_karaoke_active_color: Mapped[str] = mapped_column(String(16), nullable=False, default="#FFD700")
    # 'youtube_long' | 'youtube_short' | 'tiktok' | 'reels'
    script_format: Mapped[str] = mapped_column(String(32), nullable=False, server_default="youtube_long")
    # Timestamp-mapping tolerance policy (repurposed name — the legacy section
    # splitter it originally gated was removed by roadmap 6.3). False (default):
    # treat >50% proportional-fallback beat timing as a mapping failure for that
    # language. True: accept the mapping regardless.
    allow_legacy_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Dead columns dropped by migration 009 (post-roadmap deep audit):
    # shorts_rule, subtitle_style_main, subtitle_style_shorts,
    # shorts_part_label_style, runway_enabled, strict_quality_gate,
    # video_style_type, video_color_grade — none had a live runtime reader.
    # visual_style (below) is the single channel-level style column.

    # ElevenLabs v3 audio tags (e.g. [dramatic pause], [whispers]).
    # Only meaningful when provider="elevenlabs" AND tts_model="eleven_v3".
    # False for all existing channels — safe default.
    audio_tags_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # ── Content Factory V3 groundwork (Phase Agent1-V3.2) ─────────────────────
    # Additive, defaulted columns only. None of these are read by Agent 2/3/4/5
    # yet — adding them here changes no runtime behavior; only Agent 1's CRUD
    # and the Pydantic schemas in app/schemas/channel.py read/write them today.
    # See CLAUDE.md §8.1 for the full allowed-values table, current-support
    # status, and the explicit "no downstream behavior change yet" contract.

    # 'single_story' (current/only supported) | 'limited_series' | 'ongoing_series'
    # (both reserved for a future phase — accepted by the schema, not executed).
    content_mode: Mapped[str] = mapped_column(String(32), nullable=False, server_default="single_story")
    # 'reddit' (current/only supported — matches Agent 2's existing discovery
    # default) | 'ai_generated' | 'user_provided' | 'hybrid' (reserved).
    script_source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="reddit")
    # 'youtube_and_shorts' (current/only supported — matches the existing
    # parent+standalone-shorts architecture) | 'youtube_long_only' | 'shorts_only'
    # (reserved).
    output_mode: Mapped[str] = mapped_column(String(32), nullable=False, server_default="youtube_and_shorts")
    # Free-form descriptor (no DB-level enum). The single channel-level visual
    # style column — the former duplicate video_style_type was reconciled away
    # by migration 009 (its consumer chain ended in an unread Remotion prop).
    visual_style: Mapped[str] = mapped_column(Text, nullable=False, server_default="story_driven")
    # Free-form descriptor for the Flux image-generation look.
    image_style: Mapped[str] = mapped_column(Text, nullable=False, server_default="photorealistic")
    # 'third_person' | 'first_person_storytime' — narration perspective/register
    # threaded into Agent 2 prompt boundaries alongside visual_style/image_style.
    narration_pov: Mapped[str] = mapped_column(String(32), nullable=False, server_default="third_person")

    channel: Mapped["Channel"] = relationship("Channel", back_populates="config")
