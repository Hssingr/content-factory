import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, Content
from app.schemas.content import ContentResponse
from app.services.auth import get_current_user_id

router = APIRouter(prefix="/api/agent2", tags=["agent2-discovery"])


@router.post("/run/{channel_id}", status_code=202)
def manual_trigger(
    channel_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Manually trigger Agent 2 discovery for a channel.

    Queues a Celery task that runs the full pipeline:
    fetch → score → deduplicate → generate scripts → send Telegram validation.

    The channel must belong to the current user and be active.

    Returns 202 Accepted immediately — the pipeline runs asynchronously.
    """
    channel: Channel | None = (
        db.query(Channel)
        .filter(Channel.id == channel_id, Channel.user_id == user_id)
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel.active:
        raise HTTPException(status_code=400, detail="Channel is not active — activate it first")

    from app.scheduler.tasks import run_agent2_for_channel
    run_agent2_for_channel.delay(str(channel_id))

    return {"status": "queued", "channel_id": str(channel_id)}


@router.get("/content", response_model=list[ContentResponse])
def list_content(
    channel_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """List content items across the current user's channels.

    Filters:
    - ``channel_id``: restrict to one channel
    - ``status``:     e.g. ``PENDING_APPROVAL``, ``APPROVED``, ``SCRIPTS_VALIDATED``
    - ``limit``:      max results (default 50)

    Results are ordered newest first.
    """
    channel_ids = [
        row[0]
        for row in db.query(Channel.id).filter(Channel.user_id == user_id).all()
    ]
    if not channel_ids:
        return []

    query = db.query(Content).filter(Content.channel_id.in_(channel_ids))

    if channel_id is not None:
        if channel_id not in channel_ids:
            raise HTTPException(status_code=404, detail="Channel not found")
        query = query.filter(Content.channel_id == channel_id)

    if status is not None:
        query = query.filter(Content.status == status)

    return query.order_by(Content.created_at.desc()).limit(limit).all()
