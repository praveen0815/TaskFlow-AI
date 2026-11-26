import re
from collections import defaultdict, deque
from collections.abc import Sequence

from jsonschema import (
    Draft202012Validator,
    SchemaError,
    ValidationError as JsonSchemaValidationError,
)
from pydantic import ValidationError

from app.schemas.planning import (
    IntentInterpretation,
    PlanningFailure,
    StructuredWorkflowPlan,
    ToolCatalogEntry,
)
from app.schemas.workflows import WorkflowDefinition
from app.services.planning.errors import PlanningError
from app.services.planning.providers import PlanningProvider

_EXECUTABLE_TEXT = re.compile(
    r"```|<\s*script\b|^\s*#!|"
    r"\b(?:exec|eval|__import__)\s*\(|"
    r"\b(?:os\.system|subprocess\.(?:run|call|Popen))\s*\(|"
    r"^\s*(?:import\s+[A-Za-z_]|from\s+\S+\s+import\s+|"
    r"python(?:3)?\s+-c\b|(?:bash|sh|powershell|cmd)\s+-c\b|"
    r"sudo\s+|rm\s+-rf\b|curl\s+https?://)",
    re.IGNORECASE | re.MULTILINE,
)


class IntentInterpreter:
    def __init__(self, provider: PlanningProvider) -> None:
        self._provider = provider

    async def interpret(self, request: str) -> IntentInterpretation:
        try:
            output = await self._provider.interpret(request)
            return IntentInterpretation.model_validate(output)
        except ValidationError as exc:
            raise PlanningError(
                502,
                "planning_provider_invalid_output",
                "The workflow planning provider returned an invalid intent.",
            ) from exc


class WorkflowPlanner:
    def __init__(self, provider: PlanningProvider) -> None:
        self._provider = provider

    async def plan(
        self,
        request: str,
        intent: IntentInterpretation,
        tool_catalog: Sequence[ToolCatalogEntry],
    ) -> StructuredWorkflowPlan:
        try:
            output = await self._provider.plan(request, intent, tool_catalog)
            return StructuredWorkflowPlan.model_validate(output)
        except ValidationError as exc:
            raise PlanningError(
                502,
                "planning_provider_invalid_output",
                "The workflow planning provider returned a malformed workflow plan.",
            ) from exc


class PlanValidator:
    def validate(
        self,
        intent: IntentInterpretation,
        plan: StructuredWorkflowPlan,
        tool_catalog: Sequence[ToolCatalogEntry],
    ) -> WorkflowDefinition:
        workflow = WorkflowDefinition.model_validate(
            plan.workflow.model_dump(mode="json")
        )
        if workflow.trigger.type != intent.trigger_type:
            raise PlanningError(
                422,
                "invalid_workflow_plan",
                "The planned workflow trigger does not match the interpreted request.",
            )
        if intent.approval_required and not any(
            step.type == "approval" and step.approval.required
            for step in workflow.steps
        ):
            raise PlanningError(
                422,
                "approval_checkpoint_missing",
                "The plan requires an approval checkpoint but none was generated.",
            )
        if self._contains_executable_content(workflow.model_dump(mode="json")):
            raise PlanningError(
                422,
                "executable_content_not_allowed",
                "Generated plans may contain declarative workflow data only, not executable code.",
            )

        tools_by_name = {tool.name: tool for tool in tool_catalog}
        if len(tools_by_name) != len(tool_catalog):
            raise PlanningError(
                500,
                "invalid_tool_catalog",
                "The configured workflow tool catalog contains duplicate names.",
            )
        for step in workflow.steps:
            if step.type == "action" and step.tool is None:
                raise PlanningError(
                    422,
                    "invalid_workflow_plan",
                    f"Action step '{step.id}' must select a catalog tool.",
                )
            if step.tool is None:
                continue
            tool = tools_by_name.get(step.tool)
            if tool is None:
                raise PlanningError(
                    422,
                    "unavailable_tool",
                    f"Step '{step.id}' selected a tool that is not available.",
                )
            try:
                Draft202012Validator.check_schema(tool.input_schema)
                Draft202012Validator(tool.input_schema).validate(step.inputs)
            except SchemaError as exc:
                raise PlanningError(
                    500,
                    "invalid_tool_catalog",
                    "A configured workflow tool has an invalid input schema.",
                ) from exc
            except JsonSchemaValidationError as exc:
                raise PlanningError(
                    422,
                    "invalid_tool_inputs",
                    f"Inputs for step '{step.id}' do not match its catalog tool schema.",
                ) from exc
        return workflow

    @classmethod
    def _contains_executable_content(cls, value: object) -> bool:
        if isinstance(value, str):
            return _EXECUTABLE_TEXT.search(value) is not None
        if isinstance(value, dict):
            return any(cls._contains_executable_content(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_executable_content(item) for item in value)
        return False


class DependencyAnalyzer:
    def analyze(self, workflow: WorkflowDefinition) -> list[str]:
        step_ids = [step.id for step in workflow.steps]
        outgoing: dict[str, list[str]] = defaultdict(list)
        edges: set[tuple[str, str]] = set()
        incoming_count = {step_id: 0 for step_id in step_ids}

        def add_edge(source: str, target: str) -> None:
            edge = (source, target)
            if edge in edges:
                return
            edges.add(edge)
            outgoing[source].append(target)
            incoming_count[target] += 1

        for step in workflow.steps:
            for dependency in step.dependencies:
                add_edge(dependency, step.id)
            for branch in step.branches:
                add_edge(step.id, branch.target_step_id)
            if step.on_failure_step_id is not None:
                add_edge(step.id, step.on_failure_step_id)

        ready = deque(step_id for step_id in step_ids if incoming_count[step_id] == 0)
        order: list[str] = []
        while ready:
            step_id = ready.popleft()
            order.append(step_id)
            for target in outgoing[step_id]:
                incoming_count[target] -= 1
                if incoming_count[target] == 0:
                    ready.append(target)
        if len(order) != len(step_ids):
            raise PlanningError(
                422,
                "invalid_workflow_plan",
                "The workflow dependency graph contains a cycle.",
            )
        return order


class WorkflowCompiler:
    def compile(
        self,
        workflow: WorkflowDefinition,
        dependency_order: Sequence[str],
    ) -> WorkflowDefinition:
        step_ids = {step.id for step in workflow.steps}
        if len(dependency_order) != len(step_ids) or set(dependency_order) != step_ids:
            raise PlanningError(
                422,
                "invalid_workflow_plan",
                "The workflow dependency analysis is incomplete.",
            )
        try:
            return WorkflowDefinition.model_validate(workflow.model_dump(mode="json"))
        except ValidationError as exc:
            raise PlanningError(
                422,
                "invalid_workflow_plan",
                "The generated workflow could not be compiled.",
            ) from exc


class PlanningErrorHandler:
    def handle(self, error: PlanningError) -> PlanningFailure:
        return PlanningFailure(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
        )
