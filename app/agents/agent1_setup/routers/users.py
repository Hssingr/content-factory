import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.user import UserResponse
from app.services.auth import get_current_user_id
from app.agents.agent1_setup.services import users as users_service

router = APIRouter(prefix="/api/agent1/users", tags=["agent1-users"])


@router.get("/me", response_model=UserResponse)
def get_me(user_id: uuid.UUID = Depends(get_current_user_id), db: Session = Depends(get_db)):
    user = users_service.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user
