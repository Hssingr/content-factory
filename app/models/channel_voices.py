import uuid
from sqlalchemy import String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ChannelVoice(Base):
    __tablename__ = "channel_voices"
    __table_args__ = (UniqueConstraint("channel_id", "language", name="uq_channel_voice_language"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="elevenlabs")
    voice_id: Mapped[str] = mapped_column(String(128), nullable=False)
    emotion: Mapped[str] = mapped_column(String(64), nullable=True)
    music_style: Mapped[str] = mapped_column(String(64), nullable=True)
    use_case: Mapped[str | None] = mapped_column(String(64), nullable=True)

    channel: Mapped["Channel"] = relationship("Channel", back_populates="voices")
