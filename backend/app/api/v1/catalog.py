"""Sources, manga, chapters, search, home feed and image proxy."""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse

from mangaflow_sources import SearchQuery, SortOrder, SourceError

from ...core.errors import AppError
from ...core.ratelimit import RateLimit
from ...repositories.library import LibraryRepository
from ...repositories.reading import SearchHistoryRepository
from ...schemas.catalog import (
    ChapterOut,
    HomeOut,
    MangaList,
    PagesOut,
    SearchOut,
    SourceHealthOut,
    SourceOut,
    TitleOut,
)
from ...services.sources import parse_query
from ..deps import OptionalUser, PresenterDep, SearchDep, SessionDep, SourcesDep

limited = [Depends(RateLimit("catalog"))]
sources_router = APIRouter(prefix="/sources", tags=["sources"], dependencies=limited)
manga_router = APIRouter(prefix="/manga", tags=["manga"], dependencies=limited)
chapters_router = APIRouter(prefix="/chapters", tags=["chapters"], dependencies=limited)
search_router = APIRouter(prefix="/search", tags=["search"], dependencies=limited)
images_router = APIRouter(prefix="/images", tags=["images"])

ListQ = Annotated[list[str] | None, Query()]


def _filters(q: str, page: int, sort: str, genres: list[str] | None, exclude_genres: list[str] | None,
             types: list[str] | None, statuses: list[str] | None) -> SearchQuery:
    return parse_query(q=q, page=page, sort=sort, genres=genres, exclude_genres=exclude_genres, types=types,
                       statuses=statuses)


# ---------------------------------------------------------------- sources
@sources_router.get("", response_model=list[SourceOut])
async def list_sources(svc: SourcesDep, include_nsfw: bool = False) -> Any:
    return svc.list_sources(include_nsfw=include_nsfw)


@sources_router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: str, svc: SourcesDep) -> Any:
    return svc.source(source_id)


@sources_router.get("/{source_id}/health", response_model=SourceHealthOut)
async def source_health(source_id: str, svc: SourcesDep, force: bool = False) -> Any:
    return await svc.health(source_id, force=force)


# ---------------------------------------------------------------- manga
@manga_router.get("/{source_id}", response_model=MangaList, summary="Каталог источника с фильтрами")
async def browse(
    source_id: str, svc: SourcesDep, pres: PresenterDep,
    q: str = "", page: int = Query(1, ge=1, le=500), sort: str = "popular",
    genres: ListQ = None, exclude_genres: ListQ = None, types: ListQ = None, statuses: ListQ = None,
) -> Any:
    query = _filters(q, page, sort, genres, exclude_genres, types, statuses)
    return pres.listing(await svc.browse(source_id, query))


@manga_router.get("/{source_id}/{manga_id}", response_model=TitleOut, summary="Тайтл и список глав")
async def title(source_id: str, manga_id: str, svc: SourcesDep, pres: PresenterDep, refresh: bool = False) -> Any:
    return pres.title(await svc.title(source_id, manga_id, refresh=refresh))


@manga_router.get("/{source_id}/{manga_id}/chapters", response_model=list[ChapterOut])
async def chapters(source_id: str, manga_id: str, svc: SourcesDep, order: str = Query("desc", pattern="^(asc|desc)$")) -> Any:
    chs = (await svc.title(source_id, manga_id))["chapters"]
    return list(reversed(chs)) if order == "asc" else chs


# ---------------------------------------------------------------- chapters
@chapters_router.get("/{source_id}/{chapter_id}/pages", response_model=PagesOut)
async def pages(source_id: str, chapter_id: str, svc: SourcesDep, pres: PresenterDep) -> Any:
    raw = await svc.pages(source_id, chapter_id)
    return {"source_id": source_id, "chapter_id": chapter_id, "pages": pres.pages(source_id, raw)}


