import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.models import (
    AuditLog,
    User,
    Workflow,
    WorkflowExecution,
    WorkflowStep,
    WorkflowVersion,
)
from app.schemas.workflows import (
    WorkflowDefinition,
    WorkflowExecutionRead,
    WorkflowExecutionStepRead,
    WorkflowStepDefinition,
)
from app.services.tools.execution import execute_tool
from app.services.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_WORKFLOW_STEPS = 100
MAX_ACTIVE_DURATION_SECONDS = 300
RECOVERY_STALE_SECONDS = 90
_REFERENCE = re.compile(r"^\{\{(trigger|steps)\.([A-Za-z0-9_.-]+)\}\}$")
_CONDITION = re.compile(
    r'^\s*(\{\{(?:trigger|steps)\.[A-Za-z0-9_.-]+\}\})\s+'
    r'(is_empty|is_not_empty|contains|not_contains|==|!=|>=|<=|>|<)'
    r'(?:\s+(.+?))?\s*$'
)


class StepFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _strict_json_loads(value: str) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"{constant} is not valid JSON.")

    return json.loads(value, parse_constant=reject_constant)


def _audit(
    db: Session,
    user: User,
    execution: WorkflowExecution,
    action: str,
    **details: Any,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="workflow_execution",
            resource_id=str(execution.id),
            details={
                "workflow_id": str(execution.workflow_id),
                "workflow_version_id": str(execution.workflow_version_id),
                "status": execution.status,
                **details,
            },
        )
    )


def _step_audit(
    db: Session,
    user: User,
    execution: WorkflowExecution,
    step: WorkflowStep,
    action: str,
) -> None:
    _audit(
        db,
        user,
        execution,
        action,
        step_id=step.step_key,
        step_status=step.status,
        attempt_count=step.attempt_count,
        error_code=step.error_code,
    )


def _persist_progress(
    db: Session,
    execution: WorkflowExecution,
    *,
    now: datetime | None = None,
) -> None:
    current_time = now or _utcnow()
    if execution.run_started_at is not None:
        elapsed = max(
            0.0,
            (current_time - _aware(execution.run_started_at)).total_seconds(),
        )
        execution.active_duration_seconds += elapsed
        execution.run_started_at = current_time
    execution.heartbeat_at = current_time
    db.commit()


