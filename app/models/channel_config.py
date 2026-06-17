import uuid
from sqlalchemy import String, Integer, Boolean, ForeignKey
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
    # 'always' | 'never' | 'auto'
    shorts_rule: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    validation_timeout_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    validation_max_revisions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # 'auto_approve' | 'needs_review'
    validation_on_limit_reached: Mapped[str] = mapped_column(String(16), nullable=False, default="auto_approve")
    subtitle_style_main: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    subtitle_style_shorts: Mapped[str] = mapped_column(String(32), nullable=False, default="karaoke")
    subtitle_karaoke_active_color: Mapped[str] = mapped_column(String(16), nullable=False, default="#FFD700")
    shorts_part_label_style: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    video_style_type: Mapped[str] = mapped_column(String(64), nullable=False, default="documentary")
    video_color_grade: Mapped[str] = mapped_column(String(64), nullable=True)
    # Runway is the absolute last resort; disabled by default
    runway_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 'youtube_long' | 'youtube_short' | 'tiktok' | 'reels'
    script_format: Mapped[str] = mapped_column(String(32), nullable=False, server_default="youtube_long")
    # Storyboard failure policy. False (default): stop language generation with an
    # explicit error when the Storyboard Agent fails — never silently fall back to
    # the legacy section splitter (silent fallback was masking a 100% storyboard
    # failure rate). True: restore the previous silent-fallback behavior.
    allow_legacy_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Quality gate policy. False (default): block render only on HIGH-severity viewer
    # experience issues (visuals category) that remain after the repair pass — non-blocking
    # categories (intro, audio, captions, pacing) never block rendering.
    # True: block on any NEEDS_FIXES verdict that survives the repair pass.
    strict_quality_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # ElevenLabs v3 audio tags (e.g. [dramatic pause], [whispers]).
    # Only meaningful when provider="elevenlabs" AND tts_model="eleven_v3".
    # False for all existing channels — safe default.
    audio_tags_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    channel: Mapped["Channel"] = relationship("Channel", back_populates="config")
