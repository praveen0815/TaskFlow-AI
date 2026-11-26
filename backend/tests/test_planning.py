from copy import deepcopy
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.main import create_app
from app.models import Base, User, Workflow
from app.schemas.planning import IntentInterpretation
from app.services.planning.errors import PlanningError
from app.services.planning.providers import create_planning_provider


class MockLLMProviderForTests:
    """Test-only fake; this provider is never registered in production settings."""

    name = "test-mock"

    def __init__(self, plan_output: object | None = None) -> None:
        self.plan_output = plan_output if plan_output is not None else valid_plan()
        self.available_tools: list[str] = []

    async def interpret(self, request: str) -> IntentInterpretation:
        return IntentInterpretation(
            summary="Classify incoming support tickets and prepare a reviewed response.",
            workflow_name="Handle support ticket",
            trigger_type="event",
            goals=["classify ticket", "assign team", "prepare response"],
            approval_required=True,
        )

    async def plan(self, request, intent, tool_catalog):
        self.available_tools = [tool.name for tool in tool_catalog]
        return deepcopy(self.plan_output)


def valid_plan() -> dict:
    return {
        "workflow": {
            "name": "Handle support ticket",
            "description": "Classify and prepare a response for review.",
            "trigger": {
                "type": "event",
                "config": {"event": "support.ticket.created"},
            },
            "steps": [
                {
                    "id": "classify",
                    "name": "Normalize ticket",
                    "type": "action",
                    "tool": "text.transform",
                    "inputs": {
                        "text": "{{trigger.ticket}}",
                        "operation": "trim",
                    },
                },
                {
                    "id": "assign",
                    "name": "Draft response notification",
                    "type": "action",
                    "tool": "notification.draft",
                    "inputs": {
                        "title": "Support ticket response ready",
                        "body": "{{steps.classify.output.text}}",
                    },
                    "dependencies": ["classify"],
                    "retry": {"max_attempts": 3, "backoff_seconds": 10},
                    "on_failure_step_id": "prepare",
                },
                {
                    "id": "prepare",
                    "name": "Draft follow-up notification",
                    "type": "action",
                    "tool": "notification.draft",
                    "inputs": {
                        "title": "Support workflow needs attention",
                        "body": "The initial notification draft could not be created.",
                    },
                    "dependencies": ["assign"],
                },
                {
                    "id": "review",
                    "name": "Review response",
                    "type": "approval",
                    "dependencies": ["prepare"],
                    "approval": {
                        "required": True,
                        "reason": "A person must review the draft before sending.",
                    },
                },
                {
                    "id": "priority_check",
                    "name": "Check priority",
                    "type": "condition",
                    "dependencies": ["classify"],
                    "branches": [
                        {
                            "condition": "normalized ticket is non-empty",
                            "target_step_id": "urgent_notice",
                        }
                    ],
                },
                {
                    "id": "urgent_notice",
                    "name": "Normalize branch text",
                    "type": "action",
                    "tool": "text.transform",
                    "inputs": {
                        "text": "{{steps.classify.output.text}}",
                        "operation": "uppercase",
                    },
                    "dependencies": ["priority_check"],
                },
            ],
            "metadata": {"source": "planning-agent"},
        },
        "rationale": "The plan classifies, assigns and drafts without sending automatically.",
        "assumptions": ["The incoming event provides a ticket payload."],
    }


@pytest.fixture
def planning_client():
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
    return engine


def authorized_client(engine, provider=None):
    settings = Settings(
        jwt_secret_key=SecretStr("unit-test-signing-secret-at-least-32-bytes"),
        auth_rate_limit_per_minute=30,
    )

    class MemoryRedis:
        def __init__(self) -> None:
            self.windows = {}

        def eval(self, script, numkeys, key, window_seconds):
            now = monotonic()
            count, expires_at = self.windows.get(key, (0, now + window_seconds))
            if expires_at <= now:
                count, expires_at = 0, now + window_seconds
            count += 1
            self.windows[key] = (count, expires_at)
            return [count, max(0, int(expires_at - now))]

        def close(self):
            pass

    app = create_app(
        settings,
        database_engine=engine,
        redis_client=MemoryRedis(),
        planning_provider=provider,
    )
    client = TestClient(app)
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "planner@example.com",
            "display_name": "Planner",
            "password": "a-secure-password-123",
        },
    )
    assert registered.status_code == 201
    headers = {
        "Authorization": f"Bearer {registered.json()['data']['access_token']}"
    }
    return client, headers


