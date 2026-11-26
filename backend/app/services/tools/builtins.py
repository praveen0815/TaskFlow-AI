import ast
import base64
import binascii
import csv
import io
import json
import math
import operator
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.models import NotificationDraft, Workflow, WorkflowVersion
from app.schemas.planning import ToolCatalogEntry
from app.services.tools.errors import ToolInputError
from app.services.tools.registry import (
    RegisteredTool,
    ToolContext,
    ToolRegistry,
)

def _tool(
    name: str,
    description: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    *,
    risk: str = "low",
    timeout: int = 5,
) -> ToolCatalogEntry:
    return ToolCatalogEntry(
        name=name,
        description=description,
        input_schema=inputs,
        output_schema=outputs,
        version="1.0.0",
        permissions=(f"tool:{name}",),
        risk_classification=risk,
        timeout_seconds=timeout,
        availability_status="available",
    )


async def _calculator(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    expression = arguments["expression"]
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolInputError("The expression is not valid arithmetic.") from exc

    operations: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if isinstance(node.value, bool):
                raise ToolInputError("Boolean values are not supported.")
            value: int | float = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = evaluate(node.operand)
            value = operand if isinstance(node.op, ast.UAdd) else -operand
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and (
                not isinstance(right, int) or abs(right) > 100
            ):
                raise ToolInputError("Exponent values must be integers between -100 and 100.")
            value = operations[type(node.op)](left, right)
        else:
            raise ToolInputError("Only basic arithmetic operators and numeric literals are allowed.")

        if isinstance(value, float) and not math.isfinite(value):
            raise ToolInputError("The arithmetic result must be finite.")
        if isinstance(value, int) and value.bit_length() > 333:
            raise ToolInputError("The arithmetic result is too large.")
        if isinstance(value, float) and abs(value) > 1e100:
            raise ToolInputError("The arithmetic result is too large.")
        return value

    try:
        value = evaluate(tree.body)
    except (ArithmeticError, OverflowError) as exc:
        raise ToolInputError("The arithmetic expression could not be evaluated.") from exc
    return {"expression": expression, "value": value}


async def _date_time(arguments: dict[str, Any], _context: ToolContext) -> dict[str, str]:
    timezone_name = arguments.get("timezone", "UTC")
    try:
        current = datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolInputError("The requested timezone is not available.") from exc
    return {
        "timezone": timezone_name,
        "iso_datetime": current.isoformat(),
        "date": current.date().isoformat(),
        "time": current.timetz().isoformat(),
    }


async def _text_transform(
    arguments: dict[str, Any],
    _context: ToolContext,
) -> dict[str, str]:
    text = arguments["text"]
    operation_name = arguments["operation"]
    operations = {
        "uppercase": str.upper,
        "lowercase": str.lower,
        "trim": str.strip,
        "reverse": lambda value: value[::-1],
    }
    return {"operation": operation_name, "text": operations[operation_name](text)}


async def _json_process(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    value = arguments["value"]
    operation_name = arguments["operation"]
    if operation_name in {"compact", "pretty"}:
        return {
            "result": json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":") if operation_name == "compact" else None,
                indent=2 if operation_name == "pretty" else None,
                allow_nan=False,
            )
        }
    if operation_name == "keys":
        if not isinstance(value, dict):
            raise ToolInputError("The keys operation requires a JSON object.")
        return {"result": sorted(value.keys())}

    current: Any = value
    path = arguments["path"].split(".") if arguments.get("path") else []
    for segment in path:
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdecimal():
            index = int(segment)
            if index >= len(current):
                raise ToolInputError("The requested JSON path does not exist.")
            current = current[index]
        else:
            raise ToolInputError("The requested JSON path does not exist.")
    return {"result": current}


