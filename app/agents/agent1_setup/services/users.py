import uuid

from sqlalchemy.orm import Session

from app.models import User
from app.schemas.user import UserCreate


def create(db: Session, data: UserCreate, user_id: uuid.UUID | None = None) -> User:
    user = User(
        **({"id": user_id} if user_id else {}),
        name=data.name,
        telegram_chat_id=data.telegram_chat_id,
        primary_language=data.primary_language,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_by_id(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)
