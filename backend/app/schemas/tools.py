from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.schemas.planning import ToolCatalogEntry


class ToolRegistryResponse(BaseModel):
    data: list[ToolCatalogEntry]


class ToolExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, JsonValue] = Field(max_length=100)


class ToolExecutionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    tool_name: str
    tool_version: str
    status: Literal["running", "succeeded", "failed", "timed_out", "denied", "rejected"]
    input_sha256: str
    output_sha256: str | None
    error_code: str | None
    duration_ms: int | None
    created_at: datetime
    completed_at: datetime | None


class ToolExecuteResult(BaseModel):
    execution: ToolExecutionRead
    result: JsonValue


class ToolExecutionResponse(BaseModel):
    data: ToolExecuteResult


class ToolExecutionHistoryResponse(BaseModel):
    data: list[ToolExecutionRead]
    next_offset: int | None


class NotificationDraftRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    title: str
    body: str
    status: Literal["draft"]
    created_at: datetime


class NotificationDraftResponse(BaseModel):
    data: NotificationDraftRead
