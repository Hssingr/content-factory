import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Channel, ChannelConfig, ChannelLanguage,
    ChannelVoice, ChannelSource, ChannelPlatform, ChannelPublishTiming,
)
from app.schemas.channel import (
    ChannelCreate, ChannelUpdate, ChannelConfigUpsert,
    LanguageEntry, VoiceEntry, SourceEntry, PublishTimingEntry,
    CredentialSave, VerifyCredential,
)


def _load(db: Session, channel_id: uuid.UUID) -> Channel | None:
    """Fetch channel with all sub-resources eagerly loaded."""
    return (
        db.query(Channel)
        .options(
            selectinload(Channel.config),
            selectinload(Channel.languages),
            selectinload(Channel.voices),
            selectinload(Channel.sources),
            selectinload(Channel.platforms),
            selectinload(Channel.publish_timings),
        )
        .filter(Channel.id == channel_id)
        .first()
    )


def create(db: Session, user_id: uuid.UUID, data: ChannelCreate) -> Channel:
    channel = Channel(
        user_id=user_id,
        name=data.name,
        description=data.description,
        niche=data.niche,
        tone=data.tone,
    )
    db.add(channel)
    db.commit()
    return _load(db, channel.id)


def delete(db: Session, channel_id: uuid.UUID) -> None:
    # Use raw SQL so DB-level ON DELETE CASCADE handles child tables
    # (avoids SQLAlchemy ORM trying to null channel_config.channel_id which is a PK)
    db.execute(sa.delete(Channel).where(Channel.id == channel_id))
    db.commit()


def get_by_id(db: Session, channel_id: uuid.UUID) -> Channel | None:
    return _load(db, channel_id)


def get_all_for_user(db: Session, user_id: uuid.UUID) -> list[Channel]:
    return (
        db.query(Channel)
        .options(
            selectinload(Channel.config),
            selectinload(Channel.languages),
            selectinload(Channel.voices),
            selectinload(Channel.sources),
            selectinload(Channel.platforms),
            selectinload(Channel.publish_timings),
        )
        .filter(Channel.user_id == user_id)
        .all()
    )


def update(db: Session, channel_id: uuid.UUID, data: ChannelUpdate) -> Channel | None:
    channel = db.get(Channel, channel_id)
    if channel is None:
        return None
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(channel, field, value)
    db.commit()
    return _load(db, channel_id)


def upsert_config(db: Session, channel_id: uuid.UUID, data: ChannelConfigUpsert) -> Channel | None:
    if db.get(Channel, channel_id) is None:
        return None
    config = db.get(ChannelConfig, channel_id)
    if config is None:
        config = ChannelConfig(channel_id=channel_id)
        db.add(config)
    for field, value in data.model_dump().items():
        setattr(config, field, value)
    db.commit()
    return _load(db, channel_id)


def replace_languages(db: Session, channel_id: uuid.UUID, entries: list[LanguageEntry]) -> Channel | None:
    if db.get(Channel, channel_id) is None:
        return None
    db.query(ChannelLanguage).filter(ChannelLanguage.channel_id == channel_id).delete(synchronize_session=False)
    db.add_all([
        ChannelLanguage(channel_id=channel_id, language=e.language, channel_name=e.channel_name)
        for e in entries
    ])
    db.commit()
    return _load(db, channel_id)


def replace_voices(db: Session, channel_id: uuid.UUID, entries: list[VoiceEntry]) -> Channel | None:
    if db.get(Channel, channel_id) is None:
        return None
    db.query(ChannelVoice).filter(ChannelVoice.channel_id == channel_id).delete(synchronize_session=False)
    db.add_all([
        ChannelVoice(
            channel_id=channel_id,
            language=e.language,
            provider=e.provider,
            voice_id=e.voice_id,
            emotion=e.emotion,
            music_style=e.music_style,
            use_case=e.use_case,
        )
        for e in entries
    ])
    db.commit()
    return _load(db, channel_id)


def replace_sources(db: Session, channel_id: uuid.UUID, entries: list[SourceEntry]) -> Channel | None:
    if db.get(Channel, channel_id) is None:
        return None
    db.query(ChannelSource).filter(ChannelSource.channel_id == channel_id).delete(synchronize_session=False)
    db.add_all([
        ChannelSource(
            channel_id=channel_id,
            source_type=e.source_type,
            source_value=e.source_value,
            language=e.language,
            trust_score=e.trust_score,
        )
        for e in entries
    ])
    db.commit()
    return _load(db, channel_id)


def upsert_timings(db: Session, channel_id: uuid.UUID, entries: list[PublishTimingEntry]) -> Channel | None:
    if db.get(Channel, channel_id) is None:
        return None
    for e in entries:
        timing = (
            db.query(ChannelPublishTiming)
            .filter_by(channel_id=channel_id, platform=e.platform, language=e.language)
            .first()
        )
        if timing is None:
            timing = ChannelPublishTiming(channel_id=channel_id, platform=e.platform, language=e.language)
            db.add(timing)
        timing.timezone = e.timezone
        timing.optimal_days = e.optimal_days
        timing.optimal_hour_start = e.optimal_hour_start
        timing.optimal_hour_end = e.optimal_hour_end
        timing.shorts_spread_hours = e.shorts_spread_hours
    db.commit()
    return _load(db, channel_id)


def get_platform(db: Session, channel_id: uuid.UUID, language: str, platform: str) -> ChannelPlatform | None:
    return (
        db.query(ChannelPlatform)
        .filter_by(channel_id=channel_id, language=language, platform=platform)
        .first()
    )


def save_credential(db: Session, channel_id: uuid.UUID, data: CredentialSave, encrypted: str) -> Channel | None:
    if db.get(Channel, channel_id) is None:
        return None
    platform = (
        db.query(ChannelPlatform)
        .filter_by(channel_id=channel_id, language=data.language, platform=data.platform)
        .first()
    )
    if platform is None:
        platform = ChannelPlatform(channel_id=channel_id, language=data.language, platform=data.platform)
        db.add(platform)
    platform.platform_channel_id = data.platform_channel_id
    platform.credentials_encrypted = encrypted
    platform.verified = False
    platform.active = False
    db.commit()
    return _load(db, channel_id)


def verify_credential(db: Session, channel_id: uuid.UUID, data: VerifyCredential) -> Channel | None:
    platform = (
        db.query(ChannelPlatform)
        .filter_by(channel_id=channel_id, language=data.language, platform=data.platform)
        .first()
    )
    if platform is None:
        return None
    platform.verified = True
    platform.active = True
    db.commit()
    return _load(db, channel_id)


def activate(db: Session, channel_id: uuid.UUID) -> Channel | None:
    channel = db.get(Channel, channel_id)
    if channel is None:
        return None
    channel.active = True
    db.commit()
    return _load(db, channel_id)
