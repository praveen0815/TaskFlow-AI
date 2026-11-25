from typing import Literal

import redis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine

from app.services.health import is_database_ready, is_redis_ready

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "ready", "not_ready"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "unavailable"]]


@router.get("/live", response_model=HealthResponse)
def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse)
def readiness(request: Request) -> ReadinessResponse | JSONResponse:
    engine: Engine = request.app.state.database_engine
    redis_client: redis.Redis = request.app.state.redis_client
    checks = {
        "database": "ok" if is_database_ready(engine) else "unavailable",
        "redis": "ok" if is_redis_ready(redis_client) else "unavailable",
    }
    ready = all(status == "ok" for status in checks.values())
    response = ReadinessResponse(
        status="ready" if ready else "not_ready",
        checks=checks,
    )
    if not ready:
        return JSONResponse(status_code=503, content=response.model_dump())
    return response
