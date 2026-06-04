import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Content(Base):
    __tablename__ = "content"
    __table_args__ = (Index("ix_content_hash", "content_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_language: Mapped[str] = mapped_column(String(10), nullable=False)
    # SHA-256(URL + title) for deduplication
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # pipeline state machine — see CLAUDE.md §Agent descriptions for full list
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    channel: Mapped["Channel"] = relationship("Channel", back_populates="contents")
    scripts: Mapped[list["Script"]] = relationship("Script", back_populates="content")
    validation: Mapped["ContentValidation"] = relationship("ContentValidation", back_populates="content", uselist=False)
    audio_files: Mapped[list["AudioFile"]] = relationship("AudioFile", back_populates="content")
    video_sections: Mapped[list["VideoSection"]] = relationship("VideoSection", back_populates="content")
    video_renders: Mapped[list["VideoRender"]] = relationship("VideoRender", back_populates="content")
    publish_schedules: Mapped[list["PublishSchedule"]] = relationship("PublishSchedule", back_populates="content")
    metadata_entries: Mapped[list["VideoMetadata"]] = relationship("VideoMetadata", back_populates="content")
    analytics: Mapped[list["VideoAnalytics"]] = relationship("VideoAnalytics", back_populates="content")
    anomalies: Mapped[list["AnalyticsAnomaly"]] = relationship("AnalyticsAnomaly", back_populates="content")
