import uuid
from sqlalchemy import String, Integer, Float, Boolean, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Script(Base):
    __tablename__ = "scripts"
    __table_args__ = (UniqueConstraint("content_id", "language", "version", name="uq_script_content_language_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    video_script: Mapped[str] = mapped_column(Text, nullable=False)
    voice_script: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    content: Mapped["Content"] = relationship("Content", back_populates="scripts")
