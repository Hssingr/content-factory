import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.suggest import SuggestRequest, SuggestResponse
from app.schemas.research_ideas import ResearchIdeasRequest, ResearchIdeasResponse
from app.services.auth import get_current_user_id
from app.agents.agent1_setup.services import users as users_service
from app.agents.agent1_setup.system_prompt import suggest_field, research_channel_ideas

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

@router.post("/research-ideas", response_model=ResearchIdeasResponse)
def research_ideas(
    body: ResearchIdeasRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    # validate mode requires a description; explore mode is fine with an empty one.
    if body.mode == "validate" and not (body.channel_description or "").strip():
        raise HTTPException(status_code=400, detail="Enter a channel description before validating")

    # Load the user through the existing Agent 1 pattern so auth/session wiring is
    # identical to /suggest. The current research prompt does not need user data.
    users_service.get_by_id(db, user_id)
    try:
        result = research_channel_ideas(
            channel_description=body.channel_description,
            content_mode=body.content_mode,
            target_languages=body.target_languages,
            target_platforms=body.target_platforms,
            mode=body.mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Claude research ideas failed: %s", exc)
        raise HTTPException(status_code=503, detail="AI market research service unavailable")

    # Explicit validation here, before returning (CLAUDE.md Golden Rule 7 —
    # "No unvalidated AI output"): without this, an incomplete Claude
    # response only ever surfaced as FastAPI's own automatic response_model
    # serialization crashing with a raw, uncaught ResponseValidationError —
    # an opaque 500 with no HTTPException/detail the frontend could show.
    # research_channel_ideas() no longer has known-duplicate fields to
    # backfill — the two-call split removes the duplication at the schema
    # level instead (CLAUDE.md §8.5); this is the last-resort net for
    # anything else that still doesn't validate.
    try:
        return ResearchIdeasResponse.model_validate(result)
    except ValidationError as exc:
        logger.error("Research ideas response failed validation: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="AI market research returned an incomplete response — please try again",
        )
