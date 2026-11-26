from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db
from app.models import AuditLog, User
from app.schemas.users import UserProfileUpdate, UserRead, UserResponse

router = APIRouter(prefix="/users", tags=["users"])
CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_db)]


@router.get("/me", response_model=UserResponse)
def get_profile(response: Response, user: CurrentUser) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return UserResponse(data=UserRead.model_validate(user))


@router.patch("/me", response_model=UserResponse)
def update_profile(
    payload: UserProfileUpdate,
    response: Response,
    user: CurrentUser,
    db: DatabaseSession,
) -> UserResponse:
    user.display_name = payload.display_name
    db.add(
        AuditLog(
            user_id=user.id,
            action="user.profile_updated",
            resource_type="user",
            resource_id=str(user.id),
            details={},
        )
    )
    db.flush()
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return UserResponse(data=UserRead.model_validate(user))
