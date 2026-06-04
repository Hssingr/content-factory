import uuid
from sqlalchemy import String, Integer, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.database import Base


class AudioFile(Base):
    __tablename__ = "audio_files"
    __table_args__ = (UniqueConstraint("content_id", "language", name="uq_audio_content_language"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Recalculated breakpoints after real audio duration is known
    shorts_breakpoints: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Whisper word-level timestamps: [{"word": str, "start": float, "end": float}]
    whisper_transcript: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="audio_files")