async def _document_extract(
    arguments: dict[str, Any],
    _context: ToolContext,
) -> dict[str, Any]:
    filename = arguments["filename"]
    media_type = arguments["media_type"].casefold()
    supported = {
        ".txt": {"text/plain"},
        ".md": {"text/markdown", "text/plain"},
        ".csv": {"text/csv", "text/plain"},
        ".json": {"application/json", "text/json", "text/plain"},
    }
    suffix = "." + filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if suffix not in supported or media_type not in supported[suffix]:
        raise ToolInputError("Only user-provided TXT, Markdown, CSV, and JSON files are supported.")
    try:
        content = base64.b64decode(arguments["content_base64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolInputError("The file content must be valid base64.") from exc
    if len(content) > 5 * 1024 * 1024:
        raise ToolInputError("The decoded file must not exceed 5 MiB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolInputError("The file must contain valid UTF-8 text.") from exc

    if suffix == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolInputError("The JSON document is malformed.") from exc
        text = json.dumps(parsed, ensure_ascii=False, indent=2, allow_nan=False)
    elif suffix == ".csv":
        try:
            text = "\n".join(
                "\t".join(row)
                for row in csv.reader(io.StringIO(text), strict=True)
            )
        except csv.Error as exc:
            raise ToolInputError("The CSV document is malformed.") from exc
    return {
        "filename": filename,
        "media_type": media_type,
        "byte_size": len(content),
        "text": text,
    }


async def _workflow_lookup(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    query = select(Workflow).where(Workflow.owner_id == context.user.id)
    workflow_id = arguments.get("workflow_id")
    if workflow_id is not None:
        try:
            parsed_id = UUID(workflow_id)
        except ValueError as exc:
            raise ToolInputError("workflow_id must be a valid UUID.") from exc
        query = query.where(Workflow.id == parsed_id)
    else:
        query = query.where(Workflow.name.ilike(f"%{arguments['name']}%"))
    workflows = context.db.scalars(query.order_by(Workflow.updated_at.desc()).limit(10)).all()
    results = []
    for workflow in workflows:
        version = context.db.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.version == workflow.current_version,
            )
        )
        if version is not None:
            results.append(
                {
                    "id": str(workflow.id),
                    "name": workflow.name,
                    "description": workflow.description,
                    "current_version": workflow.current_version,
                    "published_version": workflow.published_version,
                    "definition": version.definition,
                }
            )
    return {"workflows": results}


async def _notification_draft(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, str]:
    draft = NotificationDraft(
        owner_id=context.user.id,
        title=arguments["title"],
        body=arguments["body"],
    )
    context.db.add(draft)
    context.db.flush()
    return {
        "id": str(draft.id),
        "title": draft.title,
        "status": draft.status,
        "created_at": draft.created_at.isoformat(),
    }


def _unavailable_tool(
    name: str,
    description: str,
    permissions: tuple[str, ...],
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> RegisteredTool:
    return RegisteredTool(
        metadata=ToolCatalogEntry(
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            version="1.0.0",
            permissions=permissions,
            risk_classification="high",
            timeout_seconds=10,
            availability_status="unavailable",
        ),
        handler=None,
    )


def create_default_tool_registry() -> ToolRegistry:
    definitions: list[tuple[ToolCatalogEntry, Callable[..., Awaitable[Any]]]] = [
        (
            _tool(
                "calculator.evaluate",
                "Evaluate a bounded arithmetic expression without executing code.",
                {
                    "type": "object",
                    "properties": {"expression": {"type": "string", "maxLength": 256}},
                    "required": ["expression"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string"},
                        "value": {"type": "number"},
                    },
                    "required": ["expression", "value"],
                    "additionalProperties": False,
                },
            ),
            _calculator,
        ),
        (
            _tool(
                "datetime.now",
                "Return the current date and time in an IANA timezone.",
                {
                    "type": "object",
                    "properties": {"timezone": {"type": "string", "maxLength": 100}},
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "timezone": {"type": "string"},
                        "iso_datetime": {"type": "string"},
                        "date": {"type": "string"},
                        "time": {"type": "string"},
                    },
                    "required": ["timezone", "iso_datetime", "date", "time"],
                    "additionalProperties": False,
                },
            ),
            _date_time,
        ),
        (
            _tool(
                "text.transform",
                "Apply a fixed, non-executable transformation to text.",
                {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "maxLength": 20000},
                        "operation": {
                            "type": "string",
                            "enum": ["uppercase", "lowercase", "trim", "reverse"],
                        },
                    },
                    "required": ["text", "operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["uppercase", "lowercase", "trim", "reverse"],
                        },
                        "text": {"type": "string"},
                    },
                    "required": ["operation", "text"],
                    "additionalProperties": False,
                },
            ),
            _text_transform,
        ),
        (
            _tool(
                "json.process",
                "Format JSON, list object keys, or read a value by a fixed dotted path.",
                {
                    "type": "object",
                    "properties": {
                        "value": {},
                        "operation": {
                            "type": "string",
                            "enum": ["compact", "pretty", "keys", "get"],
                        },
                        "path": {"type": "string", "maxLength": 500},
                    },
                    "allOf": [
                        {
                            "if": {
                                "properties": {"operation": {"const": "get"}},
                                "required": ["operation"],
                            },
                            "then": {"required": ["path"]},
                        }
                    ],
                    "required": ["value", "operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"result": {}},
                    "required": ["result"],
                    "additionalProperties": False,
                },
            ),
            _json_process,
        ),
        (
            _tool(
                "document.extract",
                "Extract UTF-8 text from user-provided TXT, Markdown, CSV, or JSON file bytes.",
                {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "maxLength": 255},
                        "media_type": {"type": "string", "maxLength": 100},
                        "content_base64": {"type": "string", "maxLength": 7000000},
                    },
                    "required": ["filename", "media_type", "content_base64"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "media_type": {"type": "string"},
                        "byte_size": {"type": "integer", "minimum": 0},
                        "text": {"type": "string"},
                    },
                    "required": ["filename", "media_type", "byte_size", "text"],
                    "additionalProperties": False,
                },
                timeout=10,
            ),
            _document_extract,
        ),
        (
            _tool(
                "workflow.lookup",
                "Find workflows owned by the current user by ID or name.",
                {
                    "type": "object",
                    "properties": {
                        "workflow_id": {"type": "string"},
                        "name": {"type": "string", "maxLength": 120},
                    },
                    "oneOf": [
                        {"required": ["workflow_id"], "not": {"required": ["name"]}},
                        {"required": ["name"], "not": {"required": ["workflow_id"]}},
                    ],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "workflows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "name": {"type": "string"},
                                    "description": {"type": ["string", "null"]},
                                    "current_version": {"type": "integer"},
                                    "published_version": {
                                        "type": ["integer", "null"]
                                    },
                                    "definition": {"type": "object"},
                                },
                                "required": [
                                    "id",
                                    "name",
                                    "description",
                                    "current_version",
                                    "published_version",
                                    "definition",
                                ],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["workflows"],
                    "additionalProperties": False,
                },
                risk="medium",
            ),
            _workflow_lookup,
        ),
        (
            _tool(
                "notification.draft",
                "Create a private notification draft; this does not send or deliver a message.",
                {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "body": {"type": "string", "minLength": 1, "maxLength": 20000},
                    },
                    "required": ["title", "body"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"const": "draft"},
                        "created_at": {"type": "string"},
                    },
                    "required": ["id", "title", "status", "created_at"],
                    "additionalProperties": False,
                },
                risk="medium",
                timeout=3,
            ),
            _notification_draft,
        ),
    ]

    registered = [
        RegisteredTool(metadata=metadata, handler=handler)
        for metadata, handler in definitions
    ]
    registered.extend(
        [
            _unavailable_tool(
                "integration.email.send",
                "Future email delivery connector; no provider is configured.",
                ("integration:email.send",),
                {"type": "object"},
                {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
            ),
            _unavailable_tool(
                "integration.messaging.send",
                "Future Slack or equivalent messaging connector; no provider is configured.",
                ("integration:messaging.send",),
                {"type": "object"},
                {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
            ),
            _unavailable_tool(
                "integration.crm.lookup",
                "Future CRM connector; no provider is configured.",
                ("integration:crm.read",),
                {"type": "object"},
                {"type": "object", "properties": {"records": {"type": "array"}}},
            ),
            _unavailable_tool(
                "integration.webhook.call",
                "Future webhook connector; no endpoint allowlist is configured.",
                ("integration:webhook.call",),
                {"type": "object"},
                {"type": "object", "properties": {"status_code": {"type": "integer"}}},
            ),
            _unavailable_tool(
                "integration.external_api.request",
                "Future external API connector; no provider or destination policy is configured.",
                ("integration:external_api.request",),
                {"type": "object"},
                {"type": "object", "properties": {"status_code": {"type": "integer"}}},
            ),
            _unavailable_tool(
                "integration.document_storage.get",
                "Future document storage connector; no storage provider is configured.",
                ("integration:document_storage.read",),
                {"type": "object"},
                {"type": "object", "properties": {"content_base64": {"type": "string"}}},
            ),
        ]
    )
    return ToolRegistry(registered)
