"""Resilient HTTP client used by adapters.

* per-source token-bucket rate limiting (be polite to the sites);
* bounded retries with exponential backoff for network errors / 429 / 5xx;
* honours ``Retry-After``;
* maps HTTP failures to :mod:`mangaflow_sources.errors`.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any

import httpx

from .errors import (
    SourceAuthRequiredError,
    SourceBlockedError,
    SourceNotFoundError,
    SourceParseError,
    SourceRateLimitedError,
    SourceUnavailableError,
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 524}


class RateLimiter:
    """Async token bucket: ``rate`` requests per second, burst ``capacity``."""

    def __init__(self, rate: float, capacity: int | None = None) -> None:
        self.rate = rate
        self.capacity = capacity or max(1, int(rate * 2))
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


class SourceHttpClient:
    def __init__(
        self,
        source_id: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        rate_per_second: float = 4.0,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
    ) -> None:
        self.source_id = source_id
        self.max_retries = max_retries
        self._limiter = RateLimiter(rate_per_second)
        base_headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
        base_headers.update(headers or {})
        self._client = httpx.AsyncClient(
            headers=base_headers,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
            follow_redirects=True,
            transport=transport,
            proxy=proxy,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: Any = None,
        json_body: Any = None,
        allow_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        attempt = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise SourceUnavailableError(self.source_id, f"network error: {exc!r}") from exc
                await self._backoff(attempt, None)
                attempt += 1
                continue

            if resp.status_code < 400 or resp.status_code in allow_statuses:
                return resp
            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            self._raise_for(resp)

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 0.5 * (2**attempt) + random.uniform(0, 0.25)
        if retry_after:
            try:
                delay = min(float(retry_after), 10.0)
            except ValueError:
                pass
        await asyncio.sleep(delay)

    def _raise_for(self, resp: httpx.Response) -> None:
        code = resp.status_code
        msg = f"HTTP {code} for {resp.request.url.host}"
        if code in (401, 422):
            raise SourceAuthRequiredError(self.source_id, msg, status=code)
        if code in (403, 451):
            raise SourceBlockedError(self.source_id, msg + " (geo-block / anti-bot)", status=code)
        if code == 404:
            raise SourceNotFoundError(self.source_id, msg, status=code)
        if code == 429:
            raise SourceRateLimitedError(self.source_id, msg, status=code)
        raise SourceUnavailableError(self.source_id, msg, status=code)

    async def get_text(self, url: str, **kw: Any) -> str:
        resp = await self.request("GET", url, **kw)
        return resp.text

    async def get_json(self, url: str, **kw: Any) -> Any:
        resp = await self.request("GET", url, **kw)
        return self._decode(resp)

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        resp = await self.request("POST", url, json_body=body, **kw)
        return self._decode(resp)

    def _decode(self, resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise SourceParseError(self.source_id, "invalid JSON response") from exc
