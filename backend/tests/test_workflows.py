from uuid import UUID
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.main import create_app
from app.models import Base, User, WorkflowExecution, WorkflowVersion


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
def workflow_client():
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
        auth_rate_limit_per_minute=20,
    )
    app = create_app(settings, database_engine=engine, redis_client=MemoryRedis())
    with TestClient(app) as client:
        yield client, engine


def register_user(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Workflow User",
            "password": "a-secure-password-123",
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def workflow_definition(name: str = "Example workflow") -> dict:
    return {
        "name": name,
        "description": "Workflow used by the API tests.",
        "trigger": {"type": "manual", "config": {"source": "test"}},
        "steps": [
            {
                "id": "prepare",
                "name": "Prepare",
                "type": "action",
                "inputs": {"operation": "prepare"},
                "dependencies": [],
                "timeout_seconds": 60,
                "retry": {"max_attempts": 2, "backoff_seconds": 5},
            },
            {
                "id": "review",
                "name": "Review",
                "type": "approval",
                "dependencies": ["prepare"],
                "approval": {"required": True, "reason": "Human review required"},
            },
        ],
        "metadata": {"team": "platform"},
    }


def create_workflow(client: TestClient, headers: dict[str, str], name: str):
    return client.post(
        "/api/v1/workflows",
        headers=headers,
        json=workflow_definition(name),
    )


def test_workflow_crud_versions_publish_archive_and_duplicate(workflow_client):
    client, engine = workflow_client
    headers = register_user(client, "workflow-owner@example.com")

    created = create_workflow(client, headers, "Build pipeline")
    assert created.status_code == 201
    initial = created.json()["data"]
    workflow_id = initial["id"]
    assert initial["current_version"] == 1
    assert initial["is_published"] is False
    assert initial["steps"][1]["approval"]["required"] is True

    fetched = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["data"]["trigger"]["type"] == "manual"

    updated_definition = workflow_definition("Build and review")
    updated_definition["steps"][0]["inputs"]["operation"] = "build"
    updated = client.put(
        f"/api/v1/workflows/{workflow_id}",
        headers=headers,
        json=updated_definition,
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["current_version"] == 2
    assert updated.json()["data"]["name"] == "Build and review"

    history = client.get(
        f"/api/v1/workflows/{workflow_id}/versions?limit=1&offset=0",
        headers=headers,
    )
    assert history.status_code == 200
    assert history.json()["pagination"] == {"limit": 1, "offset": 0, "total": 2}
    assert history.json()["data"][0]["version"] == 2
    previous_page = client.get(
        f"/api/v1/workflows/{workflow_id}/versions?limit=1&offset=1",
        headers=headers,
    )
    assert previous_page.json()["data"][0]["version"] == 1

    published = client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=headers)
    assert published.status_code == 200
    assert published.json()["data"]["is_published"] is True
    version_two = client.get(
        f"/api/v1/workflows/{workflow_id}/versions/2",
        headers=headers,
    )
    assert version_two.status_code == 200
    assert version_two.json()["data"]["published_at"] is not None
    assert version_two.json()["data"]["definition"]["steps"][0]["inputs"] == {
        "operation": "build"
    }

    with Session(engine) as db:
        published_version = db.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == UUID(workflow_id),
                WorkflowVersion.version == 2,
            )
        )
        assert published_version is not None
        published_version.definition = workflow_definition("Mutated published version")
        with pytest.raises(ValueError, match="Published workflow versions are immutable"):
            db.flush()
        db.rollback()

    with Session(engine) as db:
        published_version = db.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == UUID(workflow_id),
                WorkflowVersion.version == 2,
            )
        )
        assert published_version is not None
        db.delete(published_version)
        with pytest.raises(ValueError, match="Published workflow versions cannot be deleted"):
            db.flush()
        db.rollback()

    further_update = client.put(
        f"/api/v1/workflows/{workflow_id}",
        headers=headers,
        json=workflow_definition("Next draft"),
    )
    assert further_update.status_code == 200
    assert further_update.json()["data"]["current_version"] == 3
    assert further_update.json()["data"]["published_version"] == 2
    assert further_update.json()["data"]["is_published"] is False
    assert client.get(
        f"/api/v1/workflows/{workflow_id}/versions/2",
        headers=headers,
    ).json()["data"]["definition"]["name"] == "Build and review"

    archive = client.post(f"/api/v1/workflows/{workflow_id}/archive", headers=headers)
    assert archive.status_code == 200
    assert archive.json()["data"]["archived_at"] is not None
    archived_list = client.get(
        "/api/v1/workflows?archived=true",
        headers=headers,
    )
    assert archived_list.json()["pagination"]["total"] == 1
    assert client.get("/api/v1/workflows", headers=headers).json()["pagination"]["total"] == 0
    cannot_update = client.put(
        f"/api/v1/workflows/{workflow_id}",
        headers=headers,
        json=workflow_definition("Archived edit"),
    )
    assert cannot_update.status_code == 409

    duplicate = client.post(
        f"/api/v1/workflows/{workflow_id}/duplicate",
        headers=headers,
    )
    assert duplicate.status_code == 201
    duplicate_data = duplicate.json()["data"]
    assert duplicate_data["name"] == "Next draft (copy)"
    assert duplicate_data["current_version"] == 1
    assert duplicate_data["published_version"] is None


