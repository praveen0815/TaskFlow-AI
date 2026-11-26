import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.models import AuditLog, NotificationDraft, ToolExecution, User
from app.schemas.tools import ToolExecuteResult, ToolExecutionRead
from app.services.tools.errors import ToolInputError
from app.services.tools.registry import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _record_audit(
    db: Session,
    user: User,
    execution: ToolExecution,
    action: str,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="tool_execution",
            resource_id=str(execution.id),
            details={
                "tool_name": execution.tool_name,
                "tool_version": execution.tool_version,
                "status": execution.status,
                "input_sha256": execution.input_sha256,
                "output_sha256": execution.output_sha256,
                "error_code": execution.error_code,
                "duration_ms": execution.duration_ms,
            },
        )
    )


def _finish_execution(
    db: Session,
    user: User,
    execution: ToolExecution,
    status: str,
    *,
    started_at: float,
    error_code: str | None = None,
    output_sha256: str | None = None,
) -> None:
    execution.status = status
    execution.error_code = error_code
    execution.output_sha256 = output_sha256
    execution.duration_ms = max(0, int((perf_counter() - started_at) * 1000))
    execution.completed_at = datetime.now(timezone.utc)
    _record_audit(db, user, execution, f"tool.execution_{status}")
    db.commit()


def _read_execution(execution: ToolExecution) -> ToolExecutionRead:
    return ToolExecutionRead.model_validate(execution)


async def execute_tool(
    db: Session,
    user: User,
    registry: ToolRegistry,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> ToolExecuteResult:
    registered = registry.get(name)
    if registered is None:
        raise APIError(404, "tool_not_found", "The requested tool is not registered.")
    metadata = registered.metadata
    timeout = min(
        float(metadata.timeout_seconds),
        timeout_seconds if timeout_seconds is not None else float(metadata.timeout_seconds),
    )
    if timeout <= 0:
        raise APIError(
            504,
            "tool_timeout",
            "No execution time remains for this tool.",
        )
    try:
        normalized_arguments = _canonical_json(arguments)
    except (RecursionError, TypeError, ValueError) as exc:
        raise APIError(422, "invalid_tool_arguments", "Tool arguments must be valid JSON data.") from exc
    if len(normalized_arguments.encode("utf-8")) > 8 * 1024 * 1024:
        raise APIError(
            413,
            "tool_arguments_too_large",
            "Tool arguments exceed the 8 MiB limit.",
        )
    input_sha256 = hashlib.sha256(normalized_arguments.encode("utf-8")).hexdigest()

    started_at = perf_counter()
    execution = ToolExecution(
        owner_id=user.id,
        tool_name=name,
        tool_version=metadata.version,
        status="running",
        input_sha256=input_sha256,
    )
    db.add(execution)
    db.flush()
    _record_audit(db, user, execution, "tool.execution_started")
    db.commit()

    if not set(metadata.permissions).issubset(set(user.tool_permissions)):
        _finish_execution(
            db,
            user,
            execution,
            "denied",
            started_at=started_at,
            error_code="tool_permission_denied",
        )
        raise APIError(
            403,
            "tool_permission_denied",
            "The current user is not authorized to execute this tool.",
        )

    if metadata.availability_status != "available" or registered.handler is None:
        _finish_execution(
            db,
            user,
            execution,
            "failed",
            started_at=started_at,
            error_code="tool_unavailable",
        )
        raise APIError(
            503,
            "tool_unavailable",
            "This integration is not configured and cannot be executed.",
        )

    try:
        Draft202012Validator(metadata.input_schema).validate(arguments)
    except JsonSchemaValidationError as exc:
        _finish_execution(
            db,
            user,
            execution,
            "rejected",
            started_at=started_at,
            error_code="invalid_tool_arguments",
        )
        raise APIError(
            422,
            "invalid_tool_arguments",
            "Tool arguments do not match the registered input schema.",
        ) from exc

    completed = False
    try:
        result = await asyncio.wait_for(
            registered.handler(arguments, ToolContext(db=db, user=user)),
            timeout=timeout,
        )
        normalized_json = _canonical_json(result)
        if len(normalized_json.encode("utf-8")) > 8 * 1024 * 1024:
            raise APIError(
                502,
                "tool_result_too_large",
                "The tool returned a result larger than the 8 MiB limit.",
            )
        normalized_result = json.loads(normalized_json)
        Draft202012Validator(metadata.output_schema).validate(normalized_result)
        completed = True
    except asyncio.TimeoutError as exc:
        _finish_execution(
            db,
            user,
            execution,
            "timed_out",
            started_at=started_at,
            error_code="tool_timeout",
        )
        raise APIError(
            504,
            "tool_timeout",
            "The tool exceeded its configured execution timeout.",
        ) from exc
    except ToolInputError as exc:
        _finish_execution(
            db,
            user,
            execution,
            "rejected",
            started_at=started_at,
            error_code="invalid_tool_arguments",
        )
        raise APIError(422, "invalid_tool_arguments", str(exc)) from exc
    except JsonSchemaValidationError as exc:
        _finish_execution(
            db,
            user,
            execution,
            "failed",
            started_at=started_at,
            error_code="invalid_tool_output",
        )
        raise APIError(
            502,
            "invalid_tool_output",
            "The tool returned data that does not match its output schema.",
        ) from exc
    except SchemaError as exc:
        _finish_execution(
            db,
            user,
            execution,
            "failed",
            started_at=started_at,
            error_code="invalid_tool_schema",
        )
        logger.exception("Registered tool schema is invalid.", exc_info=exc)
        raise APIError(500, "invalid_tool_schema", "A registered tool schema is invalid.") from exc
    finally:
        if not completed and execution.status == "running":
            _finish_execution(
                db,
                user,
                execution,
                "failed",
                started_at=started_at,
                error_code="tool_execution_failed",
            )

    output_sha256 = hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()
    _finish_execution(
        db,
        user,
        execution,
        "succeeded",
        started_at=started_at,
        output_sha256=output_sha256,
    )
    return ToolExecuteResult(execution=_read_execution(execution), result=normalized_result)


def list_tool_executions(
    db: Session,
    user: User,
    *,
    limit: int,
    offset: int,
) -> tuple[list[ToolExecution], int | None]:
    records = list(
        db.scalars(
            select(ToolExecution)
            .where(ToolExecution.owner_id == user.id)
            .order_by(ToolExecution.created_at.desc(), ToolExecution.id)
            .limit(limit + 1)
            .offset(offset)
        ).all()
    )
    has_more = len(records) > limit
    return records[:limit], offset + limit if has_more else None


def get_notification_draft(
    db: Session,
    user: User,
    draft_id: UUID,
) -> NotificationDraft:
    draft = db.scalar(
        select(NotificationDraft).where(
            NotificationDraft.id == draft_id,
            NotificationDraft.owner_id == user.id,
        )
    )
    if draft is None:
        raise APIError(404, "notification_draft_not_found", "Notification draft not found.")
    return draft
