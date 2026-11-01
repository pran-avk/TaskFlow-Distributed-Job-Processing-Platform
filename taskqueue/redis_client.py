import redis

from core.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _client


def set_redis(client: redis.Redis | None) -> None:
    """Replace the shared client (used by tests)."""
    global _client
    _client = client
