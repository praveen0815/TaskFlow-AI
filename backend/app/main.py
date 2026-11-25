from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
import redis
from sqlalchemy import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.logging import configure_logging
from app.db import create_database_engine
from app.redis_client import create_redis_client


def create_app(
    settings: Settings | None = None,
    *,
    database_engine: Engine | None = None,
    redis_client: redis.Redis | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.app_log_level)
    app = FastAPI(
        title="TaskFlow AI API",
        version="1.0.0",
        description="Versioned API foundation for TaskFlow AI.",
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
    app.include_router(api_router, prefix="/api/v1")
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    return app


app = create_app()
