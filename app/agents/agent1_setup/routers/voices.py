import uuid

from fastapi import APIRouter, Depends, Query

from app.services.auth import get_current_user_id
from app.agents.agent1_setup.services import elevenlabs

router = APIRouter(prefix="/api/agent1", tags=["agent1-voices"])


@router.get("/voices")
def list_voices(
    language: str = Query(..., description="Language code: fr | en | de | es | it | pt"),
    use_case: str = Query(..., description="ElevenLabs use case"),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    return {"voices": elevenlabs.get_shared_voices(language, use_case)}
