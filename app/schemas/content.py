import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class ContentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_id: uuid.UUID
    source_url: str
    source_language: str
    title: str
    status: str
    created_at: datetime
    published_at: datetime | None
