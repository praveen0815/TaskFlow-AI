import asyncio
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.errors import APIError
from app.main import create_app
from app.models import (
    Base,
    ToolExecution,
    User,
    WorkflowExecution,
    WorkflowStep,
    WorkflowVersion,
)
from app.schemas.planning import ToolCatalogEntry
from app.services.execution_engine import (
    WorkflowExecutionService,
    recover_interrupted_executions,
)
from app.services.tools import (
    RegisteredTool,
    ToolRegistry,
    create_default_tool_registry,
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
def execution_client():
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
            "display_name": "Execution User",
            "password": "a-secure-password-123",
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def make_definition(name: str, steps: list[dict]) -> dict:
    return {
        "name": name,
        "description": "Workflow execution test.",
        "trigger": {"type": "manual", "config": {}},
        "steps": steps,
    }


def create_published_workflow(
    client: TestClient,
    headers: dict[str, str],
    definition: dict,
) -> str:
    created = client.post("/api/v1/workflows", headers=headers, json=definition)
    assert created.status_code == 201
    workflow_id = created.json()["data"]["id"]
    published = client.post(
        f"/api/v1/workflows/{workflow_id}/publish",
        headers=headers,
    )
    assert published.status_code == 200
    return workflow_id


def calculator_step(step_id: str, expression: str, *, dependencies=None) -> dict:
    return {
        "id": step_id,
        "name": step_id.title(),
        "type": "action",
        "tool": "calculator.evaluate",
        "inputs": {"expression": expression},
        "dependencies": dependencies or [],
    }


def test_execution_runs_registered_steps_and_selects_conditional_branch(execution_client):
    client, engine, _app = execution_client
    headers = register_user(client, "execution-success@example.com")
    results = []

    for expression, expected in (("2 + 3", "accepted"), ("2 + 2", "rejected")):
        workflow_id = create_published_workflow(
            client,
            headers,
            make_definition(
                f"Conditional {expected}",
                [
                    calculator_step("calculate", expression),
                    {
                        "id": "check",
                        "name": "Check result",
                        "type": "condition",
                        "dependencies": ["calculate"],
                        "branches": [
                            {
                                "condition": "{{steps.calculate.output.value}} >= 5",
                                "target_step_id": "accepted",
                            },
                            {
                                "condition": "{{steps.calculate.output.value}} < 5",
                                "target_step_id": "rejected",
                            },
                        ],
                    },
                    {
                        "id": "accepted",
                        "name": "Create accepted draft",
                        "type": "notification",
                        "dependencies": ["check"],
                        "inputs": {"title": "Accepted", "body": "The condition matched."},
                    },
                    {
                        "id": "rejected",
                        "name": "Create rejected draft",
                        "type": "notification",
                        "dependencies": ["check"],
                        "inputs": {"title": "Rejected", "body": "The condition did not match."},
                    },
                ],
            ),
        )
        response = client.post(
            f"/api/v1/workflows/{workflow_id}/executions",
            headers=headers,
            json={"trigger_data": {}},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "completed"
        step_states = {step["step_key"]: step["status"] for step in data["steps"]}
        assert step_states["calculate"] == "completed"
        assert step_states["check"] == "completed"
        assert step_states[expected] == "completed"
        other = "rejected" if expected == "accepted" else "accepted"
        assert step_states[other] == "skipped"
        results.append(data)

    assert results[0]["steps"][0]["output_data"]["value"] == 5
    with Session(engine) as db:
        assert db.scalar(select(ToolExecution).where(ToolExecution.tool_name == "calculator.evaluate"))


def test_execution_idempotency_history_and_owner_scoping(execution_client):
    client, _, _app = execution_client
    owner_headers = register_user(client, "execution-owner@example.com")
    other_headers = register_user(client, "execution-other@example.com")
    workflow_id = create_published_workflow(
        client,
        owner_headers,
        make_definition("Idempotent", [calculator_step("calculate", "2 + 4")]),
    )
    path = f"/api/v1/workflows/{workflow_id}/executions"
    first = client.post(
        path,
        headers={**owner_headers, "Idempotency-Key": "request-001"},
        json={"trigger_data": {"source": "test"}},
    )
    replay = client.post(
        path,
        headers={**owner_headers, "Idempotency-Key": "request-001"},
        json={"trigger_data": {"source": "test"}},
    )
    conflict = client.post(
        path,
        headers={**owner_headers, "Idempotency-Key": "request-001"},
        json={"trigger_data": {"source": "different"}},
    )

    assert first.status_code == replay.status_code == 201
    execution_id = first.json()["data"]["id"]
    assert replay.json()["data"]["id"] == execution_id
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"

    assert client.get(
        f"{path}/{execution_id}",
        headers=other_headers,
    ).status_code == 404
    assert client.post(
        f"{path}/{execution_id}/cancel",
        headers=other_headers,
    ).status_code == 404
    assert client.get(path, headers=owner_headers).json()["pagination"]["total"] == 1
    assert client.get(path, headers=other_headers).status_code == 404
    invalid_key = client.post(
        path,
        headers={**owner_headers, "Idempotency-Key": "not a valid key"},
        json={"trigger_data": {}},
    )
    assert invalid_key.status_code == 422
    assert invalid_key.json()["error"]["code"] == "invalid_idempotency_key"


def test_approval_pauses_resumes_and_cancel_is_idempotent(execution_client):
    client, _, _app = execution_client
    headers = register_user(client, "execution-approval@example.com")
    workflow_id = create_published_workflow(
        client,
        headers,
        make_definition(
            "Approval workflow",
            [
                {
                    "id": "review",
                    "name": "Review",
                    "type": "approval",
                    "approval": {"required": True, "reason": "Human review"},
                },
                {
                    "id": "draft",
                    "name": "Draft notification",
                    "type": "notification",
                    "dependencies": ["review"],
                    "inputs": {"title": "Reviewed", "body": "Approval granted."},
                },
            ],
        ),
    )
    path = f"/api/v1/workflows/{workflow_id}/executions"
    started = client.post(path, headers=headers, json={"trigger_data": {}})
    assert started.status_code == 201
    execution_id = started.json()["data"]["id"]
    assert started.json()["data"]["status"] == "paused"

    approved = client.post(
        f"{path}/{execution_id}/approval",
        headers=headers,
        json={"approved": True, "reason": "Reviewed by owner."},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "completed"
    assert approved.json()["data"]["steps"][0]["output_data"]["approved"] is True

    second_workflow_id = create_published_workflow(
        client,
        headers,
        make_definition(
            "Cancelled approval workflow",
            [
                {
                    "id": "review",
                    "name": "Review",
                    "type": "approval",
                    "approval": {"required": True},
                }
            ],
        ),
    )
    second_path = f"/api/v1/workflows/{second_workflow_id}/executions"
    waiting = client.post(second_path, headers=headers, json={"trigger_data": {}})
    waiting_id = waiting.json()["data"]["id"]
    cancelled = client.post(
        f"{second_path}/{waiting_id}/cancel",
        headers=headers,
    )
    repeated = client.post(
        f"{second_path}/{waiting_id}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == repeated.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert cancelled.json()["data"]["steps"][0]["status"] == "cancelled"


def _custom_tool(name: str, handler) -> RegisteredTool:
    return RegisteredTool(
        metadata=ToolCatalogEntry(
            name=name,
            description="Test-only workflow execution tool.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            version="1.0.0",
            permissions=(f"tool:{name}",),
            risk_classification="low",
            timeout_seconds=5,
            availability_status="available",
        ),
        handler=handler,
    )


def test_step_retries_transient_tool_failures(execution_client):
    client, engine, app = execution_client
    headers = register_user(client, "execution-retry@example.com")
    attempts = 0

    async def flaky_handler(arguments, _context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise APIError(503, "tool_execution_failed", "Temporary failure.")
        return {"value": arguments["value"]}

    app.state.tool_registry = ToolRegistry(
        (*create_default_tool_registry().all(), _custom_tool("test.retry", flaky_handler))
    )
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "execution-retry@example.com"))
        assert user is not None
        user.tool_permissions = [*user.tool_permissions, "tool:test.retry"]
        db.commit()

    workflow_id = create_published_workflow(
        client,
        headers,
        make_definition(
            "Retry workflow",
            [
                {
                    "id": "retry",
                    "name": "Retry action",
                    "type": "action",
                    "tool": "test.retry",
                    "inputs": {"value": 7},
                    "retry": {"max_attempts": 2, "backoff_seconds": 0},
                }
            ],
        ),
    )
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/executions",
        headers=headers,
        json={"trigger_data": {}},
    )

    assert response.status_code == 201
    step = response.json()["data"]["steps"][0]
    assert response.json()["data"]["status"] == "completed", response.json()
    assert step["attempt_count"] == 2
    assert step["output_data"] == {"value": 7}
    with Session(engine) as db:
        statuses = list(
            db.scalars(
                select(ToolExecution.status)
                .where(ToolExecution.tool_name == "test.retry")
                .order_by(ToolExecution.created_at)
            ).all()
        )
    assert statuses == ["failed", "succeeded"]


def test_cancellation_during_a_tool_prevents_later_steps(execution_client):
    client, engine, app = execution_client
    headers = register_user(client, "execution-active-cancel@example.com")

    async def canceling_handler(arguments, context):
        execution = context.db.scalar(
            select(WorkflowExecution).where(
                WorkflowExecution.owner_id == context.user.id,
                WorkflowExecution.status == "running",
            )
        )
        assert execution is not None
        WorkflowExecutionService(app.state.tool_registry).cancel(
            context.db,
            context.user,
            execution.workflow_id,
            execution.id,
        )
        return {"value": arguments["value"]}

    app.state.tool_registry = ToolRegistry(
        (*create_default_tool_registry().all(), _custom_tool("test.cancel", canceling_handler))
    )
    with Session(engine) as db:
        user = db.scalar(
            select(User).where(User.email == "execution-active-cancel@example.com")
        )
        assert user is not None
        user.tool_permissions = [*user.tool_permissions, "tool:test.cancel"]
        db.commit()

    workflow_id = create_published_workflow(
        client,
        headers,
        make_definition(
            "Active cancellation workflow",
            [
                {
                    "id": "cancel",
                    "name": "Cancel during action",
                    "type": "action",
                    "tool": "test.cancel",
                    "inputs": {"value": 1},
                },
                {
                    "id": "later",
                    "name": "Must not run",
                    "type": "notification",
                    "dependencies": ["cancel"],
                    "inputs": {"title": "Later", "body": "This step must be cancelled."},
                },
            ],
        ),
    )
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/executions",
        headers=headers,
        json={"trigger_data": {}},
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "cancelled"
    assert {step["step_key"]: step["status"] for step in data["steps"]} == {
        "cancel": "completed",
        "later": "cancelled",
    }


def test_failed_step_runs_failure_branch_then_fails_execution(execution_client):
    client, engine, app = execution_client
    headers = register_user(client, "execution-failure@example.com")

    async def failing_handler(_arguments, _context):
        raise APIError(503, "tool_execution_failed", "The operation failed.")

    app.state.tool_registry = ToolRegistry(
        (*create_default_tool_registry().all(), _custom_tool("test.fail", failing_handler))
    )
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "execution-failure@example.com"))
        assert user is not None
        user.tool_permissions = [*user.tool_permissions, "tool:test.fail"]
        db.commit()

    workflow_id = create_published_workflow(
        client,
        headers,
        make_definition(
            "Failure branch workflow",
            [
                {
                    "id": "operation",
                    "name": "Failing operation",
                    "type": "action",
                    "tool": "test.fail",
                    "inputs": {"value": 1},
                    "retry": {"max_attempts": 1},
                    "on_failure_step_id": "fallback",
                },
                {
                    "id": "fallback",
                    "name": "Fallback",
                    "type": "notification",
                    "inputs": {"title": "Failed", "body": "Fallback executed."},
                },
            ],
        ),
    )
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/executions",
        headers=headers,
        json={"trigger_data": {}},
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["error_code"] == "tool_execution_failed"
    assert {step["step_key"]: step["status"] for step in data["steps"]} == {
        "operation": "failed",
        "fallback": "completed",
    }


