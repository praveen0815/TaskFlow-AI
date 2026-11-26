from collections.abc import Sequence
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.schemas.planning import (
    IntentInterpretation,
    PlanningFailure,
    StructuredWorkflowPlan,
    ToolCatalogEntry,
)
from app.schemas.workflows import WorkflowDefinition
from app.services.planning.components import (
    DependencyAnalyzer,
    IntentInterpreter,
    PlanValidator,
    PlanningErrorHandler,
    WorkflowCompiler,
    WorkflowPlanner,
)
from app.services.planning.errors import PlanningError
from app.services.planning.providers import PlanningProvider


class PlanningState(TypedDict, total=False):
    request: str
    tool_catalog: list[ToolCatalogEntry]
    intent: IntentInterpretation
    plan: StructuredWorkflowPlan
    validated_workflow: WorkflowDefinition
    dependency_order: list[str]
    compiled_workflow: WorkflowDefinition
    failure: PlanningFailure


class WorkflowPlanningGraph:
    def __init__(
        self,
        provider: PlanningProvider,
        tool_catalog: Sequence[ToolCatalogEntry],
    ) -> None:
        self.provider = provider
        self.tool_catalog = tuple(tool_catalog)
        self.intent_interpreter = IntentInterpreter(provider)
        self.workflow_planner = WorkflowPlanner(provider)
        self.plan_validator = PlanValidator()
        self.dependency_analyzer = DependencyAnalyzer()
        self.workflow_compiler = WorkflowCompiler()
        self.error_handler = PlanningErrorHandler()
        self._graph = self._build_graph()

    async def plan(self, request: str) -> PlanningState:
        return await self._graph.ainvoke(
            {
                "request": request,
                "tool_catalog": list(self.tool_catalog),
            }
        )

    def _build_graph(self):
        builder = StateGraph(PlanningState)
        builder.add_node("intent_interpreter", self._interpret)
        builder.add_node("workflow_planner", self._plan)
        builder.add_node("plan_validator", self._validate)
        builder.add_node("dependency_analyzer", self._analyze_dependencies)
        builder.add_node("workflow_compiler", self._compile)
        builder.add_node("planning_error_handler", self._handle_error)
        builder.add_edge(START, "intent_interpreter")
        builder.add_conditional_edges(
            "intent_interpreter",
            self._route_failure,
            {"continue": "workflow_planner", "error": "planning_error_handler"},
        )
        builder.add_conditional_edges(
            "workflow_planner",
            self._route_failure,
            {"continue": "plan_validator", "error": "planning_error_handler"},
        )
        builder.add_conditional_edges(
            "plan_validator",
            self._route_failure,
            {"continue": "dependency_analyzer", "error": "planning_error_handler"},
        )
        builder.add_conditional_edges(
            "dependency_analyzer",
            self._route_failure,
            {"continue": "workflow_compiler", "error": "planning_error_handler"},
        )
        builder.add_conditional_edges(
            "workflow_compiler",
            self._route_failure,
            {"continue": END, "error": "planning_error_handler"},
        )
        builder.add_edge("planning_error_handler", END)
        return builder.compile()

    async def _interpret(self, state: PlanningState) -> PlanningState:
        try:
            return {
                "intent": await self.intent_interpreter.interpret(state["request"])
            }
        except PlanningError as exc:
            return self._failure(exc)

    async def _plan(self, state: PlanningState) -> PlanningState:
        try:
            return {
                "plan": await self.workflow_planner.plan(
                    state["request"],
                    state["intent"],
                    state["tool_catalog"],
                )
            }
        except PlanningError as exc:
            return self._failure(exc)

    def _validate(self, state: PlanningState) -> PlanningState:
        try:
            workflow = self.plan_validator.validate(
                state["intent"],
                state["plan"],
                state["tool_catalog"],
            )
            return {"validated_workflow": workflow}
        except PlanningError as exc:
            return self._failure(exc)
        except ValidationError as exc:
            return self._failure(
                PlanningError(
                    422,
                    "invalid_workflow_plan",
                    "The generated workflow did not pass validation.",
                )
            )

    def _analyze_dependencies(self, state: PlanningState) -> PlanningState:
        try:
            return {
                "dependency_order": self.dependency_analyzer.analyze(
                    state["validated_workflow"]
                )
            }
        except PlanningError as exc:
            return self._failure(exc)

    def _compile(self, state: PlanningState) -> PlanningState:
        try:
            return {
                "compiled_workflow": self.workflow_compiler.compile(
                    state["validated_workflow"],
                    state["dependency_order"],
                )
            }
        except PlanningError as exc:
            return self._failure(exc)

    def _handle_error(self, state: PlanningState) -> PlanningState:
        return {"failure": state["failure"]}

    def _failure(self, error: PlanningError) -> PlanningState:
        return {"failure": self.error_handler.handle(error)}

    @staticmethod
    def _route_failure(state: PlanningState) -> str:
        return "error" if "failure" in state else "continue"
