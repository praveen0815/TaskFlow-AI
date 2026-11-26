from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
import redis
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import (
    APIError,
    RateLimitExceeded,
    api_error_handler,
    http_exception_handler,
    rate_limit_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.logging import configure_logging
from app.db import create_database_engine, create_session_factory
from app.redis_client import create_redis_client
from app.services.execution_engine import recover_interrupted_executions
from app.services.tools import ToolRegistry, create_default_tool_registry

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.planning.providers import PlanningProvider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        if app.state.session_factory is not None:
            with app.state.session_factory() as db:
                try:
                    recovered = recover_interrupted_executions(db)
                    if recovered:
                        logger.warning(
                            "Recovered interrupted workflow executions.",
                            extra={"execution_count": recovered},
                        )
                except SQLAlchemyError:
                    db.rollback()
                    logger.exception(
                        "Workflow execution recovery failed; API startup will continue."
                    )
        yield
    finally:
        app.state.database_engine.dispose()
        app.state.redis_client.close()


def create_app(
    settings: Settings | None = None,
    *,
    database_engine: Engine | None = None,
    redis_client: redis.Redis | None = None,
    planning_provider: PlanningProvider | None = None,
    tool_registry: ToolRegistry | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.app_log_level)
    app = FastAPI(
        title="TaskFlow AI API",
        version="1.0.0",
        description="Versioned API foundation for TaskFlow AI.",
        lifespan=lifespan,
    )
    app.state.database_engine = (
        database_engine
        if database_engine is not None
        else create_database_engine(app_settings.database_url)
    )
    app.state.redis_client = (
        redis_client
        if redis_client is not None
        else create_redis_client(app_settings.redis_url)
    )
    app.state.session_factory = (
        create_session_factory(app.state.database_engine)
        if isinstance(app.state.database_engine, Engine)
        else None
    )
    app.state.settings = app_settings
    app.state.planning_provider = planning_provider
    app.state.tool_registry = tool_registry or create_default_tool_registry()
    app.include_router(api_router, prefix="/api/v1")
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    return app


app = create_app()
