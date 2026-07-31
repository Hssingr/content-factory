from pydantic import BaseModel


class VoiceSummary(BaseModel):
    """One provider voice-catalog entry, normalized across Cartesia/ElevenLabs
    so the frontend voice browser can render either provider identically."""
    voice_id: str
    name: str
    description: str = ""
    gender: str | None = None
    language: str | None = None
    accent: str | None = None
    age: str | None = None
    preview_url: str | None = None
    provider: str


class VoiceBrowseResponse(BaseModel):
    voices: list[VoiceSummary]
    has_more: bool
    # Opaque pagination token — echo back as the next request's `cursor` param.
    # Meaning differs per provider (a Cartesia voice ID vs. an ElevenLabs page
    # number) but callers never need to interpret it themselves.
    next_cursor: str | None = None