def _finish(
    db: Session,
    user: User,
    execution: WorkflowExecution,
    result: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    now = _utcnow()
    if execution.run_started_at is not None:
        execution.active_duration_seconds += max(
            0.0,
            (now - _aware(execution.run_started_at)).total_seconds(),
        )
    execution.run_started_at = None
    execution.heartbeat_at = now
    execution.status = result
    execution.error_code = error_code or execution.error_code
    execution.error_message = error_message or execution.error_message
    execution.completed_at = now
    _audit(db, user, execution, f"workflow.execution_{result}")
    db.commit()


def _validate_reference(
    reference: str,
    *,
    current_step: str,
    ancestors: dict[str, set[str]],
    known_steps: set[str],
) -> None:
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise APIError(
            422,
            "invalid_workflow_reference",
            "Workflow values must use a complete trigger or step-output reference.",
        )
    root, path = match.groups()
    parts = path.split(".")
    if root == "trigger":
        if not parts or any(not part for part in parts):
            raise APIError(422, "invalid_workflow_reference", "A trigger field is required.")
        return
    if len(parts) < 3 or parts[1] != "output" or parts[0] not in known_steps:
        raise APIError(
            422,
            "invalid_workflow_reference",
            "Step references must use {{steps.<id>.output.<field>}}.",
        )
    if parts[0] not in ancestors[current_step]:
        raise APIError(
            422,
            "invalid_workflow_reference",
            f"Step '{current_step}' must depend on referenced step '{parts[0]}'.",
        )
    if any(not part for part in parts[2:]):
        raise APIError(422, "invalid_workflow_reference", "A step output field is required.")


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _find_references(value: Any) -> Iterator[str]:
    for text_value in _walk_strings(value):
        if "{{" not in text_value and "}}" not in text_value:
            continue
        if _REFERENCE.fullmatch(text_value) is None:
            raise APIError(
                422,
                "invalid_workflow_reference",
                "References must occupy an entire input value.",
            )
        yield text_value


def _topological_order(definition: WorkflowDefinition) -> list[str]:
    step_ids = [step.id for step in definition.steps]
    original_order = {step_id: index for index, step_id in enumerate(step_ids)}
    outgoing: dict[str, set[str]] = defaultdict(set)
    indegree = {step_id: 0 for step_id in step_ids}

    def add_edge(source: str, target: str) -> None:
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1

    for step in definition.steps:
        for dependency in step.dependencies:
            add_edge(dependency, step.id)
        for branch in step.branches:
            add_edge(step.id, branch.target_step_id)
        if step.on_failure_step_id is not None:
            add_edge(step.id, step.on_failure_step_id)

    ready = deque(
        sorted(
            (step_id for step_id in step_ids if indegree[step_id] == 0),
            key=original_order.__getitem__,
        )
    )
    result: list[str] = []
    while ready:
        current = ready.popleft()
        result.append(current)
        for target in sorted(outgoing[current], key=original_order.__getitem__):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(result) != len(step_ids):
        raise APIError(422, "invalid_workflow_definition", "Workflow steps contain a cycle.")
    return result


def _ancestors(definition: WorkflowDefinition) -> dict[str, set[str]]:
    predecessors: dict[str, set[str]] = {step.id: set() for step in definition.steps}
    for step in definition.steps:
        for target in (
            [branch.target_step_id for branch in step.branches]
            + ([step.on_failure_step_id] if step.on_failure_step_id else [])
        ):
            predecessors[target].add(step.id)
        for target in step_ids_depending_on(definition, step.id):
            predecessors[target].add(step.id)

    result: dict[str, set[str]] = {}
    for step_id in predecessors:
        found: set[str] = set()
        pending = list(predecessors[step_id])
        while pending:
            ancestor = pending.pop()
            if ancestor not in found:
                found.add(ancestor)
                pending.extend(predecessors[ancestor])
        result[step_id] = found
    return result


def step_ids_depending_on(definition: WorkflowDefinition, step_id: str) -> list[str]:
    return [
        step.id
        for step in definition.steps
        if step_id in step.dependencies
    ]


def _validate_definition(
    definition: WorkflowDefinition,
    user: User,
    registry: ToolRegistry,
) -> list[str]:
    if len(definition.steps) > MAX_WORKFLOW_STEPS:
        raise APIError(
            422,
            "workflow_step_limit_exceeded",
            f"Workflows may contain no more than {MAX_WORKFLOW_STEPS} steps.",
        )
    order = _topological_order(definition)
    known_steps = {step.id for step in definition.steps}
    ancestors = _ancestors(definition)
    for step in definition.steps:
        tool_name = (
            "notification.draft"
            if step.type == "notification"
            else step.tool if step.type == "action" else None
        )
        if step.type == "action" and tool_name is None:
            raise APIError(
                422,
                "workflow_tool_required",
                f"Action step '{step.id}' must select a registered tool.",
            )
        if tool_name is not None:
            registered = registry.get(tool_name)
            if registered is None or registered.metadata.availability_status != "available":
                raise APIError(
                    422,
                    "workflow_tool_unavailable",
                    f"Step '{step.id}' selects a tool that is unavailable.",
                )
            if not set(registered.metadata.permissions).issubset(
                set(user.tool_permissions)
            ):
                raise APIError(
                    403,
                    "tool_permission_denied",
                    f"The current user is not authorized to run step '{step.id}'.",
                )
        for reference in _find_references(step.inputs):
            _validate_reference(
                reference,
                current_step=step.id,
                ancestors=ancestors,
                known_steps=known_steps,
            )
        for branch in step.branches:
            match = _CONDITION.fullmatch(branch.condition)
            if match is None:
                raise APIError(
                    422,
                    "invalid_branch_condition",
                    f"Condition step '{step.id}' uses an unsupported condition expression.",
                )
            _validate_reference(
                match.group(1),
                current_step=step.id,
                ancestors=ancestors,
                known_steps=known_steps,
            )
            operator, raw_expected = match.group(2), match.group(3)
            if operator in {"is_empty", "is_not_empty"}:
                if raw_expected is not None:
                    raise APIError(
                        422,
                        "invalid_branch_condition",
                        "Empty checks do not accept comparison values.",
                    )
            else:
                try:
                    _strict_json_loads(raw_expected or "")
                except (ValueError, RecursionError) as exc:
                    raise APIError(
                        422,
                        "invalid_branch_condition",
                        "Branch comparison values must be valid JSON literals.",
                    ) from exc
    return order


def _get_path(value: Any, parts: list[str]) -> Any:
    current = value
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdecimal() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise StepFailure(
                "workflow_reference_unresolved",
                "A referenced trigger or step output value does not exist.",
            )
    return current


def _resolve_reference(
    reference: str,
    trigger_data: dict[str, Any],
    step_outputs: dict[str, Any],
) -> Any:
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise StepFailure(
            "invalid_workflow_reference",
            "A workflow value reference is malformed.",
        )
    root, path = match.groups()
    parts = path.split(".")
    if root == "trigger":
        return _get_path(trigger_data, parts)
    step_id = parts[0]
    if len(parts) < 3 or parts[1] != "output" or step_id not in step_outputs:
        raise StepFailure(
            "workflow_reference_unresolved",
            "The referenced step output is not available.",
        )
    return _get_path(step_outputs[step_id], parts[2:])


def _resolve_inputs(
    value: Any,
    trigger_data: dict[str, Any],
    step_outputs: dict[str, Any],
) -> Any:
    if isinstance(value, str):
        if "{{" in value or "}}" in value:
            if _REFERENCE.fullmatch(value) is None:
                raise StepFailure(
                    "invalid_workflow_reference",
                    "References must occupy an entire input value.",
                )
            return _resolve_reference(value, trigger_data, step_outputs)
        return value
    if isinstance(value, dict):
        return {
            key: _resolve_inputs(item, trigger_data, step_outputs)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_inputs(item, trigger_data, step_outputs)
            for item in value
        ]
    return value


def _evaluate_condition(
    condition: str,
    trigger_data: dict[str, Any],
    step_outputs: dict[str, Any],
) -> bool:
    match = _CONDITION.fullmatch(condition)
    if match is None:
        raise StepFailure(
            "invalid_branch_condition",
            "The branch condition is not in the supported expression format.",
        )
    reference, operator, raw_expected = match.groups()
    actual = _resolve_reference(reference, trigger_data, step_outputs)
    if operator == "is_empty":
        if raw_expected is not None:
            raise StepFailure("invalid_branch_condition", "is_empty does not accept a value.")
        return actual is None or actual == "" or actual == [] or actual == {}
    if operator == "is_not_empty":
        if raw_expected is not None:
            raise StepFailure(
                "invalid_branch_condition",
                "is_not_empty does not accept a value.",
            )
        return not (actual is None or actual == "" or actual == [] or actual == {})
    if raw_expected is None:
        raise StepFailure("invalid_branch_condition", "A comparison value is required.")
    try:
        expected = _strict_json_loads(raw_expected)
    except (ValueError, RecursionError) as exc:
        raise StepFailure(
            "invalid_branch_condition",
            "Comparison values must be valid JSON literals.",
        ) from exc
    if operator == "==":
        if isinstance(actual, bool) != isinstance(expected, bool):
            return False
        return actual == expected
    if operator == "!=":
        if isinstance(actual, bool) != isinstance(expected, bool):
            return True
        return actual != expected
    if operator in {"contains", "not_contains"}:
        if not isinstance(actual, (str, list, dict)):
            raise StepFailure(
                "invalid_branch_condition",
                "contains operators require text, arrays, or objects.",
            )
        if isinstance(actual, (str, dict)) and not isinstance(expected, str):
            raise StepFailure(
                "invalid_branch_condition",
                "Text and object contains checks require a string value.",
            )
        contains = expected in actual
        return contains if operator == "contains" else not contains
    try:
        if isinstance(actual, bool) or isinstance(expected, bool):
            raise TypeError
        if operator == ">":
            return actual > expected
        if operator == "<":
            return actual < expected
        if operator == ">=":
            return actual >= expected
        return actual <= expected
    except TypeError as exc:
        raise StepFailure(
            "invalid_branch_condition",
            "The comparison values are not compatible.",
        ) from exc


def _execution_read(
    db: Session,
    execution: WorkflowExecution,
) -> WorkflowExecutionRead:
    version = db.get(WorkflowVersion, execution.workflow_version_id)
    if version is None:
        raise APIError(
            409,
            "workflow_version_missing",
            "The workflow version associated with this execution was not found.",
        )
    steps = db.scalars(
        select(WorkflowStep)
        .where(WorkflowStep.execution_id == execution.id)
        .order_by(WorkflowStep.step_order)
    ).all()
    return WorkflowExecutionRead(
        id=execution.id,
        workflow_id=execution.workflow_id,
        workflow_version_id=execution.workflow_version_id,
        workflow_version=version.version,
        status=execution.status,
        trigger_data=execution.trigger_data,
        checkpoint_data=execution.checkpoint_data,
        error_code=execution.error_code,
        error_message=execution.error_message,
        active_duration_seconds=execution.active_duration_seconds,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        created_at=execution.created_at,
        steps=[
            WorkflowExecutionStepRead(
                step_key=step.step_key,
                step_order=step.step_order,
                status=step.status,
                attempt_count=step.attempt_count,
                input_data=step.input_data,
                output_data=step.output_data,
                error_code=step.error_code,
                error_message=step.error_message,
                started_at=step.started_at,
                completed_at=step.completed_at,
            )
            for step in steps
        ],
    )


def _owned_execution(
    db: Session,
    user: User,
    workflow_id: UUID,
    execution_id: UUID,
    *,
    for_update: bool = False,
) -> WorkflowExecution:
    statement = select(WorkflowExecution).where(
        WorkflowExecution.id == execution_id,
        WorkflowExecution.workflow_id == workflow_id,
        WorkflowExecution.owner_id == user.id,
    )
    if for_update:
        statement = statement.with_for_update()
    execution = db.scalar(statement)
    if execution is None:
        raise APIError(404, "workflow_execution_not_found", "Workflow execution not found.")
    return execution


class WorkflowExecutionService:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_duration_seconds: int = MAX_ACTIVE_DURATION_SECONDS,
    ) -> None:
        self.registry = registry
        self.max_duration_seconds = max_duration_seconds

    async def start(
        self,
        db: Session,
        user: User,
        workflow_id: UUID,
        trigger_data: dict[str, Any],
        idempotency_key: str | None,
    ) -> WorkflowExecutionRead:
        if db.scalar(
            select(User.id).where(User.id == user.id).with_for_update()
        ) is None:
            raise APIError(404, "user_not_found", "The current user was not found.")
        workflow = db.scalar(
            select(Workflow)
            .where(Workflow.id == workflow_id, Workflow.owner_id == user.id)
            .with_for_update()
        )
        if workflow is None:
            raise APIError(404, "workflow_not_found", "Workflow not found.")
        if workflow.archived_at is not None:
            raise APIError(409, "workflow_archived", "Archived workflows cannot be executed.")
        if workflow.published_version is None:
            raise APIError(409, "workflow_not_published", "Publish the workflow before executing it.")
        version = db.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.version == workflow.published_version,
            )
        )
        if version is None:
            raise APIError(409, "published_workflow_version_missing", "The published version was not found.")
        try:
            definition = WorkflowDefinition.model_validate(version.definition)
        except ValidationError as exc:
            raise APIError(
                422,
                "invalid_workflow_definition",
                "The published workflow definition is invalid.",
            ) from exc

        try:
            trigger_json = _canonical_json(trigger_data)
            if len(trigger_json.encode("utf-8")) > 1024 * 1024:
                raise APIError(
                    413,
                    "trigger_data_too_large",
                    "Trigger data exceeds the 1 MiB limit.",
                )
            request_hash = hashlib.sha256(
                _canonical_json(
                    {
                        "workflow_id": str(workflow.id),
                        "workflow_version_id": str(version.id),
                        "trigger_data": trigger_data,
                    }
                ).encode("utf-8")
            ).hexdigest()
        except APIError:
            raise
        except (TypeError, ValueError, RecursionError) as exc:
            raise APIError(422, "invalid_trigger_data", "Trigger data must be valid JSON.") from exc

        order = _validate_definition(definition, user, self.registry)
        key = idempotency_key or str(uuid4())
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", key):
            raise APIError(
                422,
                "invalid_idempotency_key",
                "Idempotency keys may contain only letters, numbers, '.', '_', ':', and '-'.",
            )
        existing = db.scalar(
            select(WorkflowExecution).where(
                WorkflowExecution.owner_id == user.id,
                WorkflowExecution.idempotency_key == key,
            )
        )
        if existing is not None:
            if (
                existing.workflow_id != workflow.id
                or existing.workflow_version_id != version.id
                or existing.request_sha256 != request_hash
            ):
                raise APIError(
                    409,
                    "idempotency_key_conflict",
                    "The idempotency key was already used for a different execution request.",
                )
            return _execution_read(db, existing)

        now = _utcnow()
        execution = WorkflowExecution(
            owner_id=user.id,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            status="queued",
            idempotency_key=key,
            request_sha256=request_hash,
            trigger_data=trigger_data,
            checkpoint_data={
                "step_order": order,
                "branch_results": {},
                "failure_target": None,
            },
            heartbeat_at=now,
        )
        db.add(execution)
        db.flush()
        order_by_key = {step_id: index for index, step_id in enumerate(order)}
        steps = [
            WorkflowStep(
                execution_id=execution.id,
                step_key=step_id,
                step_order=order_by_key[step_id],
                status="pending",
                attempt_count=0,
            )
            for step_id in order
        ]
        db.add_all(steps)
        _audit(
            db,
            user,
            execution,
            "workflow.execution_queued",
            step_count=len(steps),
        )
        db.commit()
        await self._run(db, user, execution, definition, order)
        return _execution_read(db, execution)

    async def _run(
        self,
        db: Session,
        user: User,
        execution: WorkflowExecution,
        definition: WorkflowDefinition,
        order: list[str],
    ) -> None:
        db.refresh(execution)
        if execution.status != "queued":
            return
        now = _utcnow()
        execution.status = "running"
        execution.started_at = execution.started_at or now
        execution.run_started_at = now
        execution.heartbeat_at = now
        _audit(db, user, execution, "workflow.execution_started")
        db.commit()

        step_definitions = {step.id: step for step in definition.steps}
        step_rows = {
            step.step_key: step
            for step in db.scalars(
                select(WorkflowStep)
                .where(WorkflowStep.execution_id == execution.id)
                .order_by(WorkflowStep.step_order)
            ).all()
        }
        loop = asyncio.get_running_loop()
        available_duration = max(
            0.0,
            self.max_duration_seconds - execution.active_duration_seconds,
        )
        deadline = loop.time() + available_duration

        for step_id in order:
            db.refresh(execution)
            if execution.status == "cancelled":
                self._cancel_remaining(db, user, execution, step_rows)
                return
            step = step_rows[step_id]
            step_definition = step_definitions[step_id]
            if step.status in {"completed", "failed", "skipped", "cancelled"}:
                continue
            if loop.time() >= deadline:
                self._fail_execution(
                    db,
                    user,
                    execution,
                    step_rows,
                    "execution_timeout",
                    "The workflow exceeded its active execution time limit.",
                )
                return

            failure_target = execution.checkpoint_data.get("failure_target")
            if failure_target is not None and step_id != failure_target:
                self._skip_step(db, user, execution, step, "failure_branch_only")
                continue
            if failure_target is None and self._should_skip(
                db,
                execution,
                step,
                step_definition,
                definition,
                step_rows,
            ):
                continue

            if step_definition.type == "approval":
                step.status = "waiting_approval"
                step.started_at = step.started_at or _utcnow()
                step.output_data = {
                    "required": True,
                    "reason": step_definition.approval.reason,
                }
                execution.status = "paused"
                checkpoint = dict(execution.checkpoint_data)
                checkpoint["waiting_approval_step"] = step_id
                execution.checkpoint_data = checkpoint
                _step_audit(db, user, execution, step, "workflow.step_waiting_approval")
                self._pause(db, user, execution)
                return

            step.status = "ready"
            step.started_at = step.started_at or _utcnow()
            _persist_progress(db, execution)
            step.status = "running"
            _step_audit(db, user, execution, step, "workflow.step_started")
            _persist_progress(db, execution)

            try:
                output = await self._execute_step(
                    db,
                    user,
                    execution,
                    step,
                    step_definition,
                    step_rows,
                    deadline,
                )
            except APIError as exc:
                failure = StepFailure(
                    exc.code,
                    exc.message,
                    retryable=exc.status_code >= 500,
                )
            except StepFailure as exc:
                failure = exc
            except Exception as exc:
                logger.exception(
                    "Workflow step execution raised an unexpected error.",
                    extra={
                        "execution_id": str(execution.id),
                        "step_id": step_id,
                    },
                )
                failure = StepFailure(
                    "step_execution_failed",
                    "The workflow step failed unexpectedly.",
                )
            else:
                db.refresh(execution)
                if output is None:
                    step.status = "cancelled"
                    step.completed_at = _utcnow()
                    _step_audit(db, user, execution, step, "workflow.step_cancelled")
                    _persist_progress(db, execution)
                    self._cancel_remaining(db, user, execution, step_rows)
                    return
                if execution.status == "cancelled":
                    step.status = "completed"
                    step.output_data = output
                    step.completed_at = _utcnow()
                    _step_audit(db, user, execution, step, "workflow.step_completed")
                    _persist_progress(db, execution)
                    self._cancel_remaining(db, user, execution, step_rows)
                    return
                step.status = "completed"
                step.output_data = output
                step.error_code = None
                step.error_message = None
                step.completed_at = _utcnow()
                if step_definition.type == "condition":
                    checkpoint = dict(execution.checkpoint_data)
                    branches = dict(checkpoint.get("branch_results", {}))
                    branches.update(
                        {
                            f"{step_id}:{target}": matched
                            for target, matched in output["branches"].items()
                        }
                    )
                    checkpoint["branch_results"] = branches
                    execution.checkpoint_data = checkpoint
                _step_audit(db, user, execution, step, "workflow.step_completed")
                _persist_progress(db, execution)
                if failure_target == step_id:
                    self._fail_execution(
                        db,
                        user,
                        execution,
                        step_rows,
                        execution.error_code or "step_failed",
                        execution.error_message or "A workflow step failed.",
                    )
                    return
                continue

            db.refresh(execution)
            if execution.status == "cancelled":
                step.status = "cancelled"
                step.completed_at = _utcnow()
                _step_audit(db, user, execution, step, "workflow.step_cancelled")
                _persist_progress(db, execution)
                self._cancel_remaining(db, user, execution, step_rows)
                return
            step.status = "failed"
            step.error_code = failure.code
            step.error_message = failure.message
            step.output_data = {"error_code": failure.code}
            step.completed_at = _utcnow()
            _step_audit(db, user, execution, step, "workflow.step_failed")
            if execution.error_code is None:
                execution.error_code = failure.code
                execution.error_message = failure.message
            if step_definition.on_failure_step_id is not None:
                checkpoint = dict(execution.checkpoint_data)
                checkpoint["failure_target"] = step_definition.on_failure_step_id
                execution.checkpoint_data = checkpoint
                _persist_progress(db, execution)
                continue
            self._fail_execution(
                db,
                user,
                execution,
                step_rows,
                failure.code,
                failure.message,
            )
            return

        self._complete_execution(db, user, execution, step_rows)

    async def _execute_step(
        self,
        db: Session,
        user: User,
        execution: WorkflowExecution,
        step: WorkflowStep,
        definition: WorkflowStepDefinition,
        step_rows: dict[str, WorkflowStep],
        deadline: float,
    ) -> dict[str, Any] | None:
        outputs = {
            step_id: row.output_data
            for step_id, row in step_rows.items()
            if row.status == "completed" and row.output_data is not None
        }
        if definition.type == "condition":
            step.input_data = {
                branch.target_step_id: branch.condition
                for branch in definition.branches
            }
            branch_results = {
                branch.target_step_id: _evaluate_condition(
                    branch.condition,
                    execution.trigger_data,
                    outputs,
                )
                for branch in definition.branches
            }
            return {"branches": branch_results}
        if definition.type == "delay":
            resolved_delay = _resolve_inputs(
                definition.inputs,
                execution.trigger_data,
                outputs,
            )
            seconds = (
                resolved_delay.get("seconds")
                if isinstance(resolved_delay, dict)
                else None
            )
            step.input_data = resolved_delay
            _persist_progress(db, execution)
            if (
                type(seconds) not in {int, float}
                or not math.isfinite(seconds)
                or seconds < 0
            ):
                raise StepFailure(
                    "invalid_delay",
                    "Delay steps require a non-negative numeric seconds input.",
                )
            if seconds > definition.timeout_seconds:
                raise StepFailure(
                    "step_timeout",
                    "The delay exceeds the configured step timeout.",
                )
            if not await self._delay_with_cancellation(
                db,
                execution,
                seconds,
                deadline,
            ):
                return None
            return {"elapsed_seconds": seconds}
        if definition.type == "notification":
            tool_name = "notification.draft"
        elif definition.type == "action":
            tool_name = definition.tool
        else:
            raise StepFailure(
                "unsupported_step_type",
                f"Step type '{definition.type}' is not supported by the execution engine.",
            )
        if tool_name is None:
            raise StepFailure(
                "workflow_tool_required",
                f"Action step '{definition.id}' must select a registered tool.",
            )
        registered = self.registry.get(tool_name)
        if registered is None or registered.metadata.availability_status != "available":
            raise StepFailure(
                "workflow_tool_unavailable",
                f"Step '{definition.id}' selects an unavailable tool.",
            )
        resolved_inputs = _resolve_inputs(
            definition.inputs,
            execution.trigger_data,
            outputs,
        )
        if not isinstance(resolved_inputs, dict):
            raise StepFailure("invalid_step_inputs", "Step inputs must be a JSON object.")
        step.input_data = resolved_inputs
        max_attempts = definition.retry.max_attempts
        step_deadline = min(
            deadline,
            asyncio.get_running_loop().time() + definition.timeout_seconds,
        )
        for attempt in range(1, max_attempts + 1):
            db.refresh(execution)
            if execution.status == "cancelled":
                raise StepFailure(
                    "execution_cancelled",
                    "The workflow was cancelled before the next tool attempt.",
                )
            remaining = step_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise StepFailure(
                    "execution_timeout",
                    "The workflow exceeded its active execution time limit.",
                )
            step.attempt_count = attempt
            _persist_progress(db, execution)
            timeout = min(
                float(definition.timeout_seconds),
                float(registered.metadata.timeout_seconds),
                remaining,
            )
            try:
                result = await execute_tool(
                    db,
                    user,
                    self.registry,
                    tool_name,
                    resolved_inputs,
                    timeout_seconds=timeout,
                )
            except APIError as exc:
                can_retry = (
                    attempt < max_attempts
                    and exc.status_code >= 500
                    and exc.code in {"tool_timeout", "tool_execution_failed"}
                )
                if not can_retry:
                    raise
                remaining_after_failure = (
                    step_deadline - asyncio.get_running_loop().time()
                )
                if remaining_after_failure <= 0:
                    raise
                backoff = min(
                    float(definition.retry.backoff_seconds),
                    remaining_after_failure,
                )
                if backoff > 0 and not await self._delay_with_cancellation(
                    db,
                    execution,
                    backoff,
                    step_deadline,
                ):
                    raise StepFailure(
                        "execution_cancelled",
                        "The workflow was cancelled during a retry delay.",
                    )
            else:
                if not isinstance(result.result, dict):
                    raise StepFailure(
                        "invalid_tool_output",
                        "Workflow action tools must return a JSON object.",
                    )
                return result.result
        raise StepFailure("step_failed", "The workflow step exhausted its retry policy.")

    async def _delay_with_cancellation(
        self,
        db: Session,
        execution: WorkflowExecution,
        seconds: float,
        deadline: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        end_time = loop.time() + seconds
        last_heartbeat = loop.time()
        while loop.time() < end_time:
            if loop.time() >= deadline:
                raise StepFailure(
                    "execution_timeout",
                    "The workflow exceeded its active execution time limit.",
                )
            await asyncio.sleep(min(0.1, end_time - loop.time(), deadline - loop.time()))
            db.refresh(execution)
            if execution.status == "cancelled":
                return False
            if loop.time() - last_heartbeat >= 5:
                _persist_progress(db, execution)
                last_heartbeat = loop.time()
        return True

    @staticmethod
    def _should_skip(
        db: Session,
        execution: WorkflowExecution,
        row: WorkflowStep,
        definition: Any,
        workflow: WorkflowDefinition,
        step_rows: dict[str, WorkflowStep],
    ) -> bool:
        branch_inputs = [
            (source.id, branch.target_step_id)
            for source in workflow.steps
            for branch in source.branches
            if branch.target_step_id == definition.id
        ]
        branch_results = execution.checkpoint_data.get("branch_results", {})
        if branch_inputs:
            results: list[bool] = []
            for source_id, target_id in branch_inputs:
                key = f"{source_id}:{target_id}"
                if key not in branch_results:
                    WorkflowExecutionService._skip_step(
                        db,
                        execution.owner,
                        execution,
                        row,
                        "branch_not_selected",
                    )
                    return True
                results.append(bool(branch_results[key]))
            if not any(results):
                WorkflowExecutionService._skip_step(
                    db,
                    execution.owner,
                    execution,
                    row,
                    "branch_not_selected",
                )
                return True
        for dependency in definition.dependencies:
            dependency_status = step_rows[dependency].status
            if dependency_status != "completed":
                WorkflowExecutionService._skip_step(
                    db,
                    execution.owner,
                    execution,
                    row,
                    "dependency_not_completed",
                )
                return True
        return False

    @staticmethod
    def _skip_step(
        db: Session,
        user: User,
        execution: WorkflowExecution,
        step: WorkflowStep,
        reason: str,
    ) -> None:
        step.status = "skipped"
        step.output_data = {"skip_reason": reason}
        step.completed_at = _utcnow()
        _step_audit(db, user, execution, step, "workflow.step_skipped")
        _persist_progress(db, execution)

    def _pause(self, db: Session, user: User, execution: WorkflowExecution) -> None:
        now = _utcnow()
        if execution.run_started_at is not None:
            execution.active_duration_seconds += max(
                0.0,
                (now - _aware(execution.run_started_at)).total_seconds(),
            )
        execution.run_started_at = None
        execution.heartbeat_at = now
        _audit(db, user, execution, "workflow.execution_paused")
        db.commit()

    def _cancel_remaining(
        self,
        db: Session,
        user: User,
        execution: WorkflowExecution,
        step_rows: dict[str, WorkflowStep],
    ) -> None:
        for step in step_rows.values():
            if step.status in {"pending", "ready", "waiting_approval"}:
                step.status = "cancelled"
                step.completed_at = _utcnow()
                _step_audit(db, user, execution, step, "workflow.step_cancelled")
        db.commit()

    def _fail_execution(
        self,
        db: Session,
        user: User,
        execution: WorkflowExecution,
        step_rows: dict[str, WorkflowStep],
        code: str,
        message: str,
    ) -> None:
        for step in step_rows.values():
            if step.status in {"pending", "ready", "waiting_approval"}:
                step.status = "skipped"
                step.output_data = {"skip_reason": "workflow_failed"}
                step.completed_at = _utcnow()
                _step_audit(db, user, execution, step, "workflow.step_skipped")
        _finish(
            db,
            user,
            execution,
            "failed",
            error_code=code,
            error_message=message,
        )

    def _complete_execution(
        self,
        db: Session,
        user: User,
        execution: WorkflowExecution,
        step_rows: dict[str, WorkflowStep],
    ) -> None:
        if execution.status == "cancelled":
            self._cancel_remaining(db, user, execution, step_rows)
            return
        _finish(db, user, execution, "completed")

    async def approve(
        self,
        db: Session,
        user: User,
        workflow_id: UUID,
        execution_id: UUID,
        approved: bool,
        reason: str | None,
    ) -> WorkflowExecutionRead:
        execution = _owned_execution(
            db,
            user,
            workflow_id,
            execution_id,
            for_update=True,
        )
        if execution.status != "paused":
            raise APIError(409, "execution_not_paused", "The execution is not waiting for approval.")
        step_id = execution.checkpoint_data.get("waiting_approval_step")
        if not isinstance(step_id, str):
            raise APIError(409, "approval_checkpoint_missing", "No approval checkpoint is pending.")
        step = db.scalar(
            select(WorkflowStep).where(
                WorkflowStep.execution_id == execution.id,
                WorkflowStep.step_key == step_id,
                WorkflowStep.status == "waiting_approval",
            )
        )
        if step is None:
            raise APIError(409, "approval_checkpoint_missing", "No approval checkpoint is pending.")
        if approved:
            step.status = "completed"
            step.output_data = {
                "approved": True,
                "approved_by": str(user.id),
                "reason": reason,
            }
            step.completed_at = _utcnow()
            execution.status = "queued"
            checkpoint = dict(execution.checkpoint_data)
            checkpoint.pop("waiting_approval_step", None)
            execution.checkpoint_data = checkpoint
            _step_audit(db, user, execution, step, "workflow.step_approved")
            _audit(db, user, execution, "workflow.execution_approval_granted")
            db.commit()
            version = db.get(WorkflowVersion, execution.workflow_version_id)
            if version is None:
                raise APIError(409, "workflow_version_missing", "The executed workflow version was not found.")
            definition = WorkflowDefinition.model_validate(version.definition)
            order = _topological_order(definition)
            await self._run(db, user, execution, definition, order)
        else:
            step.status = "failed"
            step.error_code = "approval_rejected"
            step.error_message = reason or "The approval request was rejected."
            step.output_data = {"approved": False, "reason": reason}
            step.completed_at = _utcnow()
            _step_audit(db, user, execution, step, "workflow.step_approval_rejected")
            self._fail_execution(
                db,
                user,
                execution,
                {
                    item.step_key: item
                    for item in db.scalars(
                        select(WorkflowStep).where(
                            WorkflowStep.execution_id == execution.id
                        )
                    ).all()
                },
                "approval_rejected",
                reason or "The approval request was rejected.",
            )
        return _execution_read(db, execution)

    def cancel(
        self,
        db: Session,
        user: User,
        workflow_id: UUID,
        execution_id: UUID,
    ) -> WorkflowExecutionRead:
        execution = _owned_execution(
            db,
            user,
            workflow_id,
            execution_id,
            for_update=True,
        )
        if execution.status in {"completed", "failed"}:
            raise APIError(409, "execution_terminal", "A completed or failed execution cannot be cancelled.")
        if execution.status != "cancelled":
            execution.status = "cancelled"
            execution.error_code = "execution_cancelled"
            execution.error_message = "The execution was cancelled by its owner."
            execution.completed_at = _utcnow()
            execution.heartbeat_at = _utcnow()
            if execution.run_started_at is not None:
                execution.active_duration_seconds += max(
                    0.0,
                    (_utcnow() - _aware(execution.run_started_at)).total_seconds(),
                )
            execution.run_started_at = None
            steps = db.scalars(
                select(WorkflowStep).where(
                    WorkflowStep.execution_id == execution.id,
                    WorkflowStep.status.in_(["pending", "ready", "waiting_approval"]),
                )
            ).all()
            for step in steps:
                step.status = "cancelled"
                step.completed_at = _utcnow()
                _step_audit(db, user, execution, step, "workflow.step_cancelled")
            _audit(db, user, execution, "workflow.execution_cancelled")
            db.commit()
        return _execution_read(db, execution)


def get_execution(
    db: Session,
    user: User,
    workflow_id: UUID,
    execution_id: UUID,
) -> WorkflowExecutionRead:
    return _execution_read(
        db,
        _owned_execution(db, user, workflow_id, execution_id),
    )


def list_executions(
    db: Session,
    user: User,
    workflow_id: UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[WorkflowExecutionRead], int]:
    if db.scalar(
        select(Workflow.id).where(
            Workflow.id == workflow_id,
            Workflow.owner_id == user.id,
        )
    ) is None:
        raise APIError(404, "workflow_not_found", "Workflow not found.")
    total = db.scalar(
        select(func.count()).select_from(WorkflowExecution).where(
            WorkflowExecution.owner_id == user.id,
            WorkflowExecution.workflow_id == workflow_id,
        )
    ) or 0
    executions = db.scalars(
        select(WorkflowExecution)
        .where(
            WorkflowExecution.owner_id == user.id,
            WorkflowExecution.workflow_id == workflow_id,
        )
        .order_by(WorkflowExecution.created_at.desc(), WorkflowExecution.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [_execution_read(db, execution) for execution in executions], total


def recover_interrupted_executions(
    db: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = RECOVERY_STALE_SECONDS,
) -> int:
    recovery_time = now or _utcnow()
    cutoff = recovery_time - timedelta(seconds=stale_after_seconds)
    cancelled_with_running_step = and_(
        WorkflowExecution.status == "cancelled",
        exists(
            select(WorkflowStep.id).where(
                WorkflowStep.execution_id == WorkflowExecution.id,
                WorkflowStep.status == "running",
            )
        ),
    )
    stale = db.scalars(
        select(WorkflowExecution)
        .where(
            or_(
                WorkflowExecution.status.in_(["queued", "running"]),
                cancelled_with_running_step,
            ),
            func.coalesce(
                WorkflowExecution.heartbeat_at,
                WorkflowExecution.created_at,
            )
            < cutoff,
        )
        .with_for_update(skip_locked=True)
    ).all()
    recovered = 0
    for execution in stale:
        was_cancelled = execution.status == "cancelled"
        user = db.get(User, execution.owner_id)
        if user is None:
            continue
        steps = db.scalars(
            select(WorkflowStep).where(
                WorkflowStep.execution_id == execution.id
            )
        ).all()
        has_uncertain_step = False
        for step in steps:
            if step.status == "running":
                step.status = "cancelled" if was_cancelled else "failed"
                step.error_code = (
                    "execution_cancelled_outcome_uncertain"
                    if was_cancelled
                    else "execution_interrupted"
                )
                step.error_message = (
                    "Cancellation interrupted this step; its outcome is uncertain and it was not replayed."
                    if was_cancelled
                    else "The process stopped during this step; it was not replayed."
                )
                step.output_data = {"error_code": step.error_code}
                step.completed_at = recovery_time
                _step_audit(
                    db,
                    user,
                    execution,
                    step,
                    (
                        "workflow.step_cancelled_outcome_uncertain"
                        if was_cancelled
                        else "workflow.step_interrupted"
                    ),
                )
                has_uncertain_step = True
            elif step.status in {"pending", "ready", "waiting_approval"}:
                step.status = "cancelled" if was_cancelled else "skipped"
                step.completed_at = recovery_time
                _step_audit(
                    db,
                    user,
                    execution,
                    step,
                    (
                        "workflow.step_cancelled"
                        if was_cancelled
                        else "workflow.step_skipped"
                    ),
                )
        if execution.run_started_at is not None:
            last_active_at = (
                _aware(execution.heartbeat_at)
                if execution.heartbeat_at is not None
                else recovery_time
            )
            execution.active_duration_seconds += max(
                0.0,
                (
                    last_active_at - _aware(execution.run_started_at)
                ).total_seconds(),
            )
        execution.run_started_at = None
        execution.heartbeat_at = recovery_time
        if not was_cancelled:
            execution.status = "failed"
            execution.error_code = "execution_interrupted"
            execution.error_message = (
                "The execution was interrupted before completion and was not replayed."
            )
        elif execution.error_code is None:
            execution.error_code = "execution_cancelled"
            execution.error_message = "The cancelled execution stopped before its active step settled."
        execution.completed_at = recovery_time
        checkpoint = dict(execution.checkpoint_data)
        checkpoint["recovery"] = {
            "recovered_at": recovery_time.isoformat(),
            "disposition": (
                "cancelled_without_replay"
                if was_cancelled
                else "failed_without_replay"
            ),
            "in_flight_step_outcome_uncertain": has_uncertain_step,
        }
        execution.checkpoint_data = checkpoint
        _audit(db, user, execution, "workflow.execution_recovered")
        recovered += 1
    if recovered:
        db.commit()
    return recovered
