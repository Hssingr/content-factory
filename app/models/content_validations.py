import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.database import Base


class ContentValidation(Base):
    __tablename__ = "content_validations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False, unique=True)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # PENDING | APPROVED | REJECTED | TIMEOUT
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # PENDING | PASSED | AUTO_CORRECTED | NEEDS_REVIEW
    script_validation_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # List of issue dicts logged by Agent 3
    script_issues_log: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    self_correction_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    content: Mapped["Content"] = relationship("Content", back_populates="validation")
