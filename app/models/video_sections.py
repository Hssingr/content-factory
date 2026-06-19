import uuid
from sqlalchemy import Float, String, Integer, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class VideoSection(Base):
    __tablename__ = "video_sections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id", ondelete="CASCADE"), nullable=False)
    # "__visual__" for the shared storyboard/Flux pass; BCP-47 language code for per-language render copies
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    section_order: Mapped[int] = mapped_column(Integer, nullable=False)
    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    audio_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Flux Schnell image generation prompt for this beat
    flux_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    effect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # JSON blob: visual_intent, visual_type, environment, motif, transition_to_next,
    # overlay_text, overlay_position, media_url (local cache path after Flux generation)
    generation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    beat_intensity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    suggested_duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    media_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text_card_style: Mapped[str | None] = mapped_column(String(32), nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="video_sections")
