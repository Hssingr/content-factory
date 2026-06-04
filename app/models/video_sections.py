import uuid
from sqlalchemy import String, Integer, Boolean, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class VideoSection(Base):
    __tablename__ = "video_sections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    section_order: Mapped[int] = mapped_column(Integer, nullable=False)
    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'pexels' | 'unsplash' | 'runway' | 'stock'
    visual_source: Mapped[str] = mapped_column(String(32), nullable=False)
    search_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    effect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runway_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subagent_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # True when the section used the best attempt after hitting max rounds
    best_attempt_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    content: Mapped["Content"] = relationship("Content", back_populates="video_sections")
