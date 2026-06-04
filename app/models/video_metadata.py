import uuid
from datetime import datetime
from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.database import Base


class VideoMetadata(Base):
    __tablename__ = "video_metadata"
    __table_args__ = (UniqueConstraint("content_id", "language", "platform", name="uq_video_metadata_content_lang_platform"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Platform-specific hashtags list e.g. ["#news", "#viral"]
    hashtags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    thumbnail_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_uploaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    content: Mapped["Content"] = relationship("Content", back_populates="metadata_entries")
