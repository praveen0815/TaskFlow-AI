import redis


def create_redis_client(redis_url: str) -> redis.Redis:
    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
