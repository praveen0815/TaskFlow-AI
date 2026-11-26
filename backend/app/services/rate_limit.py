import hashlib
import logging

import redis
from fastapi import HTTPException, Request

from app.core.errors import RateLimitExceeded

logger = logging.getLogger(__name__)

_INCREMENT_WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('TTL', KEYS[1])}
"""


def enforce_rate_limit(request: Request, *, limit: int, window_seconds: int = 60) -> None:
    address = request.client.host if request.client is not None else "unknown"
    bucket = hashlib.sha256(f"{request.url.path}:{address}".encode()).hexdigest()
    key = f"taskflow:rate-limit:{bucket}"
    try:
        count, ttl = request.app.state.redis_client.eval(
            _INCREMENT_WINDOW_SCRIPT,
            1,
            key,
            window_seconds,
        )
    except redis.exceptions.RedisError as exc:
        logger.exception("Rate limit storage unavailable", exc_info=exc)
        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable.",
        ) from exc

    if int(count) > limit:
        raise RateLimitExceeded(max(1, int(ttl)))


def auth_rate_limit(request: Request) -> None:
    enforce_rate_limit(
        request,
        limit=request.app.state.settings.auth_rate_limit_per_minute,
    )