def test_planning_creates_a_reviewable_unpublished_draft(planning_client):
    provider = MockLLMProviderForTests()
    client, headers = authorized_client(planning_client, provider)
    prompt = (
        "When a support ticket arrives, classify it, determine priority, assign a "
        "team and prepare a response for approval."
    )

    response = client.post(
        "/api/v1/workflows/plan",
        headers=headers,
        json={"prompt": prompt},
    )

    assert response.status_code == 201
    result = response.json()["data"]
    workflow = result["workflow"]
    assert workflow["current_version"] == 1
    assert workflow["is_published"] is False
    assert workflow["published_version"] is None
    assert workflow["steps"][1]["on_failure_step_id"] == "prepare"
    assert result["dependency_order"].index("classify") < result["dependency_order"].index(
        "assign"
    )
    assert result["provider"] == "test-mock"
    assert set(provider.available_tools) == {
        "calculator.evaluate",
        "datetime.now",
        "text.transform",
        "json.process",
        "document.extract",
        "workflow.lookup",
        "notification.draft",
    }

    version_history = client.get(
        f"/api/v1/workflows/{workflow['id']}/versions",
        headers=headers,
    )
    assert version_history.status_code == 200
    version = version_history.json()["data"][0]
    assert version["source_request"] == prompt
    assert version["generation_provider"] == "test-mock"
    assert version["published_at"] is None
    assert client.get(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
    ).json()["data"]["is_published"] is False


@pytest.mark.parametrize(
    ("plan_output", "expected_code", "expected_status"),
    [
        ({"rationale": "missing workflow"}, "planning_provider_invalid_output", 502),
        (
            {
                "workflow": {
                    "name": "Malformed",
                    "trigger": {"type": "event", "config": {}},
                    "steps": [
                        {
                            "id": "first",
                            "name": "First",
                            "type": "action",
                            "tool": "support.classify_ticket",
                            "inputs": {"ticket": "{{trigger.ticket}}"},
                            "dependencies": ["missing"],
                        }
                    ],
                },
                "rationale": "Malformed step graph.",
            },
            "planning_provider_invalid_output",
            502,
        ),
    ],
)
def test_malformed_provider_outputs_do_not_persist_drafts(
    planning_client,
    plan_output,
    expected_code,
    expected_status,
):
    provider = MockLLMProviderForTests(plan_output)
    client, headers = authorized_client(planning_client, provider)

    response = client.post(
        "/api/v1/workflows/plan",
        headers=headers,
        json={"prompt": "Make a safe support-ticket workflow."},
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    with Session(planning_client) as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_planner_rejects_tools_not_in_the_server_catalog(planning_client):
    plan = valid_plan()
    plan["workflow"]["steps"][0]["tool"] = "shell.execute"
    provider = MockLLMProviderForTests(plan)
    client, headers = authorized_client(planning_client, provider)

    response = client.post(
        "/api/v1/workflows/plan",
        headers=headers,
        json={"prompt": "Create a support-ticket workflow."},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unavailable_tool"
    with Session(planning_client) as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_planner_rejects_inputs_outside_the_selected_tool_schema(planning_client):
    plan = valid_plan()
    plan["workflow"]["steps"][0]["inputs"]["script"] = "run arbitrary code"
    provider = MockLLMProviderForTests(plan)
    client, headers = authorized_client(planning_client, provider)

    response = client.post(
        "/api/v1/workflows/plan",
        headers=headers,
        json={"prompt": "Create a support-ticket workflow."},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_tool_inputs"
    with Session(planning_client) as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_planner_rejects_executable_code_in_workflow_text(planning_client):
    plan = valid_plan()
    plan["workflow"]["steps"][0]["inputs"]["ticket"] = (
        "```python\nimport os\nos.system('whoami')\n```"
    )
    provider = MockLLMProviderForTests(plan)
    client, headers = authorized_client(planning_client, provider)

    response = client.post(
        "/api/v1/workflows/plan",
        headers=headers,
        json={"prompt": "Create a support-ticket workflow."},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "executable_content_not_allowed"
    with Session(planning_client) as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_planning_provider_requires_explicit_credentials(planning_client):
    client, headers = authorized_client(planning_client)

    response = client.post(
        "/api/v1/workflows/plan",
        headers=headers,
        json={"prompt": "Create a support-ticket workflow."},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "planning_provider_not_configured"
    with Session(planning_client) as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_planning_provider_requires_an_explicit_model_name():
    settings = Settings(
        openai_api_key=SecretStr("test-only-api-key"),
        planning_model=None,
    )

    with pytest.raises(PlanningError, match="Configure PLANNING_MODEL") as error:
        create_planning_provider(settings)

    assert error.value.code == "planning_model_not_configured"
