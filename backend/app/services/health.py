import logging

import redis
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


def is_database_ready(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        logger.warning("Database readiness probe failed", exc_info=True)
        return False


def is_redis_ready(client: redis.Redis) -> bool:
    try:
        return bool(client.ping())
    except redis.exceptions.RedisError:
        logger.warning("Redis readiness probe failed", exc_info=True)
        return False
