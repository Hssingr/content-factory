import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.suggest import SuggestRequest, SuggestResponse
from app.services.auth import get_current_user_id
from app.agents.agent1_setup.services import users as users_service
from app.agents.agent1_setup.system_prompt import suggest_field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent1", tags=["agent1-suggest"])


@router.post("/suggest", response_model=SuggestResponse)
def suggest(
    body: SuggestRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    user = users_service.get_by_id(db, user_id)
    context_with_lang = {
        **body.context,
        "user_language": user.primary_language if user else "en",
    }
    try:
        suggestion = suggest_field(body.field, context_with_lang)
    except Exception as exc:
        logger.error("Claude suggest failed for field=%s: %s", body.field, exc)
        raise HTTPException(status_code=503, detail="AI suggestion service unavailable")
    return SuggestResponse(field=body.field, suggestion=suggestion)
