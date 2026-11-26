from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    User,
    Workflow,
    WorkflowExecution,
    WorkflowVersion,
)
from app.schemas.workflows import WorkflowDefinition, WorkflowRead


def _record_workflow_event(
    db: Session,
    user: User,
    workflow: Workflow,
    action: str,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="workflow",
            resource_id=str(workflow.id),
            details={"version": workflow.current_version},
        )
    )


def _get_owned_workflow(
    db: Session,
    user: User,
    workflow_id: UUID,
    *,
    for_update: bool = False,
) -> Workflow:
    statement = select(Workflow).where(
        Workflow.id == workflow_id,
        Workflow.owner_id == user.id,
    )
    if for_update:
        statement = statement.with_for_update()
    workflow = db.scalar(statement)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return workflow


def _get_version(
    db: Session,
    workflow_id: UUID,
    version_number: int,
) -> WorkflowVersion:
    version = db.scalar(
        select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.version == version_number,
        )
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Workflow version not found.")
    return version


def _latest_version(db: Session, workflow: Workflow) -> WorkflowVersion:
    return _get_version(db, workflow.id, workflow.current_version)


def _workflow_read(workflow: Workflow, version: WorkflowVersion) -> WorkflowRead:
    definition = WorkflowDefinition.model_validate(version.definition)
    return WorkflowRead.model_validate(
        {
            **definition.model_dump(),
            "id": workflow.id,
            "current_version": workflow.current_version,
            "published_version": workflow.published_version,
            "is_published": workflow.published_version == workflow.current_version,
            "archived_at": workflow.archived_at,
            "created_at": workflow.created_at,
            "updated_at": workflow.updated_at,
        }
    )


def _version_read(version: WorkflowVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "version": version.version,
        "definition": WorkflowDefinition.model_validate(version.definition),
        "source_request": version.source_request,
        "generation_provider": version.generation_provider,
        "published_at": version.published_at,
        "created_at": version.created_at,
        "updated_at": version.updated_at,
    }


def _assert_active(workflow: Workflow) -> None:
    if workflow.archived_at is not None:
        raise HTTPException(
            status_code=409,
            detail="Archived workflows cannot be changed or published.",
        )


def create_workflow(
    db: Session,
    user: User,
    definition: WorkflowDefinition,
    *,
    source_request: str | None = None,
    generation_provider: str | None = None,
) -> WorkflowRead:
    workflow = Workflow(
        owner_id=user.id,
        name=definition.name,
        description=definition.description,
        current_version=1,
    )
    db.add(workflow)
    db.flush()
    version = WorkflowVersion(
        workflow_id=workflow.id,
        version=1,
        definition=definition.model_dump(mode="json"),
        source_request=source_request,
        generation_provider=generation_provider,
    )
    db.add(version)
    _record_workflow_event(db, user, workflow, "workflow.created")
    db.flush()
    result = _workflow_read(workflow, version)
    db.commit()
    return result


def get_workflow(db: Session, user: User, workflow_id: UUID) -> WorkflowRead:
    workflow = _get_owned_workflow(db, user, workflow_id)
    return _workflow_read(workflow, _latest_version(db, workflow))


def update_workflow(
    db: Session,
    user: User,
    workflow_id: UUID,
    definition: WorkflowDefinition,
) -> WorkflowRead:
    workflow = _get_owned_workflow(db, user, workflow_id, for_update=True)
    _assert_active(workflow)
    next_version_number = workflow.current_version + 1
    version = WorkflowVersion(
        workflow_id=workflow.id,
        version=next_version_number,
        definition=definition.model_dump(mode="json"),
    )
    workflow.name = definition.name
    workflow.description = definition.description
    workflow.current_version = next_version_number
    db.add(version)
    _record_workflow_event(db, user, workflow, "workflow.updated")
    db.flush()
    result = _workflow_read(workflow, version)
    db.commit()
    return result


