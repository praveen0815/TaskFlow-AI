import asyncio
import base64
from time import monotonic
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.main import create_app
from app.models import AuditLog, Base, NotificationDraft, ToolExecution, User
from app.schemas.planning import ToolCatalogEntry
from app.services.tools import RegisteredTool, ToolRegistry, create_default_tool_registry


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

    def close(self) -> None:
        pass


@pytest.fixture
def tool_client():
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
    app = create_app(
        Settings(
            jwt_secret_key=SecretStr("unit-test-signing-secret-at-least-32-bytes"),
            auth_rate_limit_per_minute=30,
        ),
        database_engine=engine,
        redis_client=MemoryRedis(),
    )
    with TestClient(app) as client:
        yield client, engine, app


def register_user(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Tool User",
            "password": "a-secure-password-123",
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def execute(client: TestClient, headers: dict[str, str], tool: str, arguments: dict):
    return client.post(
        f"/api/v1/tools/{tool}/execute",
        headers=headers,
        json={"arguments": arguments},
    )


def test_safe_built_in_tool_executes_and_is_audited(tool_client):
    client, engine, _app = tool_client
    headers = register_user(client, "calculator@example.com")

    response = execute(client, headers, "calculator.evaluate", {"expression": "(2 + 5) * 2"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result"] == {"expression": "(2 + 5) * 2", "value": 14}
    assert data["execution"]["status"] == "succeeded"
    assert len(data["execution"]["input_sha256"]) == 64
    with Session(engine) as db:
        record = db.get(ToolExecution, UUID(data["execution"]["id"]))
        assert record is not None
        assert record.output_sha256 is not None
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.resource_id == str(record.id),
                AuditLog.action == "tool.execution_succeeded",
            )
        )
        assert audit is not None
        assert "arguments" not in audit.details
        assert "result" not in audit.details


def test_registry_tools_transform_text_extract_files_and_process_json(tool_client):
    client, _, _app = tool_client
    headers = register_user(client, "built-ins@example.com")
    registry = client.get("/api/v1/tools", headers=headers)
    calculator = next(
        item
        for item in registry.json()["data"]
        if item["name"] == "calculator.evaluate"
    )
    assert calculator["version"] == "1.0.0"
    assert calculator["risk_classification"] == "low"
    assert calculator["availability_status"] == "available"
    assert calculator["permissions"] == ["tool:calculator.evaluate"]
    assert calculator["input_schema"]["required"] == ["expression"]
    assert calculator["output_schema"]["required"] == ["expression", "value"]

    time_response = execute(
        client,
        headers,
        "datetime.now",
        {"timezone": "Asia/Kolkata"},
    )
    assert time_response.status_code == 200
    assert time_response.json()["data"]["result"]["timezone"] == "Asia/Kolkata"

    text_response = execute(
        client,
        headers,
        "text.transform",
        {"text": " TaskFlow ", "operation": "trim"},
    )
    assert text_response.status_code == 200
    assert text_response.json()["data"]["result"]["text"] == "TaskFlow"

    source = b"first line\nsecond line"
    document_response = execute(
        client,
        headers,
        "document.extract",
        {
            "filename": "notes.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(source).decode("ascii"),
        },
    )
    assert document_response.status_code == 200
    assert document_response.json()["data"]["result"]["text"] == source.decode("utf-8")

    json_response = execute(
        client,
        headers,
        "json.process",
        {"value": {"items": [{"label": "done"}]}, "operation": "get", "path": "items.0.label"},
    )
    assert json_response.status_code == 200
    assert json_response.json()["data"]["result"] == {"result": "done"}


def test_internal_workflow_lookup_never_returns_another_users_workflow(tool_client):
    client, _, _app = tool_client
    owner_headers = register_user(client, "workflow-tool-owner@example.com")
    other_headers = register_user(client, "workflow-tool-other@example.com")
    created = client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={
            "name": "Private workflow",
            "trigger": {"type": "manual", "config": {}},
            "steps": [{"id": "start", "name": "Start", "type": "notification"}],
        },
    )
    assert created.status_code == 201
    workflow_id = created.json()["data"]["id"]

    owner_result = execute(
        client,
        owner_headers,
        "workflow.lookup",
        {"workflow_id": workflow_id},
    )
    other_result = execute(
        client,
        other_headers,
        "workflow.lookup",
        {"workflow_id": workflow_id},
    )

    assert owner_result.status_code == 200
    assert len(owner_result.json()["data"]["result"]["workflows"]) == 1
    assert other_result.status_code == 200
    assert other_result.json()["data"]["result"]["workflows"] == []


