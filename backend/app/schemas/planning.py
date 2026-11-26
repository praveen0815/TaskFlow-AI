from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from app.schemas.workflows import WorkflowDefinition, WorkflowRead

ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]


class ToolCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ToolName
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    version: str = Field(min_length=1, max_length=40)
    permissions: tuple[str, ...]
    risk_classification: Literal["low", "medium", "high"]
    timeout_seconds: int = Field(ge=1, le=60)
    availability_status: Literal["available", "unavailable"]


class IntentInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    workflow_name: str = Field(min_length=1, max_length=120)
    trigger_type: Literal["manual", "schedule", "webhook", "event"]
    goals: list[str] = Field(min_length=1, max_length=12)
    approval_required: bool = False


class StructuredWorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: WorkflowDefinition
    rationale: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)


class PlanningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=10, max_length=4000)


class PlanningResult(BaseModel):
    workflow: WorkflowRead
    intent_summary: str
    rationale: str
    assumptions: list[str]
    dependency_order: list[str]
    provider: str


class PlanningResponse(BaseModel):
    data: PlanningResult


class PlanningFailure(BaseModel):
    status_code: int
    code: str
    message: str
