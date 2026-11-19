"""Client of the index service's plot search (descriptions → vectors)."""
from __future__ import annotations

from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import AppError


class PlotClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=20, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.imgindex_url)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise AppError("Поиск по сюжету выключен на сервере", code="plot_unavailable", status_code=503)
        try:
            r = await self.client.post(self.settings.imgindex_url.rstrip("/") + path, json=body)
        except httpx.HTTPError:
            raise AppError("Сервис поиска по сюжету недоступен", code="plot_unavailable", status_code=503) from None
        if r.status_code == 503:
            raise AppError("Модель поиска по сюжету ещё загружается", code="plot_unavailable", status_code=503)
        if r.status_code >= 400:
            raise AppError(f"Сервис поиска по сюжету ответил {r.status_code}", code="plot_failed", status_code=502)
        return r.json()

    async def search(self, q: str, limit: int = 20, exclude: list[str] | None = None) -> dict[str, Any]:
        return await self._post("/plot/search", {"q": q, "limit": limit, "exclude": exclude or []})

    async def similar(self, seeds: list[dict[str, Any]], limit: int = 20, exclude: list[str] | None = None) -> dict[str, Any]:
        return await self._post("/plot/similar", {"seeds": seeds, "limit": limit, "exclude": exclude or []})
