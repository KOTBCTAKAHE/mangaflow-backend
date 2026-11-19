"""«Читать вместе»: small WebSocket rooms (page sync + reactions).

Room metadata and the last known state live in the cache (Redis in production)
so any API worker can serve any member; messages fan out through Redis pub/sub,
or in-process when running with the memory cache (tests, single worker).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from typing import Any, Protocol

from ..core.cache import Cache
from ..core.logging import get_logger

log = get_logger(__name__)

ROOM_TTL = 6 * 3600
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L — read aloud easily
CHANNEL = "mfroom:"


class Socket(Protocol):
    async def send_text(self, data: str) -> None: ...


class RoomHub:
    def __init__(self, cache: Cache, redis_url: str | None = None) -> None:
        self.cache = cache
        self._local: dict[str, set[Socket]] = {}
        self._redis = None
        self._listener: asyncio.Task[None] | None = None
        if redis_url and not redis_url.startswith("memory://"):
            from redis.asyncio import Redis

            self._redis = Redis.from_url(redis_url, decode_responses=True)

    async def aclose(self) -> None:
        if self._listener:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()

    # ------------------------------------------------------------ room data
    async def create(self, info: dict[str, Any]) -> str:
        for _ in range(10):
            code = "".join(secrets.choice(ALPHABET) for _ in range(6))
            if await self.cache.get(f"room:{code}") is None:
                await self.cache.set(f"room:{code}", info, ROOM_TTL)
                await self.cache.set(f"room:{code}:state", {"chapter_id": info.get("chapter_id"), "page": 0}, ROOM_TTL)
                return code
        raise RuntimeError("no free room code")

    async def info(self, code: str) -> dict[str, Any] | None:
        return await self.cache.get(f"room:{code.upper()}")

    async def state(self, code: str) -> dict[str, Any]:
        return await self.cache.get(f"room:{code}:state") or {}

    async def set_state(self, code: str, state: dict[str, Any]) -> None:
        await self.cache.set(f"room:{code}:state", state, ROOM_TTL)
        info = await self.info(code)
        if info is not None:
            await self.cache.set(f"room:{code}", info, ROOM_TTL)  # keep active rooms alive

    async def members(self, code: str) -> list[str]:
        return list((await self.cache.get(f"room:{code}:members") or {}).values())

    async def _members_update(self, code: str, uid: str, name: str | None) -> None:
        m = await self.cache.get(f"room:{code}:members") or {}
        if name is None:
            m.pop(uid, None)
        else:
            m[uid] = name
        await self.cache.set(f"room:{code}:members", m, ROOM_TTL)

    # ------------------------------------------------------------ fan-out
    async def _ensure_listener(self) -> None:
        if self._redis is None or self._listener is not None:
            return
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(CHANNEL + "*")

        async def run() -> None:
            async for msg in pubsub.listen():
                if msg.get("type") != "pmessage":
                    continue
                code = str(msg["channel"])[len(CHANNEL):]
                await self._deliver(code, str(msg["data"]))

        self._listener = asyncio.create_task(run())

    async def _deliver(self, code: str, data: str) -> None:
        for ws in list(self._local.get(code, ())):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001 — a dead socket is removed by its own handler
                self._local.get(code, set()).discard(ws)

    async def publish(self, code: str, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False)
        if self._redis is not None:
            try:
                await self._redis.publish(CHANNEL + code, data)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("room_publish_failed", error=str(exc))
        await self._deliver(code, data)

    async def join(self, code: str, ws: Socket, uid: str, name: str) -> None:
        await self._ensure_listener()
        self._local.setdefault(code, set()).add(ws)
        await self._members_update(code, uid, name)
        await self.publish(code, {"t": "join", "uid": uid, "name": name, "members": await self.members(code)})

    async def leave(self, code: str, ws: Socket, uid: str, name: str) -> None:
        self._local.get(code, set()).discard(ws)
        if not self._local.get(code):
            self._local.pop(code, None)
        await self._members_update(code, uid, None)
        await self.publish(code, {"t": "leave", "uid": uid, "name": name, "members": await self.members(code)})
