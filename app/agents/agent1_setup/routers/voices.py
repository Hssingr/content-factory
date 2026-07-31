import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.schemas.voice_browse import VoiceBrowseResponse
from app.services.auth import get_current_user_id
from app.agents.agent1_setup.services import cartesia_voices, elevenlabs_voices

router = APIRouter(prefix="/api/agent1", tags=["agent1-voices"])


@router.get("/voices", response_model=VoiceBrowseResponse)
def list_voices(
    provider: Literal["cartesia", "elevenlabs"] = Query(...),
    search: str | None = Query(None, description="Free-text search across name/description"),
    gender: str | None = Query(None, description="Cartesia: masculine|feminine|gender_neutral. ElevenLabs: free-text, e.g. male|female"),
    language: str | None = Query(None, description="Language/locale code, e.g. en"),
    accent: str | None = Query(None, description="ElevenLabs only — ignored for Cartesia"),
    age: str | None = Query(None, description="ElevenLabs only — ignored for Cartesia"),
    use_case: str | None = Query(None, description="ElevenLabs only — ignored for Cartesia"),
    cursor: str | None = Query(None, description="Opaque next_cursor from a previous response"),
    page_size: int = Query(20, ge=1, le=100),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Browse a TTS provider's voice catalog — paginated and filtered the
    same way the provider's own platform lets an operator browse it
    (Cartesia's `GET /voices`, ElevenLabs' Voice Library `get_shared()`).

    This is Agent 1 setup-time tooling only: it helps the operator find a
    real Voice ID to paste into a Voice card; it never validates or writes
    a `ChannelVoice` row itself (that remains `PUT .../voices`, unchanged).
    """
    if provider == "cartesia":
        return cartesia_voices.list_voices(
            search=search, gender=gender, language=language,
            page_size=page_size, cursor=cursor,
        )
    return elevenlabs_voices.list_shared_voices(
        search=search, gender=gender, age=age, accent=accent,
        language=language, use_case=use_case,
        page_size=page_size, cursor=cursor,
    )


@router.get("/voices/preview")
def preview_voice(
    provider: Literal["cartesia", "elevenlabs"] = Query(...),
    voice_id: str = Query(...),
    user_id: uuid.UUID = Depends(get_current_user_id),
):
    """Live-synthesize a short preview clip for a voice that has no static
    preview sample available (Cartesia — see
    `cartesia_voices.synthesize_preview()`'s module docstring for why).

    ElevenLabs voices already carry a real `preview_url` in `GET /voices`'
    response — the frontend plays that directly and never calls this
    endpoint for that provider; requesting it here is a 400, not a silent
    no-op, since it would indicate a frontend bug.
    """
    if provider != "cartesia":
        raise HTTPException(
            status_code=400,
            detail=f"provider={provider!r} already returns a static preview_url — no on-demand preview endpoint needed",
        )
    audio_bytes = cartesia_voices.synthesize_preview(voice_id)
    if audio_bytes is None:
        raise HTTPException(status_code=503, detail="Voice preview synthesis unavailable")
    return Response(content=audio_bytes, media_type="audio/wav")
