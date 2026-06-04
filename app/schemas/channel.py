import uuid
from pydantic import BaseModel, ConfigDict


class ChannelCreate(BaseModel):
    name: str
    description: str | None = None
    niche: str
    tone: str


class ChannelUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    niche: str | None = None
    tone: str | None = None


class ChannelConfigUpsert(BaseModel):
    videos_per_week: int = 3
    shorts_rule: str = "auto"
    validation_timeout_hours: int = 24
    validation_max_revisions: int = 3
    validation_on_limit_reached: str = "auto_approve"
    subtitle_style_main: str = "standard"
    subtitle_style_shorts: str = "karaoke"
    subtitle_karaoke_active_color: str = "#FFD700"
    shorts_part_label_style: str = "default"
    video_style_type: str = "documentary"
    video_color_grade: str | None = None
    runway_enabled: bool = False


class LanguageEntry(BaseModel):
    language: str
    channel_name: str


class VoiceEntry(BaseModel):
    language: str
    provider: str = "elevenlabs"
    voice_id: str
    emotion: str | None = None
    music_style: str | None = None
    use_case: str | None = None


class SourceEntry(BaseModel):
    source_type: str
    source_value: str
    language: str
    trust_score: float = 1.0


class PublishTimingEntry(BaseModel):
    platform: str
    language: str
    timezone: str = "UTC"
    optimal_days: list = []
    optimal_hour_start: int = 9
    optimal_hour_end: int = 21
    shorts_spread_hours: int = 6


class CredentialSave(BaseModel):
    language: str
    platform: str
    platform_channel_id: str | None = None
    credentials: dict  # raw dict; will be Fernet-encrypted in Step 3


class VerifyCredential(BaseModel):
    language: str
    platform: str


# ── Response models ───────────────────────────────────────────────────────────

class ChannelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    videos_per_week: int
    shorts_rule: str
    validation_timeout_hours: int
    validation_max_revisions: int
    validation_on_limit_reached: str
    subtitle_style_main: str
    subtitle_style_shorts: str
    subtitle_karaoke_active_color: str
    shorts_part_label_style: str
    video_style_type: str
    video_color_grade: str | None
    runway_enabled: bool


class LanguageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    language: str
    channel_name: str


class VoiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    language: str
    provider: str
    voice_id: str
    emotion: str | None
    music_style: str | None
    use_case: str | None


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_type: str
    source_value: str
    language: str
    trust_score: float


class PlatformResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    language: str
    platform: str
    platform_channel_id: str | None
    verified: bool
    active: bool


class PublishTimingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    platform: str
    language: str
    timezone: str
    optimal_days: list
    optimal_hour_start: int
    optimal_hour_end: int
    shorts_spread_hours: int


class ChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None
    niche: str
    tone: str
    active: bool
    config: ChannelConfigResponse | None
    languages: list[LanguageResponse]
    voices: list[VoiceResponse]
    sources: list[SourceResponse]
    platforms: list[PlatformResponse]
    publish_timings: list[PublishTimingResponse]
