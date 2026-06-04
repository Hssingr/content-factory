import uuid
from sqlalchemy import String, Boolean, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ChannelPlatform(Base):
    __tablename__ = "channel_platforms"
    __table_args__ = (UniqueConstraint("channel_id", "language", "platform", name="uq_channel_platform_language"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    # 'youtube' | 'tiktok' | 'instagram' | 'facebook'
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_channel_id: Mapped[str] = mapped_column(String(256), nullable=True)
    # Fernet-encrypted JSON blob of API credentials; decrypted only at publish time
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    channel: Mapped["Channel"] = relationship("Channel", back_populates="platforms")
