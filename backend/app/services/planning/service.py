from app.models import User
from app.schemas.planning import PlanningResult
from app.services.planning.errors import PlanningError
from app.services.planning.graph import WorkflowPlanningGraph
from app.services.planning.providers import PlanningProvider
from app.services.tools.registry import ToolRegistry
from app.services.workflows import create_workflow
from sqlalchemy.orm import Session


class WorkflowPlanningService:
    def __init__(self, provider: PlanningProvider, tool_registry: ToolRegistry) -> None:
        self.provider = provider
        self.graph = WorkflowPlanningGraph(provider, tool_registry.available_catalog())

    async def create_draft(
        self,
        db: Session,
        user: User,
        request: str,
    ) -> PlanningResult:
        state = await self.graph.plan(request)
        failure = state.get("failure")
        if failure is not None:
            raise PlanningError(
                failure.status_code,
                failure.code,
                failure.message,
            )

        intent = state["intent"]
        plan = state["plan"]
        workflow = create_workflow(
            db,
            user,
            state["compiled_workflow"],
            source_request=request,
            generation_provider=self.provider.name,
        )
        return PlanningResult(
            workflow=workflow,
            intent_summary=intent.summary,
            rationale=plan.rationale,
            assumptions=plan.assumptions,
            dependency_order=state["dependency_order"],
            provider=self.provider.name,
        )
