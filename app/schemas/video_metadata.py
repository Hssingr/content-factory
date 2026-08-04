import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class VideoMetadataResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_id: uuid.UUID
    language: str
    platform: str
    title: str
    description: str | None
    hashtags: list | None
    thumbnail_file_path: str | None
    thumbnail_text: str | None
    generated_at: datetime


class VideoMetadataUpdate(BaseModel):
    """PATCH body — every field optional; only fields present are edited.

    Every edited field is re-run through the same deterministic
    ``platform_limits`` enforcement generation already uses before
    persisting (Agent 6 roadmap Check 6, fix 1) — this is not a bare field
    write. A ``thumbnail_text`` edit on a ``platform="youtube"`` row also
    triggers a Pillow-only re-composite from the existing base image
    (fix 2); it is rejected on any other platform's row.
    """

    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    thumbnail_text: str | None = None