def test_workflow_endpoints_enforce_owner_scoped_access(workflow_client):
    client, _ = workflow_client
    owner_headers = register_user(client, "owner@example.com")
    stranger_headers = register_user(client, "stranger@example.com")
    created = create_workflow(client, owner_headers, "Private workflow")
    workflow_id = created.json()["data"]["id"]

    assert client.get(
        f"/api/v1/workflows/{workflow_id}",
        headers=stranger_headers,
    ).status_code == 404
    assert client.get(
        "/api/v1/workflows",
        headers=stranger_headers,
    ).json()["pagination"]["total"] == 0
    assert client.put(
        f"/api/v1/workflows/{workflow_id}",
        headers=stranger_headers,
        json=workflow_definition("Hijacked"),
    ).status_code == 404
    assert client.post(
        f"/api/v1/workflows/{workflow_id}/publish",
        headers=stranger_headers,
    ).status_code == 404
    assert client.get(
        f"/api/v1/workflows/{workflow_id}/versions",
        headers=stranger_headers,
    ).status_code == 404
    assert client.get(
        f"/api/v1/workflows/{workflow_id}/versions/1",
        headers=stranger_headers,
    ).status_code == 404
    assert client.post(
        f"/api/v1/workflows/{workflow_id}/duplicate",
        headers=stranger_headers,
    ).status_code == 404
    assert client.post(
        f"/api/v1/workflows/{workflow_id}/archive",
        headers=stranger_headers,
    ).status_code == 404
    assert client.delete(
        f"/api/v1/workflows/{workflow_id}",
        headers=stranger_headers,
    ).status_code == 404


