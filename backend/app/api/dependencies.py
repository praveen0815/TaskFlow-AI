from collections.abc import Generator
from datetime import datetime, timezone
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import User, UserSession

bearer_scheme = HTTPBearer(auto_error=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Authentication is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise _unauthorized()

    settings: Settings = request.app.state.settings
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "iss", "sid", "sub", "type"]},
        )
        if claims.get("type") != "access":
            raise _unauthorized()
        user_id = UUID(claims["sub"])
        session_id = UUID(claims["sid"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise _unauthorized() from None

    user = db.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    user_session = db.scalar(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
    )
    if user is None or user_session is None:
        raise _unauthorized()

    expires_at = user_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise _unauthorized()
    return user
