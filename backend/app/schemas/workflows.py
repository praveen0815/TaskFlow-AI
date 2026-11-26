from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

StepKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    ),
]
JsonObject = dict[str, JsonValue]


class TriggerDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["manual", "schedule", "webhook", "event"]
    config: JsonObject = Field(default_factory=dict, max_length=100)


class ConditionalBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(min_length=1, max_length=1000)
    target_step_id: StepKey

    @field_validator("condition", mode="before")
    @classmethod
    def trim_condition(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RetryConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: int = Field(default=0, ge=0, le=300)


class ApprovalRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = False
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class WorkflowStepDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StepKey
    name: str = Field(min_length=1, max_length=120)
    type: Literal["action", "condition", "approval", "delay", "notification"]
    tool: str | None = Field(default=None, min_length=1, max_length=120)
    inputs: JsonObject = Field(default_factory=dict, max_length=100)
    dependencies: list[StepKey] = Field(default_factory=list, max_length=100)
    branches: list[ConditionalBranch] = Field(default_factory=list, max_length=20)
    on_failure_step_id: StepKey | None = None
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    retry: RetryConfiguration = Field(default_factory=RetryConfiguration)
    approval: ApprovalRequirement = Field(default_factory=ApprovalRequirement)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_branches(self) -> "WorkflowStepDefinition":
        if self.type == "condition" and not self.branches:
            raise ValueError("Condition steps must define at least one branch.")
        if self.type != "condition" and self.branches:
            raise ValueError("Only condition steps may define branches.")
        if self.tool is not None and self.type != "action":
            raise ValueError("Only action steps may select a tool.")
        if self.approval.required and self.type != "approval":
            raise ValueError("Approval requirements can only be set on approval steps.")
        return self


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    trigger: TriggerDefinition
    steps: list[WorkflowStepDefinition] = Field(min_length=1, max_length=100)
    metadata: JsonObject = Field(default_factory=dict, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_step_graph(self) -> "WorkflowDefinition":
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Step IDs must be unique.")

        known_ids = set(step_ids)
        graph: dict[str, set[str]] = {step_id: set() for step_id in step_ids}
        for step in self.steps:
            if len(step.dependencies) != len(set(step.dependencies)):
                raise ValueError(f"Step '{step.id}' contains duplicate dependencies.")
            for dependency in step.dependencies:
                if dependency not in known_ids:
                    raise ValueError(
                        f"Step '{step.id}' depends on unknown step '{dependency}'."
                    )
                if dependency == step.id:
                    raise ValueError(f"Step '{step.id}' cannot depend on itself.")
                graph[dependency].add(step.id)

            branch_targets = [branch.target_step_id for branch in step.branches]
            if len(branch_targets) != len(set(branch_targets)):
                raise ValueError(f"Step '{step.id}' contains duplicate branch targets.")
            for target in branch_targets:
                if target not in known_ids:
                    raise ValueError(
                        f"Step '{step.id}' branches to unknown step '{target}'."
                    )
                graph[step.id].add(target)
            if step.on_failure_step_id is not None:
                if step.type != "action":
                    raise ValueError("Only action steps may define a failure branch.")
                if step.on_failure_step_id not in known_ids:
                    raise ValueError(
                        f"Step '{step.id}' has an unknown failure branch "
                        f"'{step.on_failure_step_id}'."
                    )
                graph[step.id].add(step.on_failure_step_id)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("Workflow step dependencies and branches must not contain cycles.")
            if step_id in visited:
                return
            visiting.add(step_id)
            for next_step in graph[step_id]:
                visit(next_step)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        return self


class DuplicateWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class WorkflowRead(WorkflowDefinition):
    id: UUID
    current_version: int
    published_version: int | None
    is_published: bool
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkflowVersionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    version: int
    definition: WorkflowDefinition
    source_request: str | None = None
    generation_provider: str | None = None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkflowVersionResponse(BaseModel):
    data: WorkflowVersionRead


class Pagination(BaseModel):
    limit: int
    offset: int
    total: int


class WorkflowResponse(BaseModel):
    data: WorkflowRead


class WorkflowListResponse(BaseModel):
    data: list[WorkflowRead]
    pagination: "Pagination"


class WorkflowVersionListResponse(BaseModel):
    data: list[WorkflowVersionRead]
    pagination: Pagination


class WorkflowActionResponse(BaseModel):
    data: dict[str, str]


class WorkflowExecutionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_data: JsonObject = Field(default_factory=dict, max_length=100)


class WorkflowExecutionStepRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_key: StepKey
    step_order: int
    status: Literal[
        "pending",
        "ready",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "skipped",
        "cancelled",
    ]
    attempt_count: int
    input_data: JsonObject | None
    output_data: JsonValue | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class WorkflowExecutionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workflow_id: UUID
    workflow_version_id: UUID
    workflow_version: int
    status: Literal[
        "queued",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
    ]
    trigger_data: JsonObject
    checkpoint_data: JsonObject
    error_code: str | None
    error_message: str | None
    active_duration_seconds: float
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    steps: list[WorkflowExecutionStepRead]


class WorkflowExecutionResponse(BaseModel):
    data: WorkflowExecutionRead


class WorkflowExecutionListResponse(BaseModel):
    data: list[WorkflowExecutionRead]
    pagination: Pagination


class WorkflowExecutionApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str | None = Field(default=None, max_length=500)
