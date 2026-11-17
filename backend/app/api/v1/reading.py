"""Progress, history, downloads and chapter-update feed."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Request, status

from mangaflow_sources import SourceError

from ...core.ratelimit import RateLimit
from ...repositories.library import LibraryRepository
from ...repositories.reading import (
    DownloadRepository,
    HistoryRepository,
    ProgressRepository,
    UpdateRepository,
)
from ...schemas.common import Message, SyncResponse
from ...schemas.reading import (
    ContinueReadingOut,
    DownloadBatchIn,
    DownloadOut,
    HistoryBatchIn,
    HistoryOut,
    ManifestIn,
    ManifestOut,
    ProgressBatchIn,
    ProgressIn,
    ProgressOut,
    SeenIn,
    UpdateOut,
)
from ...services.sync import SyncService
from ...services.updates import check_updates
from ..deps import CurrentUser, PresenterDep, SessionDep, SourcesDep
from fastapi import Depends

progress_router = APIRouter(prefix="/progress", tags=["progress"])
history_router = APIRouter(prefix="/history", tags=["history"])
downloads_router = APIRouter(prefix="/downloads", tags=["downloads"])
updates_router = APIRouter(prefix="/updates", tags=["updates"])


# ---------------------------------------------------------------- progress
@progress_router.get("", response_model=SyncResponse[ProgressOut])
async def list_progress(user: CurrentUser, session: SessionDep, since: datetime | None = None,
                        source_id: str | None = None, manga_id: str | None = None) -> Any:
    server_time = datetime.now(UTC)
    items = await ProgressRepository(session).list(user.id, since=since, source_id=source_id, manga_id=manga_id)
    return {"items": items, "server_time": server_time}


@progress_router.put("", response_model=ProgressOut)
async def put_progress(body: ProgressIn, user: CurrentUser, session: SessionDep) -> Any:
    return await SyncService(session).upsert_progress(user.id, body)


@progress_router.post("/batch", response_model=SyncResponse[ProgressOut])
async def batch_progress(body: ProgressBatchIn, user: CurrentUser, session: SessionDep) -> Any:
    items = await SyncService(session).sync_progress(user.id, body.items)
    return {"items": items, "server_time": datetime.now(UTC)}


@progress_router.get("/continue", response_model=list[ContinueReadingOut], summary="Продолжить чтение")
async def continue_reading(user: CurrentUser, session: SessionDep, limit: int = Query(20, ge=1, le=100)) -> Any:
    latest = await ProgressRepository(session).latest_per_manga(user.id, limit=limit * 2)
    lib = {(e.source_id, e.manga_id): e for e in await LibraryRepository(session).list(user.id)}
    out = []
    for p in latest:
        e = lib.get((p.source_id, p.manga_id))
        if e is None or e.status in ("completed", "dropped"):
            continue
        out.append({"source_id": p.source_id, "manga_id": p.manga_id, "title": e.title, "cover_url": e.cover_url,
                    "chapter_id": p.chapter_id, "chapter_title": p.chapter_title, "chapter_number": p.chapter_number,
                    "page_index": p.page_index, "page_count": p.page_count, "updated_at": p.updated_at})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- history
@history_router.get("", response_model=SyncResponse[HistoryOut])
async def list_history(user: CurrentUser, session: SessionDep, since: datetime | None = None,
                       limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> Any:
    server_time = datetime.now(UTC)
    return {"items": await HistoryRepository(session).list(user.id, since=since, limit=limit, offset=offset),
            "server_time": server_time}


@history_router.post("", response_model=Message)
async def add_history(body: HistoryBatchIn, user: CurrentUser, session: SessionDep) -> Message:
    added = await SyncService(session).add_history(user.id, body.items)
    return Message(message=f"added {added}")


@history_router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_history(entry_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await HistoryRepository(session).soft_delete(user.id, datetime.now(UTC), entry_id=entry_id)


@history_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_history(user: CurrentUser, session: SessionDep, source_id: str | None = None,
                        manga_id: str | None = None) -> None:
    await HistoryRepository(session).soft_delete(user.id, datetime.now(UTC), source_id=source_id, manga_id=manga_id)


# ---------------------------------------------------------------- downloads
@downloads_router.get("", response_model=list[DownloadOut])
async def list_downloads(user: CurrentUser, session: SessionDep, device_id: str | None = None) -> Any:
    return await DownloadRepository(session).list(user.id, device_id)


@downloads_router.post("", response_model=list[DownloadOut])
async def report_downloads(body: DownloadBatchIn, user: CurrentUser, session: SessionDep) -> Any:
    return await SyncService(session).upsert_downloads(user.id, body.items)


@downloads_router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_device(device_id: str, user: CurrentUser, session: SessionDep) -> None:
    await DownloadRepository(session).delete_device(user.id, device_id)


@downloads_router.post("/manifest", response_model=ManifestOut,
                       dependencies=[Depends(RateLimit("manifest", 30))],
                       summary="Списки страниц для пакетной офлайн-загрузки")
async def manifest(body: ManifestIn, svc: SourcesDep, pres: PresenterDep) -> Any:
    sem = asyncio.Semaphore(3)

    async def one(cid: str) -> dict[str, Any]:
        async with sem:
            try:
                raw = await svc.pages(body.source_id, cid)
                return {"chapter_id": cid, "ok": True, "pages": pres.pages(body.source_id, raw)}
            except SourceError as exc:
                return {"chapter_id": cid, "ok": False, "error_code": exc.code, "error": exc.message}
            except Exception as exc:  # noqa: BLE001
                return {"chapter_id": cid, "ok": False, "error_code": "error", "error": str(exc)}

    svc.adapter(body.source_id)  # 404 for unknown source before fan-out
    return {"source_id": body.source_id, "chapters": await asyncio.gather(*(one(c) for c in body.chapter_ids))}


# ---------------------------------------------------------------- updates
@updates_router.get("", response_model=list[UpdateOut])
async def list_updates(user: CurrentUser, session: SessionDep, unseen: bool = False,
                       since: datetime | None = None, limit: int = Query(100, ge=1, le=500)) -> Any:
    return await UpdateRepository(session).list(user.id, unseen_only=unseen, since=since, limit=limit)


@updates_router.post("/seen", response_model=Message)
async def mark_seen(body: SeenIn, user: CurrentUser, session: SessionDep) -> Message:
    n = await UpdateRepository(session).mark_seen(user.id, None if body.all else (body.ids or []))
    return Message(message=f"marked {n}")


@updates_router.post("/check", response_model=Message, status_code=status.HTTP_202_ACCEPTED,
                     dependencies=[Depends(RateLimit("update_check", 4))])
async def trigger_check(user: CurrentUser, request: Request, background: BackgroundTasks) -> Message:
    background.add_task(check_updates, request.app.state.sessionmaker, request.app.state.sources, user_id=user.id)
    return Message(message="Проверка обновлений запущена")
