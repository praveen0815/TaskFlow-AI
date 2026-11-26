from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.models import AuditLog, User, UserSession
from app.schemas.auth import LoginRequest
from app.schemas.users import UserCreate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthTokens:
    user: User
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_at: datetime


def _record_auth_event(db: Session, user: User, action: str, session_id: str) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="user_session",
            resource_id=session_id,
            details={},
        )
    )


def _create_session(
    db: Session,
    user: User,
    settings: Settings,
    *,
    action: str,
    user_agent: str | None,
    ip_address: str | None,
) -> AuthTokens:
    now = datetime.now(timezone.utc)
    refresh_token = create_refresh_token()
    user_session = UserSession(
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        expires_at=now + timedelta(days=settings.refresh_token_expire_days),
        user_agent=user_agent[:512] if user_agent else None,
        ip_address=ip_address[:45] if ip_address else None,
    )
    db.add(user_session)
    db.flush()
    _record_auth_event(db, user, action, str(user_session.id))
    return AuthTokens(
        user=user,
        access_token=create_access_token(user.id, user_session.id, settings),
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        refresh_expires_at=user_session.expires_at,
    )


def register_user(
    db: Session,
    payload: UserCreate,
    settings: Settings,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> AuthTokens:
    existing_user = db.scalar(select(User.id).where(User.email == str(payload.email)))
    if existing_user is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    user = User(
        email=str(payload.email),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists.",
        ) from exc
    return _create_session(
        db,
        user,
        settings,
        action="auth.registered",
        user_agent=user_agent,
        ip_address=ip_address,
    )


def authenticate_user(
    db: Session,
    payload: LoginRequest,
    settings: Settings,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> AuthTokens:
    email = str(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    valid_password = verify_password(
        payload.password,
        user.password_hash if user is not None else None,
    )
    if user is None or not valid_password or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    return _create_session(
        db,
        user,
        settings,
        action="auth.logged_in",
        user_agent=user_agent,
        ip_address=ip_address,
    )


def refresh_session(
    db: Session,
    refresh_token: str,
    settings: Settings,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> AuthTokens:
    token_hash = hash_refresh_token(refresh_token)
    now = datetime.now(timezone.utc)
    user_session = db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == token_hash,
            UserSession.revoked_at.is_(None),
        )
    )
    if user_session is None:
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired.")

    expires_at = user_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired.")

    user = db.get(User, user_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired.")

    new_refresh_token = create_refresh_token()
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.id == user_session.id,
            UserSession.refresh_token_hash == token_hash,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .values(
            refresh_token_hash=hash_refresh_token(new_refresh_token),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired.")

    _record_auth_event(db, user, "auth.token_refreshed", str(user_session.id))
    return AuthTokens(
        user=user,
        access_token=create_access_token(user.id, user_session.id, settings),
        refresh_token=new_refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        refresh_expires_at=expires_at,
    )


def logout_session(db: Session, refresh_token: str | None) -> None:
    if not refresh_token:
        return
    user_session = db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == hash_refresh_token(refresh_token),
            UserSession.revoked_at.is_(None),
        )
    )
    if user_session is None:
        return

    user_session.revoked_at = datetime.now(timezone.utc)
    user = db.get(User, user_session.user_id)
    if user is not None:
        _record_auth_event(db, user, "auth.logged_out", str(user_session.id))
