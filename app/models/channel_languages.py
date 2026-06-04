import uuid
from sqlalchemy import String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ChannelLanguage(Base):
    __tablename__ = "channel_languages"
    __table_args__ = (UniqueConstraint("channel_id", "language", name="uq_channel_language"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)

    channel: Mapped["Channel"] = relationship("Channel", back_populates="languages")
