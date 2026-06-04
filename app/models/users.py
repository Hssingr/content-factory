import uuid
from sqlalchemy import Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    primary_language: Mapped[str] = mapped_column(String(10), nullable=False)
    # Hour (0-23, local time) at which to start story generation on the day before publishing
    pipeline_run_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=18)
    # IANA timezone for pipeline_run_hour (e.g. "Europe/Paris")
    pipeline_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    channels: Mapped[list["Channel"]] = relationship("Channel", back_populates="user")
