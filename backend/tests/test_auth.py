from time import monotonic

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.main import create_app
from app.models import (
    AuditLog,
    Base,
    User,
    UserSession,
    Workflow,
    WorkflowExecution,
    WorkflowVersion,
)


class MemoryRedis:
    def __init__(self) -> None:
        self.windows: dict[str, tuple[int, float]] = {}

    def eval(self, script: str, numkeys: int, key: str, window_seconds: int):
        now = monotonic()
        count, expires_at = self.windows.get(key, (0, now + window_seconds))
        if expires_at <= now:
            count, expires_at = 0, now + window_seconds
        count += 1
        self.windows[key] = (count, expires_at)
        return [count, max(0, int(expires_at - now))]

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def api_client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    settings = Settings(
        jwt_secret_key=SecretStr("unit-test-signing-secret-at-least-32-bytes"),
        auth_rate_limit_per_minute=3,
    )
    app = create_app(settings, database_engine=engine, redis_client=MemoryRedis())
    with TestClient(app) as client:
        yield client, engine


def test_register_login_refresh_logout_and_profile_are_user_scoped(api_client):
    client, engine = api_client
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": " Person@Example.com ",
            "display_name": "  Example User  ",
            "password": "a-secure-password-123",
        },
    )

    assert registration.status_code == 201
    body = registration.json()["data"]
    assert body["user"]["email"] == "person@example.com"
    assert body["user"]["display_name"] == "Example User"
    assert body["token_type"] == "bearer"
    access_token = body["access_token"]
    original_refresh_token = client.cookies.get("taskflow_refresh")
    set_cookie = registration.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/api/v1/auth" in set_cookie
    assert registration.headers["cache-control"] == "no-store"

    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "person@example.com"))
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert user.password_hash != "a-secure-password-123"
        assert db.scalar(select(UserSession).where(UserSession.user_id == user.id))

    current_user = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert current_user.status_code == 200
    assert current_user.json()["data"]["id"] == body["user"]["id"]

    profile = client.patch(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"display_name": "Updated Name"},
    )
    assert profile.status_code == 200
    assert profile.headers["cache-control"] == "no-store"
    assert profile.json()["data"]["display_name"] == "Updated Name"

    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    refreshed_access_token = refreshed.json()["data"]["access_token"]
    rotated_refresh_token = client.cookies.get("taskflow_refresh")
    assert rotated_refresh_token != original_refresh_token

    stale_refresh = client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"taskflow_refresh={original_refresh_token}"},
    )
    assert stale_refresh.status_code == 401

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 200
    assert client.cookies.get("taskflow_refresh") is None
    revoked_access = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refreshed_access_token}"},
    )
    assert revoked_access.status_code == 401

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "person@example.com", "password": "a-secure-password-123"},
    )
    assert login.status_code == 200
    login_token = login.json()["data"]["access_token"]
    assert client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {login_token}"},
    ).status_code == 200

    with Session(engine) as db:
        assert db.scalar(
            select(AuditLog).where(AuditLog.action == "user.profile_updated")
        )


def test_registration_conflicts_and_input_validation_use_consistent_errors(api_client):
    client, _ = api_client
    payload = {
        "email": "user@example.com",
        "display_name": "User",
        "password": "a-secure-password-123",
    }

    assert client.post("/api/v1/auth/register", json=payload).status_code == 201
    duplicate = client.post(
        "/api/v1/auth/register",
        json={**payload, "email": "USER@example.com"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "http_error"

    invalid = client.post(
        "/api/v1/auth/register",
        json={**payload, "password": "short"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"


def test_login_rate_limit_is_enforced_and_uses_standard_error_shape(api_client):
    client, _ = api_client
    payload = {"email": "missing@example.com", "password": "wrong-password"}

    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    limited = client.post("/api/v1/auth/login", json=payload)

    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"


def test_workflow_execution_constraints_enforce_owner_and_version(api_client):
    _, engine = api_client
    with Session(engine) as db:
        owner = User(
            email="owner@example.com",
            display_name="Owner",
            password_hash="not-used-in-this-fixture",
        )
        other_user = User(
            email="other@example.com",
            display_name="Other",
            password_hash="not-used-in-this-fixture",
        )
        db.add_all([owner, other_user])
        db.flush()
        workflow = Workflow(owner_id=owner.id, name="Owner workflow")
        unrelated_workflow = Workflow(owner_id=owner.id, name="Another workflow")
        db.add_all([workflow, unrelated_workflow])
        db.flush()
        version = WorkflowVersion(
            workflow_id=workflow.id,
            version=1,
            definition={"steps": []},
        )
        unrelated_version = WorkflowVersion(
            workflow_id=unrelated_workflow.id,
            version=1,
            definition={"steps": []},
        )
        db.add_all([version, unrelated_version])
        db.commit()

        db.add(
            WorkflowExecution(
                owner_id=other_user.id,
                workflow_id=workflow.id,
                workflow_version_id=version.id,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

        db.add(
            WorkflowExecution(
                owner_id=owner.id,
                workflow_id=workflow.id,
                workflow_version_id=unrelated_version.id,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