def test_invalid_arguments_are_rejected_and_recorded(tool_client):
    client, engine, _app = tool_client
    headers = register_user(client, "invalid-tool-input@example.com")

    response = execute(client, headers, "calculator.evaluate", {"expression": "__import__('os')"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_tool_arguments"
    with Session(engine) as db:
        record = db.scalar(select(ToolExecution).where(ToolExecution.owner_id.is_not(None)))
        assert record is not None
        assert record.status == "rejected"


def test_tool_permissions_and_execution_history_are_user_scoped(tool_client):
    client, engine, _app = tool_client
    headers = register_user(client, "restricted-tool@example.com")
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "restricted-tool@example.com"))
        assert user is not None
        user.tool_permissions = [
            permission
            for permission in user.tool_permissions
            if permission != "tool:calculator.evaluate"
        ]
        db.commit()

    denied = execute(client, headers, "calculator.evaluate", {"expression": "2 + 2"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "tool_permission_denied"
    history = client.get("/api/v1/tools/executions", headers=headers)
    assert history.status_code == 200
    assert history.json()["data"][0]["status"] == "denied"
    with Session(engine) as db:
        assert db.scalar(
            select(AuditLog).where(AuditLog.action == "tool.execution_denied")
        ) is not None


def test_tool_timeout_is_reported_and_recorded(tool_client):
    client, engine, app = tool_client
    headers = register_user(client, "slow-tool@example.com")
    base = create_default_tool_registry()

    async def slow_handler(_arguments, _context):
        await asyncio.sleep(2)
        return {"ok": True}

    slow = RegisteredTool(
        metadata=ToolCatalogEntry(
            name="test.slow",
            description="Test-only slow tool.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            version="1.0.0",
            permissions=("tool:test.slow",),
            risk_classification="low",
            timeout_seconds=1,
            availability_status="available",
        ),
        handler=slow_handler,
    )
    app.state.tool_registry = ToolRegistry((*base.all(), slow))
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "slow-tool@example.com"))
        assert user is not None
        user.tool_permissions = [*user.tool_permissions, "tool:test.slow"]
        db.commit()

    response = execute(client, headers, "test.slow", {})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "tool_timeout"
    with Session(engine) as db:
        record = db.scalar(select(ToolExecution).where(ToolExecution.tool_name == "test.slow"))
        assert record is not None
        assert record.status == "timed_out"
        assert record.error_code == "tool_timeout"


def test_notification_drafts_are_private_and_not_delivered(tool_client):
    client, engine, _app = tool_client
    owner_headers = register_user(client, "draft-owner@example.com")
    other_headers = register_user(client, "draft-other@example.com")

    response = execute(
        client,
        owner_headers,
        "notification.draft",
        {"title": "Review needed", "body": "Please review this notification."},
    )

    assert response.status_code == 200
    result = response.json()["data"]["result"]
    draft_response = client.get(
        f"/api/v1/tools/notification-drafts/{result['id']}",
        headers=owner_headers,
    )
    assert draft_response.status_code == 200
    assert draft_response.json()["data"]["status"] == "draft"
    assert draft_response.json()["data"]["body"] == "Please review this notification."
    assert client.get(
        f"/api/v1/tools/notification-drafts/{result['id']}",
        headers=other_headers,
    ).status_code == 404
    with Session(engine) as db:
        assert db.scalar(select(NotificationDraft)) is not None


def test_external_integrations_are_advertised_but_not_executable(tool_client):
    client, engine, _app = tool_client
    headers = register_user(client, "integration-status@example.com")
    with Session(engine) as db:
        user = db.scalar(
            select(User).where(User.email == "integration-status@example.com")
        )
        assert user is not None
        user.tool_permissions = [
            *user.tool_permissions,
            "integration:email.send",
        ]
        db.commit()
    registry = client.get("/api/v1/tools", headers=headers)
    email_tool = next(
        item
        for item in registry.json()["data"]
        if item["name"] == "integration.email.send"
    )
    assert email_tool["availability_status"] == "unavailable"

    response = execute(client, headers, "integration.email.send", {})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tool_unavailable"
    with Session(engine) as db:
        execution = db.scalar(
            select(ToolExecution).where(
                ToolExecution.tool_name == "integration.email.send"
            )
        )
        assert execution is not None
        assert execution.status == "failed"
