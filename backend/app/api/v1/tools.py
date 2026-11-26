from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db
from app.models import User
from app.schemas.tools import (
    NotificationDraftRead,
    NotificationDraftResponse,
    ToolExecuteRequest,
    ToolExecutionHistoryResponse,
    ToolExecutionRead,
    ToolExecutionResponse,
    ToolRegistryResponse,
)
from app.services.rate_limit import auth_rate_limit
from app.services.tools.execution import (
    execute_tool,
    get_notification_draft,
    list_tool_executions,
)

router = APIRouter(prefix="/tools", tags=["tools"])
CurrentUser = Annotated[User, Depends(get_current_user)]
DatabaseSession = Annotated[Session, Depends(get_db)]
RateLimit = Annotated[None, Depends(auth_rate_limit)]


@router.get("", response_model=ToolRegistryResponse)
def list_registered_tools(request: Request, _user: CurrentUser) -> ToolRegistryResponse:
    return ToolRegistryResponse(data=list(request.app.state.tool_registry.catalog()))


@router.post("/{tool_name}/execute", response_model=ToolExecutionResponse)
async def execute_registered_tool(
    tool_name: str,
    payload: ToolExecuteRequest,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
    _: RateLimit,
) -> ToolExecutionResponse:
    result = await execute_tool(
        db,
        user,
        request.app.state.tool_registry,
        tool_name,
        payload.arguments,
    )
    return ToolExecutionResponse(data=result)


@router.get("/executions", response_model=ToolExecutionHistoryResponse)
def list_my_tool_executions(
    user: CurrentUser,
    db: DatabaseSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> ToolExecutionHistoryResponse:
    records, next_offset = list_tool_executions(
        db,
        user,
        limit=limit,
        offset=offset,
    )
    return ToolExecutionHistoryResponse(
        data=[ToolExecutionRead.model_validate(record) for record in records],
        next_offset=next_offset,
    )


@router.get(
    "/notification-drafts/{draft_id}",
    response_model=NotificationDraftResponse,
)
def get_my_notification_draft(
    draft_id: Annotated[UUID, Path()],
    user: CurrentUser,
    db: DatabaseSession,
) -> NotificationDraftResponse:
    draft = get_notification_draft(db, user, draft_id)
    return NotificationDraftResponse(
        data=NotificationDraftRead.model_validate(draft)
    )
