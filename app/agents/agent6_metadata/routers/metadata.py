"""Agent 6 — manual metadata review API surface (roadmap Phase E, Check 6).

Backend-only per the resolved Open Decision — no operator-facing review
screen ships here, only the minimum endpoints that make one possible later:

- ``POST .../content/{content_id}/approve`` — bypasses
  ``check_metadata_auto_approve()``'s timer, mirroring how a human Telegram
  ``APPROVE`` reply already bypasses ``check_validation_timeouts()``'s sweep.
- ``PATCH .../video-metadata/{id}`` — edits one ``VideoMetadata`` row.
  **Not** a bare field write (Check 6, fixes 1-2, both required together):
  every edited field is re-run through the same ``platform_limits``
  enforcement generation already uses, and an edited ``thumbnail_text`` on a
  ``platform="youtube"`` row triggers a Pillow-only re-composite from the
  Phase C base image already on disk — zero paid calls.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Channel, Content, VideoMetadata
from app.schemas.video_metadata import VideoMetadataResponse, VideoMetadataUpdate
from app.services.auth import get_current_user_id
from app.agents.agent6_metadata.services import platform_limits, thumbnail

router = APIRouter(prefix="/api/agent6", tags=["agent6-metadata"])


def _load_owned_content(content_id: uuid.UUID, user_id: uuid.UUID, db: Session) -> Content:
    """Two plain ``.get()`` lookups rather than a join — simpler and
    trivially testable against this repo's established fake-DB pattern
    (``.get()`` is universally supported; a real SQL join is not)."""
    content: Content | None = db.get(Content, content_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Content not found")

    channel: Channel | None = db.get(Channel, content.channel_id)
    if channel is None or channel.user_id != user_id:
        raise HTTPException(status_code=404, detail="Content not found")
    return content


@router.post("/content/{content_id}/approve", response_model=dict)
def approve_metadata(
    content_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Manually approve a content item's metadata early, bypassing the
    ``metadata_auto_approve_seconds`` timer.

    Requires ``Content.status == "METADATA_PENDING_APPROVAL"`` — approving a
    content item at any other stage (still generating, already approved, or
    failed) is a client error, not silently accepted.
    """
    content = _load_owned_content(content_id, user_id, db)
    if content.status != "METADATA_PENDING_APPROVAL":
        raise HTTPException(
            status_code=400,
            detail=f"Content status is {content.status}, expected METADATA_PENDING_APPROVAL",
        )

    content.status = "METADATA_APPROVED"
    db.commit()
    return {"status": "approved", "content_id": str(content_id)}


@router.patch("/video-metadata/{video_metadata_id}", response_model=VideoMetadataResponse)
def update_video_metadata(
    video_metadata_id: uuid.UUID,
    update: VideoMetadataUpdate,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Edit a ``VideoMetadata`` row. See module docstring for the
    re-limit/re-composite contract — this is never a bare field write."""
    row: VideoMetadata | None = db.get(VideoMetadata, video_metadata_id)
    if row is None:
        raise HTTPException(status_code=404, detail="VideoMetadata row not found")

    # Ownership check: walk VideoMetadata -> Content -> Channel -> user_id,
    # the same ownership chain every other content-scoped endpoint enforces.
    content = _load_owned_content(row.content_id, user_id, db)

    if update.thumbnail_text is not None and row.platform != "youtube":
        raise HTTPException(
            status_code=400,
            detail="thumbnail_text can only be set on a platform=\"youtube\" row",
        )

    title = update.title if update.title is not None else row.title
    description = update.description if update.description is not None else row.description
    hashtags = update.hashtags if update.hashtags is not None else (row.hashtags or [])

    title, description, hashtags = platform_limits.enforce_platform_limits(
        row.platform, title, description or "", hashtags,
    )
    row.title = title
    row.description = description
    row.hashtags = hashtags

    if update.thumbnail_text is not None:
        final_thumbnail_text = platform_limits.enforce_thumbnail_text_limit(update.thumbnail_text)
        row.thumbnail_text = final_thumbnail_text

        # Pillow-only re-composite from the already-on-disk base image
        # (Phase C's canonical thumbnails/{content_id}/base.jpg) — zero paid
        # calls, zero Flux interaction. Non-fatal on any failure (missing
        # base, uncovered glyph, etc.) — leaves thumbnail_file_path as
        # composite_thumbnail_overlay() returns (possibly None), matching
        # the same degrade contract generation-time compositing already has.
        base_relative = f"thumbnails/{content.id}/base.jpg"
        row.thumbnail_file_path = thumbnail.composite_thumbnail_overlay(
            base_relative, final_thumbnail_text, row.language, content.id,
        )

    db.commit()
    return row
