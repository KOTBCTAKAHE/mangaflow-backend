"""JSON cache abstraction with Redis and in-memory implementations."""
from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from .logging import get_logger

log = get_logger(__name__)


class Cache(ABC):
    @abstractmethod
    async def get(self, key: str) -> Any | None: ...

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: int) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def incr(self, key: str, ttl: int) -> int: ...

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get_or_set(self, key: str, ttl: int, factory: Callable[[], Awaitable[Any]]) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await factory()
        await self.set(key, value, ttl)
        return value


class MemoryCache(Cache):
    def __init__(self, max_items: int = 5000) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_items
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if not item:
            return None
        exp, value = item
        if exp < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if len(self._data) >= self._max:
            # drop the oldest-expiring 10%
            for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[: self._max // 10]:
                self._data.pop(k, None)
        self._data[key] = (time.monotonic() + ttl, json.loads(json.dumps(value, default=str)))

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        async with self._lock:
            current = await self.get(key) or 0
            exp = self._data.get(key, (time.monotonic() + ttl, 0))[0]
            self._data[key] = (exp, int(current) + 1)
            return int(current) + 1


class RedisCache(Cache):
    """Redis-backed cache. Failures degrade gracefully to cache misses."""

    def __init__(self, url: str, prefix: str = "mf:") -> None:
        self._redis: Redis = Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        self._prefix = prefix

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(self._prefix + key)
        except RedisError as exc:
            log.warning("cache_get_failed", error=str(exc))
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self._redis.set(self._prefix + key, json.dumps(value, default=str, ensure_ascii=False), ex=ttl)
        except RedisError as exc:
            log.warning("cache_set_failed", error=str(exc))

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(self._prefix + key)
        except RedisError:
            pass

    async def incr(self, key: str, ttl: int) -> int:
        k = self._prefix + key
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(k)
                pipe.expire(k, ttl, nx=True)
                value, _ = await pipe.execute()
            return int(value)
        except RedisError as exc:
            log.warning("rate_limit_backend_down", error=str(exc))
            return 0  # fail open

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        await self._redis.aclose()