# ---------------------------------------------------------------- search
@search_router.get("", response_model=SearchOut, summary="Глобальный поиск по всем источникам")
async def global_search(
    svc: SourcesDep, search: SearchDep, pres: PresenterDep, user: OptionalUser, session: SessionDep,
    q: str = Query("", max_length=200), page: int = Query(1, ge=1, le=50),
    sources: ListQ = None, include_nsfw: bool = False, sort: str = "popular",
    genres: ListQ = None, exclude_genres: ListQ = None, types: ListQ = None, statuses: ListQ = None,
) -> Any:
    query = _filters(q, page, sort, genres, exclude_genres, types, statuses)
    available = [s["id"] for s in svc.list_sources(include_nsfw=include_nsfw)]
    if sources:
        unknown = [s for s in sources if s not in available and s not in svc.registry]
        if unknown:
            raise AppError(f"Неизвестные источники: {', '.join(unknown)}", code="unknown_source", status_code=422)
        ids = [s for s in sources if s in svc.registry]
    else:
        ids = available
    result = await search.search(query, ids)
    if user is not None and q.strip() and page == 1:
        await SearchHistoryRepository(session).add(user.id, q)
    return pres.search(result)


@search_router.get("/history", response_model=list[str])
async def search_history(user: OptionalUser, session: SessionDep) -> list[str]:
    if user is None:
        return []
    return [h.query for h in await SearchHistoryRepository(session).list(user.id)]


@search_router.delete("/history", status_code=204)
async def clear_search_history(user: OptionalUser, session: SessionDep, q: str | None = None) -> None:
    if user is not None:
        await SearchHistoryRepository(session).clear(user.id, q)


# ---------------------------------------------------------------- home
@manga_router.get("/{source_id}/feed/home", response_model=HomeOut, summary="Подборки для главного экрана")
async def home(source_id: str, svc: SourcesDep, pres: PresenterDep, user: OptionalUser, session: SessionDep) -> Any:
    adapter = svc.adapter(source_id)
    caps = adapter.info.capabilities
    errors: list[dict[str, str]] = []
    library_keys: set[str] = set()
    recommended_genre: str | None = None
    if user is not None:
        entries = await LibraryRepository(session).list(user.id, sort="last_read")
        library_keys = {e.manga_id for e in entries if e.source_id == source_id}
        recommended_genre = await _favourite_genre(svc, adapter.info, entries[:6])

    plan: list[tuple[str, str, SearchQuery]] = [
        ("popular", "Популярное", SearchQuery(sort=SortOrder.POPULAR)),
        ("latest", "Последние обновления", SearchQuery(sort=SortOrder.UPDATED)),
    ]
    if SortOrder.CREATED in caps.sort_orders:
        plan.append(("new", "Недавно добавленные", SearchQuery(sort=SortOrder.CREATED)))
    if recommended_genre and caps.genre_filter:
        plan.append(("recommended", "Рекомендации для вас",
                     SearchQuery(sort=SortOrder.POPULAR, genres_include=[recommended_genre])))
    elif SortOrder.RATING in caps.sort_orders:
        plan.append(("recommended", "Высокий рейтинг", SearchQuery(sort=SortOrder.RATING, page=2)))

    async def run(key: str, title: str, q: SearchQuery) -> dict[str, Any] | None:
        try:
            data = await svc.browse(source_id, q)
        except SourceError as exc:
            errors.append({"source_id": source_id, "code": exc.code, "message": exc.message})
            return None
        items = data["items"]
        if key == "recommended":
            items = [c for c in items if c["id"] not in library_keys]
        return {"key": key, "title": title, "items": pres.cards(items[:20])}

    sections = [s for s in await asyncio.gather(*(run(*p) for p in plan)) if s and s["items"]]
    return {"source_id": source_id, "sections": sections, "errors": errors}


async def _favourite_genre(svc, info, entries) -> str | None:
    if not info.genres:
        return None
    names = {g.name.lower(): g.id for g in info.genres}
    counter: Counter[str] = Counter()
    for e in entries:
        try:
            data = await svc.title(e.source_id, e.manga_id)
        except Exception:  # noqa: BLE001 - recommendations are best-effort
            continue
        for g in data["manga"]["genres"]:
            gid = names.get(g.lower())
            if gid:
                counter[gid] += 1
    return counter.most_common(1)[0][0] if counter else None


# ---------------------------------------------------------------- images
@images_router.get("/{token}", summary="Подписанный прокси изображений (CDN-friendly)")
async def image(token: str, request: Request, w: int | None = Query(None, ge=64, le=2000)) -> Response:
    img = await request.app.state.images.get(token, w)
    headers = {"Cache-Control": "public, max-age=604800, immutable", "X-Content-Type-Options": "nosniff"}
    if img.path is None:
        return Response(content=img.data or b"", media_type=img.content_type, headers=headers)
    return FileResponse(img.path, media_type=img.content_type, headers=headers)

