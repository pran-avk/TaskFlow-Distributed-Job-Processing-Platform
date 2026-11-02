import logging
import time
from collections.abc import Callable

import redis
from fastapi import Depends, HTTPException, status

from core.config import Settings, get_settings
from core.security import get_current_user
from taskqueue.redis_client import get_redis
from users.models.auth import User

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


def rate_limit(scope: str, limit_of: Callable[[Settings], int]):
    """Fixed-window per-user limit backed by Redis. Fails open if Redis is down,
    so a cache outage does not take the API down with it."""

    def dependency(user: User = Depends(get_current_user)) -> None:
        limit = limit_of(get_settings())
        if limit <= 0:
            return
        window = int(time.time() // WINDOW_SECONDS)
        key = f"taskflow:ratelimit:{scope}:{user.id}:{window}"
        try:
            client = get_redis()
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS + 5)
            count = pipe.execute()[0]
        except redis.RedisError:
            logger.warning("Rate limiter unavailable; allowing request")
            return
        if count > limit:
            retry_after = WINDOW_SECONDS - int(time.time()) % WINDOW_SECONDS
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency
