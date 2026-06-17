import uuid
from sqlalchemy import Boolean, Float, String, ForeignKey, UniqueConstraint
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

    # TTS model — provider-specific model identifier.
    # Cartesia (default): "sonic-2"  |  ElevenLabs (legacy): "eleven_v3" | "eleven_multilingual_v2"
    tts_model: Mapped[str] = mapped_column(String(64), nullable=False, server_default="sonic-2")

    # Per-voice overrides for ElevenLabs VoiceSettings (ignored when provider="cartesia").
    # When set, these take full precedence over the emotion preset defaults in tts.py.
    stability_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    similarity_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    style_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    # speed_override takes full precedence over speed_profile + emotion delta when set.
    speed_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    use_speaker_boost: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # ElevenLabs eleven_v3 only: maps to stability float — "creative"=0.30 | "natural"=0.65 | "robust"=0.85
    # NULL defaults to "natural" (0.65). Ignored for Cartesia and other ElevenLabs models.
    v3_stability_preset: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Base speed multiplier. Emotion speed delta is added on top; clamped to [0.7, 1.2].
    # "slow"=0.85 | "normal"=0.97 | "fast"=1.05 | "very_fast"=1.12
    # speed_override takes full precedence when set.
    speed_profile: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")

    channel: Mapped["Channel"] = relationship("Channel", back_populates="voices")