def list_workflows(
    db: Session,
    user: User,
    *,
    limit: int,
    offset: int,
    archived: bool,
    search: str | None,
    published: bool | None,
    sort_by: str,
    sort_order: str,
) -> tuple[list[WorkflowRead], int]:
    conditions = [Workflow.owner_id == user.id]
    conditions.append(
        Workflow.archived_at.is_not(None) if archived else Workflow.archived_at.is_(None)
    )
    if search:
        pattern = f"%{search}%"
        conditions.append(or_(Workflow.name.ilike(pattern), Workflow.description.ilike(pattern)))
    if published is True:
        conditions.append(Workflow.published_version == Workflow.current_version)
    elif published is False:
        conditions.append(
            or_(
                Workflow.published_version.is_(None),
                Workflow.published_version != Workflow.current_version,
            )
        )

    total = db.scalar(
        select(func.count()).select_from(Workflow).where(*conditions)
    ) or 0
    sort_column = {
        "name": Workflow.name,
        "created_at": Workflow.created_at,
        "updated_at": Workflow.updated_at,
    }[sort_by]
    ordering = sort_column.asc() if sort_order == "asc" else sort_column.desc()
    workflows = list(
        db.scalars(
            select(Workflow)
            .where(*conditions)
            .order_by(ordering, Workflow.id)
            .limit(limit)
            .offset(offset)
        ).all()
    )
    if not workflows:
        return [], total

    latest_versions = db.scalars(
        select(WorkflowVersion)
        .join(Workflow, WorkflowVersion.workflow_id == Workflow.id)
        .where(
            Workflow.id.in_([workflow.id for workflow in workflows]),
            WorkflowVersion.version == Workflow.current_version,
        )
    ).all()
    versions_by_workflow = {version.workflow_id: version for version in latest_versions}
    return [
        _workflow_read(workflow, versions_by_workflow[workflow.id])
        for workflow in workflows
    ], total


def archive_workflow(db: Session, user: User, workflow_id: UUID) -> WorkflowRead:
    workflow = _get_owned_workflow(db, user, workflow_id, for_update=True)
    if workflow.archived_at is None:
        workflow.archived_at = datetime.now(timezone.utc)
        _record_workflow_event(db, user, workflow, "workflow.archived")
        db.flush()
    result = _workflow_read(workflow, _latest_version(db, workflow))
    db.commit()
    return result


def delete_workflow(db: Session, user: User, workflow_id: UUID) -> None:
    workflow = _get_owned_workflow(db, user, workflow_id, for_update=True)
    has_executions = db.scalar(
        select(WorkflowExecution.id)
        .where(WorkflowExecution.workflow_id == workflow.id)
        .limit(1)
    )
    if has_executions is not None or workflow.published_version is not None:
        raise HTTPException(
            status_code=409,
            detail="Workflows with execution history or published versions cannot be deleted; archive instead.",
        )
    _record_workflow_event(db, user, workflow, "workflow.deleted")
    db.delete(workflow)
    db.commit()


def duplicate_workflow(
    db: Session,
    user: User,
    workflow_id: UUID,
    name: str | None,
) -> WorkflowRead:
    source = _get_owned_workflow(db, user, workflow_id, for_update=True)
    source_version = _latest_version(db, source)
    source_definition = WorkflowDefinition.model_validate(source_version.definition)
    duplicate_name = name or f"{source_definition.name[:113]} (copy)"
    duplicate_definition = source_definition.model_copy(update={"name": duplicate_name})
    return create_workflow(db, user, duplicate_definition)


def publish_workflow(db: Session, user: User, workflow_id: UUID) -> WorkflowRead:
    workflow = _get_owned_workflow(db, user, workflow_id, for_update=True)
    _assert_active(workflow)
    version = _latest_version(db, workflow)
    if version.published_at is None:
        version.published_at = datetime.now(timezone.utc)
        workflow.published_version = workflow.current_version
        _record_workflow_event(db, user, workflow, "workflow.published")
        db.flush()
    result = _workflow_read(workflow, version)
    db.commit()
    return result


def list_workflow_versions(
    db: Session,
    user: User,
    workflow_id: UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    _get_owned_workflow(db, user, workflow_id)
    total = db.scalar(
        select(func.count()).select_from(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id
        )
    ) or 0
    versions = db.scalars(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [_version_read(version) for version in versions], total


def get_workflow_version(
    db: Session,
    user: User,
    workflow_id: UUID,
    version_number: int,
) -> dict[str, Any]:
    _get_owned_workflow(db, user, workflow_id)
    return _version_read(_get_version(db, workflow_id, version_number))
