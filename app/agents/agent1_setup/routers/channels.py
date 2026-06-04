import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.channel import (
    ChannelCreate, ChannelUpdate, ChannelResponse,
    ChannelConfigUpsert,
    LanguageEntry, VoiceEntry, SourceEntry, PublishTimingEntry,
    CredentialSave, VerifyCredential,
)
from app.services.auth import get_current_user_id
from app.services import crypto, platform_verifier
from app.agents.agent1_setup.services import channels as channels_service

router = APIRouter(prefix="/api/agent1/channels", tags=["agent1-channels"])


def _or_404(result, channel_id: uuid.UUID):
    if result is None:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
    return result


@router.post("", response_model=ChannelResponse, status_code=201)
def create_channel(
    body: ChannelCreate,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return channels_service.create(db, user_id, body)


@router.get("", response_model=list[ChannelResponse])
def list_channels(
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return channels_service.get_all_for_user(db, user_id)


@router.get("/{channel_id}", response_model=ChannelResponse)
def get_channel(
    channel_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return _or_404(channels_service.get_by_id(db, channel_id), channel_id)


@router.put("/{channel_id}", response_model=ChannelResponse)
def update_channel(
    channel_id: uuid.UUID,
    body: ChannelUpdate,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return _or_404(channels_service.update(db, channel_id, body), channel_id)


@router.put("/{channel_id}/config", response_model=ChannelResponse)
def upsert_config(
    channel_id: uuid.UUID,
    body: ChannelConfigUpsert,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return _or_404(channels_service.upsert_config(db, channel_id, body), channel_id)


@router.put("/{channel_id}/languages", response_model=ChannelResponse)
def replace_languages(
    channel_id: uuid.UUID,
    body: list[LanguageEntry],
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return _or_404(channels_service.replace_languages(db, channel_id, body), channel_id)


@router.put("/{channel_id}/voices", response_model=ChannelResponse)
def replace_voices(
    channel_id: uuid.UUID,
    body: list[VoiceEntry],
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return _or_404(channels_service.replace_voices(db, channel_id, body), channel_id)


@router.put("/{channel_id}/sources", response_model=ChannelResponse)
def replace_sources(
    channel_id: uuid.UUID,
    body: list[SourceEntry],
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return _or_404(channels_service.replace_sources(db, channel_id, body), channel_id)


@router.put("/{channel_id}/timings", response_model=ChannelResponse)
def upsert_timings(
    channel_id: uuid.UUID,
    body: list[PublishTimingEntry],
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return _or_404(channels_service.upsert_timings(db, channel_id, body), channel_id)


@router.post("/{channel_id}/credentials", response_model=ChannelResponse, status_code=201)
def save_credentials(
    channel_id: uuid.UUID,
    body: CredentialSave,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    encrypted = crypto.encrypt(json.dumps(body.credentials))
    return _or_404(channels_service.save_credential(db, channel_id, body, encrypted), channel_id)


@router.post("/{channel_id}/verify")
def verify_credential(
    channel_id: uuid.UUID,
    body: VerifyCredential,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    platform_row = channels_service.get_platform(db, channel_id, body.language, body.platform)
    if platform_row is None or not platform_row.credentials_encrypted:
        raise HTTPException(status_code=404, detail="Credential not found — save credentials first")

    credentials = json.loads(crypto.decrypt(platform_row.credentials_encrypted))
    if not platform_verifier.verify(body.platform, credentials):
        raise HTTPException(status_code=400, detail="Credential verification failed")

    channels_service.verify_credential(db, channel_id, body)
    return {"verified": True, "language": body.language, "platform": body.platform}


@router.delete("/{channel_id}", status_code=204)
def delete_channel(
    channel_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    channel = channels_service.get_by_id(db, channel_id)
    _or_404(channel, channel_id)
    if channel.active:
        raise HTTPException(status_code=400, detail="Cannot delete an active channel — deactivate it first")
    channels_service.delete(db, channel_id)


@router.post("/{channel_id}/suggest-timing")
def suggest_timing(
    channel_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Ask Claude to suggest optimal publish days/times for each channel language.

    Requires Section 2 (languages) and Section 4 config (videos_per_week) to be saved first.
    Returns a list of timing suggestions — one per language — ready to be displayed and edited.
    """
    from app.models import ChannelConfig
    from app.agents.agent1_setup.system_prompt import suggest_publish_timing

    channel = channels_service.get_by_id(db, channel_id)
    _or_404(channel, channel_id)

    config: ChannelConfig | None = db.get(ChannelConfig, channel_id)
    vpw = config.videos_per_week if config else 3

    if not channel.languages:
        raise HTTPException(status_code=400, detail="Save languages first (Section 2)")

    suggestions = []
    for lang_entry in channel.languages:
        try:
            timing = suggest_publish_timing(
                language=lang_entry.language,
                niche=channel.niche,
                videos_per_week=vpw,
                tone=channel.tone,
            )
            suggestions.append({"language": lang_entry.language, **timing})
        except Exception as exc:
            suggestions.append({
                "language": lang_entry.language,
                "error": str(exc),
                "timezone": "UTC",
                "optimal_days": [],
                "optimal_hour_start": 18,
                "optimal_hour_end": 20,
                "shorts_spread_hours": 6,
            })

    return suggestions


@router.post("/{channel_id}/activate", response_model=ChannelResponse)
def activate_channel(
    channel_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    channel = channels_service.get_by_id(db, channel_id)
    _or_404(channel, channel_id)
    if not any(p.verified for p in channel.platforms):
        raise HTTPException(status_code=400, detail="At least one platform must be verified before activating")
    return channels_service.activate(db, channel_id)
