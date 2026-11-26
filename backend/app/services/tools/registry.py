from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from app.models import User
from app.schemas.planning import ToolCatalogEntry


@dataclass(frozen=True)
class ToolContext:
    db: Session
    user: User


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[Any]]


@dataclass(frozen=True)
class RegisteredTool:
    metadata: ToolCatalogEntry
    handler: ToolHandler | None


class ToolRegistry:
    def __init__(self, tools: Sequence[RegisteredTool]) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            name = tool.metadata.name
            if name in self._tools:
                raise ValueError(f"Duplicate tool name: {name}")
            is_available = tool.metadata.availability_status == "available"
            if is_available != (tool.handler is not None):
                raise ValueError(
                    f"Tool '{name}' availability must match whether it has an executor."
                )
            Draft202012Validator.check_schema(tool.metadata.input_schema)
            Draft202012Validator.check_schema(tool.metadata.output_schema)
            self._tools[name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def all(self) -> tuple[RegisteredTool, ...]:
        return tuple(self._tools.values())

    def available_catalog(self) -> tuple[ToolCatalogEntry, ...]:
        return tuple(
            tool.metadata
            for tool in self._tools.values()
            if tool.metadata.availability_status == "available"
        )

    def catalog(self) -> tuple[ToolCatalogEntry, ...]:
        return tuple(tool.metadata for tool in self._tools.values())
