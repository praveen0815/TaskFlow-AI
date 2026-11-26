from uuid import uuid4

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

def test_passwords_use_argon2_and_verify_without_exposing_plaintext():
    password = "a-long-test-password"
    password_hash = hash_password(password)

    assert password_hash.startswith("$argon2id$")
    assert password_hash != password
    assert verify_password(password, password_hash)
    assert not verify_password("not-the-password", password_hash)
    assert not verify_password(password, None)


def test_refresh_tokens_are_stored_as_one_way_hashes():
    refresh_token = "high-entropy-refresh-token"

    token_hash = hash_refresh_token(refresh_token)

    assert len(token_hash) == 64
    assert token_hash != refresh_token


def test_access_token_has_short_lived_session_claims():
    settings = Settings(jwt_secret_key="unit-test-signing-secret-at-least-32-bytes")
    user_id = uuid4()
    session_id = uuid4()

    token = create_access_token(user_id, session_id, settings)
    claims = jwt.decode(
        token,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
    )

    assert claims["sub"] == str(user_id)
    assert claims["sid"] == str(session_id)
    assert claims["type"] == "access"


def test_production_settings_require_an_explicit_secret_and_secure_cookie():
    with pytest.raises(ValidationError):
        Settings(app_env="production")

    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            jwt_secret_key="unit-test-signing-secret-at-least-32-bytes",
        )

    settings = Settings(
        app_env="production",
        jwt_secret_key="unit-test-signing-secret-at-least-32-bytes",
        auth_cookie_secure=True,
    )
    assert settings.auth_cookie_secure


def test_production_settings_load_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-signing-secret-at-least-32-bytes")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")

    settings = Settings(_env_file=None)

    assert settings.app_env == "production"
    assert settings.auth_cookie_secure
