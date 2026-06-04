import uuid
from sqlalchemy import String, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.database import Base


class ChannelPublishTiming(Base):
    __tablename__ = "channel_publish_timing"
    __table_args__ = (UniqueConstraint("channel_id", "platform", "language", name="uq_channel_timing_platform_language"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # e.g. ["monday", "wednesday", "friday"]
    optimal_days: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    optimal_hour_start: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    optimal_hour_end: Mapped[int] = mapped_column(Integer, nullable=False, default=21)
    shorts_spread_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=6)

    channel: Mapped["Channel"] = relationship("Channel", back_populates="publish_timings")
