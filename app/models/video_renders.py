import uuid
from sqlalchemy import String, Integer, Float, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class VideoRender(Base):
    __tablename__ = "video_renders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    # 'main' (16:9) | 'short' (9:16)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    # Position within Shorts sequence; null for main video
    short_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    # Whether the first 3s hook was optimised by Shorts Cutter sub-agent
    hook_modified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    render_time_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="video_renders")
