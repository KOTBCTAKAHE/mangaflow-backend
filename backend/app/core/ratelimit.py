"""Fixed-window rate limiting keyed by user id or client IP."""
from __future__ import annotations

import time

from fastapi import Request

from .config import get_settings
from .errors import RateLimitedError


def client_key(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"u:{user_id}"
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "?")
    return f"ip:{ip}"


class RateLimit:
    """FastAPI dependency: ``Depends(RateLimit("auth", 20))``."""

    def __init__(self, scope: str, limit: int | None = None) -> None:
        self.scope = scope
        self.limit = limit

    async def __call__(self, request: Request) -> None:
        s = get_settings()
        if not s.rate_limit_enabled:
            return
        limit = self.limit or s.rate_limit_default
        window = s.rate_limit_window_seconds
        bucket = int(time.time() // window)
        key = f"rl:{self.scope}:{client_key(request)}:{bucket}"
        count = await request.app.state.cache.incr(key, window)
        if count > limit:
            retry = window - int(time.time() % window)
            raise RateLimitedError("Слишком много запросов, попробуйте позже", details={"retry_after": retry})
