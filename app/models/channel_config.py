import uuid
from sqlalchemy import String, Integer, Boolean, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ChannelConfig(Base):
    __tablename__ = "channel_config"

    # One-to-one with channels; channel_id is both FK and PK
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    videos_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # 'always' | 'never' | 'auto'
    shorts_rule: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    validation_timeout_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    validation_max_revisions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # 'auto_approve' | 'needs_review'
    validation_on_limit_reached: Mapped[str] = mapped_column(String(16), nullable=False, default="auto_approve")
    subtitle_style_main: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    subtitle_style_shorts: Mapped[str] = mapped_column(String(32), nullable=False, default="karaoke")
    subtitle_karaoke_active_color: Mapped[str] = mapped_column(String(16), nullable=False, default="#FFD700")
    shorts_part_label_style: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    video_style_type: Mapped[str] = mapped_column(String(64), nullable=False, default="documentary")
    video_color_grade: Mapped[str] = mapped_column(String(64), nullable=True)
    # Runway is the absolute last resort; disabled by default
    runway_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 'youtube_long' | 'youtube_short' | 'tiktok' | 'reels'
    script_format: Mapped[str] = mapped_column(String(32), nullable=False, server_default="youtube_long")
    # Storyboard failure policy. False (default): stop language generation with an
    # explicit error when the Storyboard Agent fails — never silently fall back to
    # the legacy section splitter (silent fallback was masking a 100% storyboard
    # failure rate). True: restore the previous silent-fallback behavior.
    allow_legacy_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Quality gate policy. False (default): block render only on HIGH-severity viewer
    # experience issues (visuals category) that remain after the repair pass — non-blocking
    # categories (intro, audio, captions, pacing) never block rendering.
    # True: block on any NEEDS_FIXES verdict that survives the repair pass.
    strict_quality_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # ElevenLabs v3 audio tags (e.g. [dramatic pause], [whispers]).
    # Only meaningful when provider="elevenlabs" AND tts_model="eleven_v3".
    # False for all existing channels — safe default.
    audio_tags_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # ── Content Factory V3 groundwork (Phase Agent1-V3.2) ─────────────────────
    # Additive, defaulted columns only. None of these are read by Agent 2/3/4/5
    # yet — adding them here changes no runtime behavior; only Agent 1's CRUD
    # and the Pydantic schemas in app/schemas/channel.py read/write them today.
    # See CLAUDE.md §8.1 for the full allowed-values table, current-support
    # status, and the explicit "no downstream behavior change yet" contract.

    # 'single_story' (current/only supported) | 'limited_series' | 'ongoing_series'
    # (both reserved for a future phase — accepted by the schema, not executed).
    content_mode: Mapped[str] = mapped_column(String(32), nullable=False, server_default="single_story")
    # 'reddit' (current/only supported — matches Agent 2's existing discovery
    # default) | 'ai_generated' | 'user_provided' | 'hybrid' (reserved).
    script_source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="reddit")
    # 'youtube_and_shorts' (current/only supported — matches the existing
    # parent+standalone-shorts architecture) | 'youtube_long_only' | 'shorts_only'
    # (reserved).
    output_mode: Mapped[str] = mapped_column(String(32), nullable=False, server_default="youtube_and_shorts")
    # Free-form descriptor, same looseness as video_style_type above (no DB-level
    # enum). Defaults to the same value video_style_type already defaults to, so
    # existing channels stay default-compatible. Distinct column from
    # video_style_type intentionally — see CLAUDE.md §8.1 for why both exist and
    # which one a future phase should reconcile/deprecate.
    visual_style: Mapped[str] = mapped_column(Text, nullable=False, server_default="documentary")
    # Free-form descriptor for the Flux image-generation look.
    image_style: Mapped[str] = mapped_column(Text, nullable=False, server_default="photorealistic")

    channel: Mapped["Channel"] = relationship("Channel", back_populates="config")
