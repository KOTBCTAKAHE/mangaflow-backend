from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, status

from ...core.errors import ConflictError, NotFoundError
from ...models import Collection, LibraryEntry
from ...repositories.library import CollectionRepository, LibraryRepository
from ...schemas.common import SyncResponse
from ...schemas.library import (
    CollectionIn,
    CollectionItemsIn,
    CollectionOut,
    LibraryEntryIn,
    LibraryEntryOut,
    LibraryPatchIn,
    LibrarySyncIn,
)
from ...services.sync import SyncService
from ..deps import CurrentUser, SessionDep

router = APIRouter(prefix="/library", tags=["library"])
collections_router = APIRouter(prefix="/collections", tags=["collections"])


async def _out(session, user_id: uuid.UUID, entries: list[LibraryEntry]) -> list[LibraryEntryOut]:
    repo = LibraryRepository(session)
    cols = await repo.collections_for([e.id for e in entries])
    unread = await repo.unread_counts(user_id)
    return [LibraryEntryOut(
        id=e.id, source_id=e.source_id, manga_id=e.manga_id, title=e.title, cover_url=e.cover_url, status=e.status,
        is_favorite=e.is_favorite, reader_settings=e.reader_settings or {}, total_chapters=e.total_chapters,
        last_chapter_number=e.last_chapter_number, last_read_at=e.last_read_at, added_at=e.added_at,
        updated_at=e.updated_at, deleted=e.deleted_at is not None, collection_ids=cols.get(e.id, []),
        unread_updates=unread.get((e.source_id, e.manga_id), 0),
    ) for e in entries]


@router.get("", response_model=SyncResponse[LibraryEntryOut])
async def list_library(
    user: CurrentUser, session: SessionDep,
    since: datetime | None = Query(None, description="Только изменённые после (включая удалённые) — для синхронизации"),
    status_: str | None = Query(None, alias="status"), favorite: bool | None = None,
    collection_id: uuid.UUID | None = None, q: str | None = None,
    sort: str = Query("updated", pattern="^(updated|added|title|last_read)$"),
) -> Any:
    server_time = datetime.now(UTC)
    entries = await LibraryRepository(session).list(user.id, since=since, status=status_, favorite=favorite,
                                                    collection_id=collection_id, q=q, sort=sort)
    return {"items": await _out(session, user.id, entries), "server_time": server_time}


@router.post("/sync", response_model=SyncResponse[LibraryEntryOut])
async def sync_library(body: LibrarySyncIn, user: CurrentUser, session: SessionDep) -> Any:
    entries = await SyncService(session).sync_library(user.id, body.items)
    return {"items": await _out(session, user.id, entries), "server_time": datetime.now(UTC)}


@router.put("", response_model=LibraryEntryOut)
async def upsert(body: LibraryEntryIn, user: CurrentUser, session: SessionDep) -> Any:
    entry = await SyncService(session).upsert_library(user.id, body)
    return (await _out(session, user.id, [entry]))[0]


@router.get("/{source_id}/{manga_id}", response_model=LibraryEntryOut)
async def get_entry(source_id: str, manga_id: str, user: CurrentUser, session: SessionDep) -> Any:
    entry = await LibraryRepository(session).get(user.id, source_id, manga_id)
    if entry is None or entry.deleted_at is not None:
        raise NotFoundError("Тайтла нет в библиотеке")
    return (await _out(session, user.id, [entry]))[0]


@router.patch("/{source_id}/{manga_id}", response_model=LibraryEntryOut)
async def patch_entry(source_id: str, manga_id: str, body: LibraryPatchIn, user: CurrentUser,
                      session: SessionDep) -> Any:
    entry = await LibraryRepository(session).get(user.id, source_id, manga_id)
    if entry is None or entry.deleted_at is not None:
        raise NotFoundError("Тайтла нет в библиотеке")
    for field in ("status", "is_favorite", "reader_settings"):
        value = getattr(body, field)
        if value is not None:
            setattr(entry, field, value)
    now = datetime.now(UTC)
    entry.updated_at = body.updated_at or now
    entry.server_updated_at = now
    return (await _out(session, user.id, [entry]))[0]


@router.delete("/{source_id}/{manga_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(source_id: str, manga_id: str, user: CurrentUser, session: SessionDep) -> None:
    entry = await LibraryRepository(session).get(user.id, source_id, manga_id)
    if entry is None:
        raise NotFoundError("Тайтла нет в библиотеке")
    now = datetime.now(UTC)
    entry.deleted_at = now
    entry.updated_at = now
    entry.server_updated_at = now


# ---------------------------------------------------------------- collections
def _col(c: Collection, members: dict[uuid.UUID, list[uuid.UUID]]) -> CollectionOut:
    return CollectionOut(id=c.id, name=c.name, position=c.position, created_at=c.created_at, updated_at=c.updated_at,
                         entry_ids=members.get(c.id, []))


@collections_router.get("", response_model=list[CollectionOut])
async def list_collections(user: CurrentUser, session: SessionDep) -> Any:
    repo = CollectionRepository(session)
    cols = await repo.list(user.id)
    members = await repo.members([c.id for c in cols])
    return [_col(c, members) for c in cols]


@collections_router.post("", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
async def create_collection(body: CollectionIn, user: CurrentUser, session: SessionDep) -> Any:
    repo = CollectionRepository(session)
    if await repo.by_name(user.id, body.name):
        raise ConflictError("Коллекция с таким именем уже есть", code="collection_exists")
    c = Collection(id=uuid.uuid4(), user_id=user.id, name=body.name.strip(), position=body.position)
    session.add(c)
    await session.flush()
    return _col(c, {})


@collections_router.patch("/{collection_id}", response_model=CollectionOut)
async def update_collection(collection_id: uuid.UUID, body: CollectionIn, user: CurrentUser,
                            session: SessionDep) -> Any:
    repo = CollectionRepository(session)
    c = await repo.get(user.id, collection_id)
    if c is None:
        raise NotFoundError("Коллекция не найдена")
    other = await repo.by_name(user.id, body.name)
    if other and other.id != c.id:
        raise ConflictError("Коллекция с таким именем уже есть", code="collection_exists")
    c.name, c.position = body.name.strip(), body.position
    await session.flush()
    return _col(c, await repo.members([c.id]))


@collections_router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(collection_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    c = await CollectionRepository(session).get(user.id, collection_id)
    if c is None:
        raise NotFoundError("Коллекция не найдена")
    await session.delete(c)


@collections_router.post("/{collection_id}/items", response_model=CollectionOut)
async def add_items(collection_id: uuid.UUID, body: CollectionItemsIn, user: CurrentUser, session: SessionDep) -> Any:
    repo = CollectionRepository(session)
    c = await repo.get(user.id, collection_id)
    if c is None:
        raise NotFoundError("Коллекция не найдена")
    await repo.add_items(c, body.entry_ids)
    await session.flush()
    return _col(c, await repo.members([c.id]))


@collections_router.delete("/{collection_id}/items", response_model=CollectionOut)
async def remove_items(collection_id: uuid.UUID, body: CollectionItemsIn, user: CurrentUser,
                       session: SessionDep) -> Any:
    repo = CollectionRepository(session)
    c = await repo.get(user.id, collection_id)
    if c is None:
        raise NotFoundError("Коллекция не найдена")
    await repo.remove_items(c, body.entry_ids)
    await session.flush()
    return _col(c, await repo.members([c.id]))
