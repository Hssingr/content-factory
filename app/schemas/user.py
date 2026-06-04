import uuid
from pydantic import BaseModel, ConfigDict


class UserCreate(BaseModel):
    name: str
    telegram_chat_id: str
    primary_language: str
    pipeline_run_hour: int = 18    # default: 18h local time
    pipeline_timezone: str = "UTC"


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    telegram_chat_id: str
    primary_language: str
    pipeline_run_hour: int
    pipeline_timezone: str
