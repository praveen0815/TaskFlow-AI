from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    event,
    ForeignKey,
    ForeignKeyConstraint,
    inspect,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Workflow(TimestampMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_workflows_id_owner"),
        CheckConstraint("current_version > 0", name="ck_workflows_current_version_positive"),
        CheckConstraint(
            "published_version IS NULL OR "
            "(published_version > 0 AND published_version <= current_version)",
            name="ck_workflows_published_version_range",
        ),
        Index("ix_workflows_owner_updated_at", "owner_id", "updated_at"),
        Index(
            "ix_workflows_owner_archived_updated",
            "owner_id",
            "archived_at",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )
    published_version: Mapped[int | None] = mapped_column(Integer)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped["User"] = relationship(back_populates="workflows")
    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    executions: Mapped[list["WorkflowExecution"]] = relationship(
        back_populates="workflow",
        foreign_keys=lambda: [WorkflowExecution.workflow_id, WorkflowExecution.owner_id],
        viewonly=True,
    )


class WorkflowVersion(TimestampMixin, Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_version"),
        UniqueConstraint("workflow_id", "id", name="uq_workflow_versions_workflow_id"),
        CheckConstraint("version > 0", name="ck_workflow_versions_version_positive"),
        Index("ix_workflow_versions_workflow_id", "workflow_id"),
        Index("ix_workflow_versions_workflow_published", "workflow_id", "published_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workflow_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_request: Mapped[str | None] = mapped_column(Text)
    generation_provider: Mapped[str | None] = mapped_column(String(40))
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        active_history=True,
    )
    workflow: Mapped[Workflow] = relationship(back_populates="versions")
    executions: Mapped[list["WorkflowExecution"]] = relationship(
        back_populates="workflow_version",
        foreign_keys=lambda: [
            WorkflowExecution.workflow_id,
            WorkflowExecution.workflow_version_id,
        ],
        viewonly=True,
    )


@event.listens_for(WorkflowVersion, "before_update")
def prevent_published_version_updates(mapper, connection, version: WorkflowVersion) -> None:
    published_history = inspect(version).attrs.published_at.history
    was_published = bool(
        published_history.deleted and published_history.deleted[0] is not None
    )
    if was_published or (
        version.published_at is not None and not published_history.has_changes()
    ):
        raise ValueError("Published workflow versions are immutable.")


@event.listens_for(WorkflowVersion, "before_delete")
def prevent_published_version_deletion(mapper, connection, version: WorkflowVersion) -> None:
    if version.published_at is not None:
        raise ValueError("Published workflow versions cannot be deleted.")


class WorkflowExecution(TimestampMixin, Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_workflow_executions_status",
        ),
        UniqueConstraint(
            "owner_id",
            "idempotency_key",
            name="uq_workflow_executions_owner_idempotency",
        ),
        ForeignKeyConstraint(
            ["workflow_id", "owner_id"],
            ["workflows.id", "workflows.owner_id"],
            ondelete="CASCADE",
            name="fk_workflow_executions_workflow_owner",
        ),
        ForeignKeyConstraint(
            ["workflow_id", "workflow_version_id"],
            ["workflow_versions.workflow_id", "workflow_versions.id"],
            ondelete="RESTRICT",
            name="fk_workflow_executions_workflow_version",
        ),
        Index("ix_workflow_executions_owner_created_at", "owner_id", "created_at"),
        Index("ix_workflow_executions_workflow_id", "workflow_id"),
        Index(
            "ix_workflow_executions_status_heartbeat",
            "status",
            "heartbeat_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    workflow_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="queued",
        server_default="queued",
        nullable=False,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    trigger_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
        nullable=False,
    )
    checkpoint_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    active_duration_seconds: Mapped[float] = mapped_column(
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped["User"] = relationship(back_populates="executions")
    workflow: Mapped[Workflow] = relationship(
        back_populates="executions",
        foreign_keys=[workflow_id, owner_id],
        viewonly=True,
    )
    workflow_version: Mapped[WorkflowVersion] = relationship(
        back_populates="executions",
        foreign_keys=[workflow_id, workflow_version_id],
        viewonly=True,
    )
    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WorkflowStep.step_order",
    )


class WorkflowStep(TimestampMixin, Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("execution_id", "step_key", name="uq_workflow_steps_execution_key"),
        UniqueConstraint("execution_id", "step_order", name="uq_workflow_steps_execution_order"),
        CheckConstraint("step_order >= 0", name="ck_workflow_steps_order_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="ck_workflow_steps_attempt_count"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'waiting_approval', "
            "'completed', 'failed', 'skipped', 'cancelled')",
            name="ck_workflow_steps_status",
        ),
        Index("ix_workflow_steps_execution_id", "execution_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_key: Mapped[str] = mapped_column(String(120), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        server_default="pending",
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    execution: Mapped[WorkflowExecution] = relationship(back_populates="steps")
