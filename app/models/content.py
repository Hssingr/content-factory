import uuid
from datetime import datetime
from sqlalchemy import Boolean, Integer, String, Text, DateTime, ForeignKey, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.database import Base


class Content(Base):
    __tablename__ = "content"
    __table_args__ = (Index("ix_content_hash", "content_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_language: Mapped[str] = mapped_column(String(10), nullable=False)
    # SHA-256(URL + title) for deduplication; short episodes append "_short_N" (up to 72 chars)
    content_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # pipeline state machine — see CLAUDE.md §Agent descriptions for full list
    # longest status: SCRIPTS_VALIDATED_AWAITING_PARENT (33 chars) → VARCHAR(64)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="DRAFT")
    # raw source body stored at discovery time (≤8 000 chars) — used by Agent 3
    # auto_correct_script() to expand minimum_length corrections from source facts
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Blueprint JSON persisted immediately after generate_story_blueprint() — used as constraint
    # for per-section generation and stored with visual_intent_history for Agent 4 visual continuity.
    story_blueprint: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Short episode fields — null on long-form content rows
    is_short_episode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    parent_content_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="SET NULL"), nullable=True)
    short_part_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    short_total_parts: Mapped[int | None] = mapped_column(Integer, nullable=True)

    channel: Mapped["Channel"] = relationship("Channel", back_populates="contents")
    parent_content: Mapped["Content | None"] = relationship("Content", remote_side="Content.id", foreign_keys="Content.parent_content_id", back_populates="short_episodes")
    short_episodes: Mapped[list["Content"]] = relationship("Content", foreign_keys="Content.parent_content_id", back_populates="parent_content")
    scripts: Mapped[list["Script"]] = relationship("Script", back_populates="content")
    validation: Mapped["ContentValidation"] = relationship("ContentValidation", back_populates="content", uselist=False)
    audio_files: Mapped[list["AudioFile"]] = relationship("AudioFile", back_populates="content")
    video_sections: Mapped[list["VideoSection"]] = relationship("VideoSection", back_populates="content")
    video_renders: Mapped[list["VideoRender"]] = relationship("VideoRender", back_populates="content")
    publish_schedules: Mapped[list["PublishSchedule"]] = relationship("PublishSchedule", back_populates="content")
    metadata_entries: Mapped[list["VideoMetadata"]] = relationship("VideoMetadata", back_populates="content")
    analytics: Mapped[list["VideoAnalytics"]] = relationship("VideoAnalytics", back_populates="content")
    anomalies: Mapped[list["AnalyticsAnomaly"]] = relationship("AnalyticsAnomaly", back_populates="content")
