import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class PublishSchedule(Base):
    __tablename__ = "publish_schedule"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proxy_region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_video_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    platform_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # SCHEDULED | PUBLISHING | PUBLISHED | FAILED | CANCELLED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="SCHEDULED")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="publish_schedules")