def test_workflow_list_filters_sorts_and_paginates(workflow_client):
    client, _ = workflow_client
    headers = register_user(client, "listing@example.com")
    names = ["Zulu workflow", "Alpha workflow", "Middle workflow"]
    workflow_ids = []
    for name in names:
        created = create_workflow(client, headers, name)
        assert created.status_code == 201
        workflow_ids.append(created.json()["data"]["id"])

    assert client.post(
        f"/api/v1/workflows/{workflow_ids[0]}/publish",
        headers=headers,
    ).status_code == 200
    assert client.post(
        f"/api/v1/workflows/{workflow_ids[2]}/archive",
        headers=headers,
    ).status_code == 200

    listed = client.get(
        "/api/v1/workflows?search=workflow&sort_by=name&sort_order=asc&limit=1",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json()["pagination"]["total"] == 2
    assert listed.json()["data"][0]["name"] == "Alpha workflow"

    published = client.get("/api/v1/workflows?published=true", headers=headers)
    assert [item["name"] for item in published.json()["data"]] == ["Zulu workflow"]
    archived = client.get("/api/v1/workflows?archived=true", headers=headers)
    assert [item["name"] for item in archived.json()["data"]] == ["Middle workflow"]


def test_invalid_workflow_definitions_are_rejected(workflow_client):
    client, _ = workflow_client
    headers = register_user(client, "invalid@example.com")
    invalid_definitions = []

    empty_steps = workflow_definition("Empty steps")
    empty_steps["steps"] = []
    invalid_definitions.append(empty_steps)

    missing_dependency = workflow_definition("Missing dependency")
    missing_dependency["steps"][1]["dependencies"] = ["does-not-exist"]
    invalid_definitions.append(missing_dependency)

    cycle = workflow_definition("Cycle")
    cycle["steps"][0]["dependencies"] = ["review"]
    invalid_definitions.append(cycle)

    duplicate_ids = workflow_definition("Duplicate IDs")
    duplicate_ids["steps"][1]["id"] = "prepare"
    invalid_definitions.append(duplicate_ids)

    invalid_branch = workflow_definition("Invalid branch")
    invalid_branch["steps"] = [
        {
            "id": "check",
            "name": "Check",
            "type": "condition",
            "branches": [{"condition": "ready", "target_step_id": "missing"}],
        }
    ]
    invalid_definitions.append(invalid_branch)

    approval_mismatch = workflow_definition("Approval mismatch")
    approval_mismatch["steps"][0]["approval"] = {"required": True}
    invalid_definitions.append(approval_mismatch)

    for definition in invalid_definitions:
        response = client.post(
            "/api/v1/workflows",
            headers=headers,
            json=definition,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


def test_conditional_branch_definitions_are_supported(workflow_client):
    client, _ = workflow_client
    headers = register_user(client, "branching@example.com")
    definition = workflow_definition("Conditional workflow")
    definition["steps"] = [
        {
            "id": "check",
            "name": "Check result",
            "type": "condition",
            "branches": [
                {"condition": "result == 'ready'", "target_step_id": "continue"}
            ],
        },
        {
            "id": "continue",
            "name": "Continue",
            "type": "action",
            "dependencies": ["check"],
        },
    ]

    created = client.post("/api/v1/workflows", headers=headers, json=definition)

    assert created.status_code == 201
    assert created.json()["data"]["steps"][0]["branches"][0]["target_step_id"] == "continue"


def test_delete_is_limited_to_unpublished_workflows_without_executions(workflow_client):
    client, engine = workflow_client
    headers = register_user(client, "safe-delete@example.com")
    published = create_workflow(client, headers, "Published")
    published_id = published.json()["data"]["id"]
    client.post(f"/api/v1/workflows/{published_id}/publish", headers=headers)
    assert client.delete(
        f"/api/v1/workflows/{published_id}",
        headers=headers,
    ).status_code == 409

    with Session(engine) as db:
        owner = db.scalar(select(User).where(User.email == "safe-delete@example.com"))
        assert owner is not None
    draft = create_workflow(client, headers, "Has execution")
    draft_id = draft.json()["data"]["id"]
    with Session(engine) as db:
        workflow_id = UUID(draft_id)
        version_id = db.scalar(
            select(WorkflowVersion.id).where(WorkflowVersion.workflow_id == workflow_id)
        )
        db.add(
            WorkflowExecution(
                owner_id=owner.id,
                workflow_id=workflow_id,
                workflow_version_id=version_id,
            )
        )
        db.commit()
    assert client.delete(
        f"/api/v1/workflows/{draft_id}",
        headers=headers,
    ).status_code == 409

    disposable = create_workflow(client, headers, "Disposable")
    disposable_id = disposable.json()["data"]["id"]
    deleted = client.delete(
        f"/api/v1/workflows/{disposable_id}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["message"] == "Workflow deleted."
    assert client.get(
        f"/api/v1/workflows/{disposable_id}",
        headers=headers,
    ).status_code == 404