def test_stale_running_execution_recovers_without_replaying_step(execution_client):
    client, engine, _app = execution_client
    headers = register_user(client, "execution-recovery@example.com")
    workflow_id = create_published_workflow(
        client,
        headers,
        make_definition("Recovery workflow", [calculator_step("calculate", "1 + 1")]),
    )
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        user = db.scalar(
            select(User).where(User.email == "execution-recovery@example.com")
        )
        version = db.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == UUID(workflow_id)
            )
        )
        assert user is not None and version is not None
        execution = WorkflowExecution(
            owner_id=user.id,
            workflow_id=UUID(workflow_id),
            workflow_version_id=version.id,
            status="running",
            heartbeat_at=now - timedelta(minutes=2),
            run_started_at=now - timedelta(minutes=2),
        )
        db.add(execution)
        db.flush()
        step = WorkflowStep(
            execution_id=execution.id,
            step_key="calculate",
            step_order=0,
            status="running",
            attempt_count=1,
        )
        db.add(step)
        cancelled_execution = WorkflowExecution(
            owner_id=user.id,
            workflow_id=UUID(workflow_id),
            workflow_version_id=version.id,
            status="cancelled",
            error_code="execution_cancelled",
            heartbeat_at=now - timedelta(minutes=2),
            completed_at=now - timedelta(minutes=2),
        )
        db.add(cancelled_execution)
        db.flush()
        cancelled_step = WorkflowStep(
            execution_id=cancelled_execution.id,
            step_key="calculate",
            step_order=0,
            status="running",
            attempt_count=1,
        )
        db.add(cancelled_step)
        db.commit()

        assert recover_interrupted_executions(
            db,
            now=now,
            stale_after_seconds=30,
        ) == 2
        db.refresh(execution)
        db.refresh(step)
        db.refresh(cancelled_execution)
        db.refresh(cancelled_step)
        assert execution.status == "failed"
        assert execution.error_code == "execution_interrupted"
        assert execution.checkpoint_data["recovery"]["disposition"] == "failed_without_replay"
        assert step.status == "failed"
        assert step.error_code == "execution_interrupted"
        assert cancelled_execution.status == "cancelled"
        assert cancelled_execution.checkpoint_data["recovery"]["disposition"] == (
            "cancelled_without_replay"
        )
        assert cancelled_step.status == "cancelled"
        assert cancelled_step.error_code == "execution_cancelled_outcome_uncertain"


def test_execution_active_duration_limit_fails_without_dispatch(execution_client):
    client, engine, app = execution_client
    headers = register_user(client, "execution-time-limit@example.com")
    workflow_id = create_published_workflow(
        client,
        headers,
        make_definition("Time-limited workflow", [calculator_step("calculate", "1 + 1")]),
    )

    with Session(engine) as db:
        user = db.scalar(
            select(User).where(User.email == "execution-time-limit@example.com")
        )
        assert user is not None
        execution = asyncio.run(
            WorkflowExecutionService(
                app.state.tool_registry,
                max_duration_seconds=0,
            ).start(
                db,
                user,
                UUID(workflow_id),
                {},
                "zero-duration",
            )
        )

    assert execution.status == "failed"
    assert execution.error_code == "execution_timeout"
    assert execution.steps[0].status == "skipped"
