"""Application-facing facade over the Source Adapter Layer.

Responsibilities: id encoding, caching (Redis), health tracking and turning
adapter dataclasses into plain JSON-able dicts. Image URLs are left raw here
and wrapped by :mod:`app.services.presenter` per request.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from mangaflow_sources import (
    MangaType,
    PublicationStatus,
    SearchQuery,
    SortOrder,
    SourceAdapter,
    SourceError,
    SourceRegistry,
    decode_ref,
    encode_ref,
)

from ..core.cache import Cache
from ..core.config import Settings
from ..core.errors import AppError
from ..core.logging import get_logger

log = get_logger(__name__)


def _plain(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def decode_id(public: str) -> str:
    try:
        return decode_ref(public)
    except ValueError:
        raise AppError("Некорректный идентификатор", code="invalid_id", status_code=400) from None


class SourceService:
    def __init__(self, registry: SourceRegistry, cache: Cache, settings: Settings) -> None:
        self.registry = registry
        self.cache = cache
        self.settings = settings

    # ------------------------------------------------------------ sources
    def adapter(self, source_id: str) -> SourceAdapter:
        return self.registry.get(source_id)

    def list_sources(self, include_nsfw: bool = True) -> list[dict[str, Any]]:
        out = []
        for info in self.registry.infos():
            if info.is_nsfw and not include_nsfw:
                continue
            d = _plain(asdict(info))
            out.append(d)
        return out

    def source(self, source_id: str) -> dict[str, Any]:
        return _plain(asdict(self.adapter(source_id).info))

    async def health(self, source_id: str, *, force: bool = False) -> dict[str, Any]:
        key = f"health:{source_id}"
        if not force and (cached := await self.cache.get(key)):
            return cached
        adapter = self.adapter(source_id)
        started = time.perf_counter()
        result: dict[str, Any] = {"id": source_id, "checked_at": datetime.now(UTC).isoformat()}
        try:
            page = await adapter.popular(1)
            result.update(ok=bool(page.items), latency_ms=int((time.perf_counter() - started) * 1000))
            if not page.items:
                result.update(error_code="empty", error="Источник вернул пустой каталог")
        except SourceError as exc:
            result.update(ok=False, error_code=exc.code, error=exc.message)
        await self.cache.set(key, result, 300)
        return result

    # ------------------------------------------------------------ catalog
    @staticmethod
    def _cards(source_id: str, items: list[Any]) -> list[dict[str, Any]]:
        return [
            {"source_id": source_id, "id": encode_ref(m.id), "title": m.title, "cover_url": m.cover_url,
             "type": m.type.value if m.type else None, "alt_titles": list(getattr(m, "alt_titles", None) or [])}
            for m in items
        ]

    async def browse(self, source_id: str, query: SearchQuery) -> dict[str, Any]:
        adapter = self.adapter(source_id)
        key = "cat:{}:{}:{}:{}:{}:{}:{}:{}".format(
            source_id, query.sort.value, query.page, query.query.strip().lower(),
            ",".join(sorted(query.genres_include)), ",".join(sorted(query.genres_exclude)),
            ",".join(sorted(t.value for t in query.types)), ",".join(sorted(s.value for s in query.statuses)),
        )
        ttl = self.settings.cache_ttl_search if query.query else self.settings.cache_ttl_catalog

        async def load() -> dict[str, Any]:
            if query.query or query.genres_include or query.genres_exclude or query.types or query.statuses:
                page = await adapter.search(query)
            else:
                page = await adapter.browse(query.sort, query.page)
            return {"source_id": source_id, "page": query.page, "has_next": page.has_next,
                    "items": self._cards(source_id, page.items)}

        return await self.cache.get_or_set(key, ttl, load)

    async def title(self, source_id: str, manga_public_id: str, *, refresh: bool = False) -> dict[str, Any]:
        adapter = self.adapter(source_id)
        raw = decode_id(manga_public_id)
        key = f"title:{source_id}:{manga_public_id}"
        if refresh:
            await self.cache.delete(key)

        async def load() -> dict[str, Any]:
            details, chapters = await adapter.title(raw)
            manga = _plain(asdict(details))
            manga.update(id=manga_public_id, source_id=source_id, source_name=adapter.info.name,
                         chapters_count=len(chapters))
            chs = []
            for c in chapters:
                d = _plain(asdict(c))
                d["id"] = encode_ref(c.id)
                chs.append(d)
            return {"manga": manga, "chapters": chs}

        cached = await self.cache.get(key)
        if cached is not None:
            return cached
        data = await load()
        # an empty chapter list is often temporary (token just added, site hiccup): keep it briefly
        await self.cache.set(key, data, self.settings.cache_ttl_details if data["chapters"] else 60)
        return data

    async def pages(self, source_id: str, chapter_public_id: str) -> list[dict[str, Any]]:
        adapter = self.adapter(source_id)
        raw = decode_id(chapter_public_id)
        key = f"pages:{source_id}:{chapter_public_id}"

        async def load() -> list[dict[str, Any]]:
            pages = await adapter.pages(raw)
            if not pages:
                raise AppError("В главе нет страниц", code="chapter_empty", status_code=404)
            return [{"index": p.index, "url": p.url} for p in pages]

        return await self.cache.get_or_set(key, self.settings.cache_ttl_pages, load)

    async def text(self, source_id: str, chapter_public_id: str) -> dict[str, Any]:
        """Ранобэ: a chapter as text blocks."""
        adapter = self.adapter(source_id)
        if adapter.info.content != "text":
            raise AppError("Этот источник — не ранобэ: глава состоит из страниц", code="not_text", status_code=400)
        raw = decode_id(chapter_public_id)

        async def load() -> dict[str, Any]:
            t = await adapter.text(raw)
            if not t.blocks:
                raise AppError("В главе нет текста", code="chapter_empty", status_code=404)
            return {"title": t.title, "blocks": t.blocks}

        return await self.cache.get_or_set(f"text:{source_id}:{chapter_public_id}", self.settings.cache_ttl_pages, load)


def parse_query(
    *, q: str = "", page: int = 1, sort: str = "popular", genres: list[str] | None = None,
    exclude_genres: list[str] | None = None, types: list[str] | None = None, statuses: list[str] | None = None,
) -> SearchQuery:
    try:
        return SearchQuery(
            query=q.strip(), page=page, sort=SortOrder(sort),
            genres_include=list(genres or []), genres_exclude=list(exclude_genres or []),
            types=[MangaType(t) for t in types or []], statuses=[PublicationStatus(s) for s in statuses or []],
        )
    except ValueError as exc:
        raise AppError(f"Некорректный фильтр: {exc}", code="invalid_filter", status_code=422) from None
