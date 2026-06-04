import uuid
from sqlalchemy import String, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ChannelSource(Base):
    __tablename__ = "channel_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    # 'rss' | 'reddit' | 'twitter' | 'youtube' | 'web'
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_value: Mapped[str] = mapped_column(String(1024), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    channel: Mapped["Channel"] = relationship("Channel", back_populates="sources")
