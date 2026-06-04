import uuid
from sqlalchemy import String, Boolean, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    niche: Mapped[str] = mapped_column(String(128), nullable=False)
    tone: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped["User"] = relationship("User", back_populates="channels")
    config: Mapped["ChannelConfig"] = relationship("ChannelConfig", back_populates="channel", uselist=False)
    languages: Mapped[list["ChannelLanguage"]] = relationship("ChannelLanguage", back_populates="channel")
    voices: Mapped[list["ChannelVoice"]] = relationship("ChannelVoice", back_populates="channel")
    sources: Mapped[list["ChannelSource"]] = relationship("ChannelSource", back_populates="channel")
    platforms: Mapped[list["ChannelPlatform"]] = relationship("ChannelPlatform", back_populates="channel")
    publish_timings: Mapped[list["ChannelPublishTiming"]] = relationship("ChannelPublishTiming", back_populates="channel")
    contents: Mapped[list["Content"]] = relationship("Content", back_populates="channel")
