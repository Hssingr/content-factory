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

    channel: Mapped["Channel"] = relationship("Channel", back_populates="config")
