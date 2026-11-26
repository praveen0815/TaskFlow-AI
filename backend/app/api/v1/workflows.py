from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db
from app.models import User
from app.schemas.planning import PlanningRequest, PlanningResponse
from app.schemas.workflows import (
    DuplicateWorkflowRequest,
    Pagination,
    WorkflowActionResponse,
    WorkflowDefinition,
    WorkflowExecutionApprovalRequest,
    WorkflowExecutionListResponse,
    WorkflowExecutionResponse,
    WorkflowExecutionStartRequest,
    WorkflowListResponse,
    WorkflowRead,
    WorkflowResponse,
    WorkflowVersionListResponse,
    WorkflowVersionRead,
    WorkflowVersionResponse,
)
from app.services.execution_engine import (
    WorkflowExecutionService,
    get_execution,
    list_executions,
)
from app.services.planning.providers import create_planning_provider
from app.services.planning.service import WorkflowPlanningService
from app.services.rate_limit import auth_rate_limit
from app.services.workflows import (
    archive_workflow,
    create_workflow,
    delete_workflow,
    duplicate_workflow,
    get_workflow,
    get_workflow_version,
    list_workflow_versions,
    list_workflows,
    publish_workflow,
    update_workflow,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])
CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_db)]
RateLimit = Annotated[None, Depends(auth_rate_limit)]


@router.post(
    "/{workflow_id}/executions",
    response_model=WorkflowExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_execution(
    workflow_id: UUID,
    payload: WorkflowExecutionStartRequest,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
    _: RateLimit,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=120),
    ] = None,
) -> WorkflowExecutionResponse:
    execution = await WorkflowExecutionService(
        request.app.state.tool_registry,
    ).start(
        db,
        user,
        workflow_id,
        payload.trigger_data,
        idempotency_key,
    )
    return WorkflowExecutionResponse(data=execution)


@router.get(
    "/{workflow_id}/executions",
    response_model=WorkflowExecutionListResponse,
)
def list_workflow_executions(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> WorkflowExecutionListResponse:
    executions, total = list_executions(
        db,
        user,
        workflow_id,
        limit=limit,
        offset=offset,
    )
    return WorkflowExecutionListResponse(
        data=executions,
        pagination=Pagination(limit=limit, offset=offset, total=total),
    )


@router.get(
    "/{workflow_id}/executions/{execution_id}",
    response_model=WorkflowExecutionResponse,
)
def get_workflow_execution(
    workflow_id: UUID,
    execution_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowExecutionResponse:
    return WorkflowExecutionResponse(
        data=get_execution(db, user, workflow_id, execution_id)
    )


@router.post(
    "/{workflow_id}/executions/{execution_id}/cancel",
    response_model=WorkflowExecutionResponse,
)
def cancel_workflow_execution(
    workflow_id: UUID,
    execution_id: UUID,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowExecutionResponse:
    return WorkflowExecutionResponse(
        data=WorkflowExecutionService(
            request.app.state.tool_registry,
        ).cancel(db, user, workflow_id, execution_id)
    )


@router.post(
    "/{workflow_id}/executions/{execution_id}/approval",
    response_model=WorkflowExecutionResponse,
)
async def approve_workflow_execution(
    workflow_id: UUID,
    execution_id: UUID,
    payload: WorkflowExecutionApprovalRequest,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
    _: RateLimit,
) -> WorkflowExecutionResponse:
    execution = await WorkflowExecutionService(
        request.app.state.tool_registry,
    ).approve(
        db,
        user,
        workflow_id,
        execution_id,
        payload.approved,
        payload.reason,
    )
    return WorkflowExecutionResponse(data=execution)


@router.post(
    "/plan",
    response_model=PlanningResponse,
    status_code=status.HTTP_201_CREATED,
)
async def plan_workflow(
    payload: PlanningRequest,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
    _: RateLimit,
) -> PlanningResponse:
    provider = request.app.state.planning_provider
    if provider is None:
        provider = create_planning_provider(request.app.state.settings)
        request.app.state.planning_provider = provider
    result = await WorkflowPlanningService(
        provider,
        request.app.state.tool_registry,
    ).create_draft(
        db,
        user,
        payload.prompt,
    )
    return PlanningResponse(data=result)


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
def create(
    definition: WorkflowDefinition,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowResponse:
    return WorkflowResponse(data=create_workflow(db, user, definition))


@router.get("", response_model=WorkflowListResponse)
def list_owned(
    user: CurrentUser,
    db: DatabaseSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    archived: bool = False,
    published: bool | None = None,
    search: str | None = Query(default=None, min_length=1, max_length=100),
    sort_by: Literal["name", "created_at", "updated_at"] = "updated_at",
    sort_order: Literal["asc", "desc"] = "desc",
) -> WorkflowListResponse:
    workflows, total = list_workflows(
        db,
        user,
        limit=limit,
        offset=offset,
        archived=archived,
        search=search,
        published=published,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return WorkflowListResponse(
        data=workflows,
        pagination=Pagination(limit=limit, offset=offset, total=total),
    )


@router.get("/{workflow_id}", response_model=WorkflowResponse)
def get(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowResponse:
    return WorkflowResponse(data=get_workflow(db, user, workflow_id))


@router.put("/{workflow_id}", response_model=WorkflowResponse)
def update(
    workflow_id: UUID,
    definition: WorkflowDefinition,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowResponse:
    return WorkflowResponse(data=update_workflow(db, user, workflow_id, definition))


@router.post("/{workflow_id}/archive", response_model=WorkflowResponse)
def archive(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowResponse:
    return WorkflowResponse(data=archive_workflow(db, user, workflow_id))


@router.delete("/{workflow_id}", response_model=WorkflowActionResponse)
def delete(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowActionResponse:
    delete_workflow(db, user, workflow_id)
    return WorkflowActionResponse(data={"message": "Workflow deleted."})


@router.post(
    "/{workflow_id}/duplicate",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
def duplicate(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
    payload: DuplicateWorkflowRequest | None = None,
) -> WorkflowResponse:
    return WorkflowResponse(
        data=duplicate_workflow(db, user, workflow_id, payload.name if payload else None)
    )


@router.post("/{workflow_id}/publish", response_model=WorkflowResponse)
def publish(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
) -> WorkflowResponse:
    return WorkflowResponse(data=publish_workflow(db, user, workflow_id))


@router.get(
    "/{workflow_id}/versions",
    response_model=WorkflowVersionListResponse,
)
def list_versions(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> WorkflowVersionListResponse:
    versions, total = list_workflow_versions(
        db,
        user,
        workflow_id,
        limit=limit,
        offset=offset,
    )
    return WorkflowVersionListResponse(
        data=[WorkflowVersionRead.model_validate(version) for version in versions],
        pagination=Pagination(limit=limit, offset=offset, total=total),
    )


@router.get(
    "/{workflow_id}/versions/{version_number}",
    response_model=WorkflowVersionResponse,
)
def get_version(
    workflow_id: UUID,
    user: CurrentUser,
    db: DatabaseSession,
    version_number: int = Path(ge=1),
) -> WorkflowVersionResponse:
    version = get_workflow_version(db, user, workflow_id, version_number)
    return WorkflowVersionResponse(
        data=WorkflowVersionRead.model_validate(version)
    )
