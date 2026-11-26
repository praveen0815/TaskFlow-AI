from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db
from app.core.config import Settings
from app.models import User
from app.schemas.auth import (
    AuthData,
    AuthResponse,
    LoginRequest,
    MessageData,
    MessageResponse,
)
from app.schemas.users import UserCreate, UserRead, UserResponse
from app.services.auth import (
    AuthTokens,
    authenticate_user,
    logout_session,
    refresh_session,
    register_user,
)
from app.services.rate_limit import auth_rate_limit

router = APIRouter(prefix="/auth", tags=["authentication"])
REFRESH_COOKIE_NAME = "taskflow_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"
RateLimit = Annotated[None, Depends(auth_rate_limit)]
DatabaseSession = Annotated[Session, Depends(get_db)]


def _set_refresh_cookie(response: Response, tokens: AuthTokens, settings: Settings) -> None:
    remaining = int(
        (tokens.refresh_expires_at - datetime.now(timezone.utc)).total_seconds()
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=tokens.refresh_token,
        max_age=max(0, remaining),
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _auth_response(tokens: AuthTokens) -> AuthResponse:
    return AuthResponse(
        data=AuthData(
            access_token=tokens.access_token,
            expires_in=tokens.expires_in,
            user=UserRead.model_validate(tokens.user),
        )
    )


@router.post("/register", response_model=AuthResponse, status_code=201)
def register(
    payload: UserCreate,
    request: Request,
    response: Response,
    db: DatabaseSession,
    _: RateLimit,
) -> AuthResponse:
    settings: Settings = request.app.state.settings
    tokens = register_user(
        db,
        payload,
        settings,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, tokens, settings)
    db.commit()
    return _auth_response(tokens)


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DatabaseSession,
    _: RateLimit,
) -> AuthResponse:
    settings: Settings = request.app.state.settings
    tokens = authenticate_user(
        db,
        payload,
        settings,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, tokens, settings)
    db.commit()
    return _auth_response(tokens)


@router.post("/refresh", response_model=AuthResponse)
def refresh(
    request: Request,
    response: Response,
    db: DatabaseSession,
    _: RateLimit,
) -> AuthResponse:
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token is required.")
    settings: Settings = request.app.state.settings
    tokens = refresh_session(
        db,
        refresh_token,
        settings,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, tokens, settings)
    db.commit()
    return _auth_response(tokens)


@router.post("/logout", response_model=MessageResponse)
def logout(
    request: Request,
    response: Response,
    db: DatabaseSession,
) -> MessageResponse:
    logout_session(db, request.cookies.get(REFRESH_COOKIE_NAME))
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=request.app.state.settings.auth_cookie_secure,
        samesite="strict",
    )
    return MessageResponse(data=MessageData(message="Logged out."))


@router.get("/me", response_model=UserResponse)
def current_user(
    response: Response,
    user: User = Depends(get_current_user),
) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return UserResponse(data=UserRead.model_validate(user))
