from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChapterUpdate, Collection, CollectionItem, LibraryEntry


class LibraryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, user_id: uuid.UUID, source_id: str, manga_id: str) -> LibraryEntry | None:
        stmt = select(LibraryEntry).where(
            LibraryEntry.user_id == user_id, LibraryEntry.source_id == source_id, LibraryEntry.manga_id == manga_id
        )
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, user_id: uuid.UUID, entry_id: uuid.UUID) -> LibraryEntry | None:
        e = await self.s.get(LibraryEntry, entry_id)
        return e if e and e.user_id == user_id else None

    async def list(
        self, user_id: uuid.UUID, *, since: datetime | None = None, status: str | None = None,
        favorite: bool | None = None, collection_id: uuid.UUID | None = None, q: str | None = None,
        sort: str = "updated", include_deleted: bool = False,
    ) -> list[LibraryEntry]:
        stmt = select(LibraryEntry).where(LibraryEntry.user_id == user_id)
        if since is not None:
            stmt = stmt.where(LibraryEntry.server_updated_at > since)
        elif not include_deleted:
            stmt = stmt.where(LibraryEntry.deleted_at.is_(None))
        if status:
            stmt = stmt.where(LibraryEntry.status == status)
        if favorite is not None:
            stmt = stmt.where(LibraryEntry.is_favorite.is_(favorite))
        if q:
            stmt = stmt.where(func.lower(LibraryEntry.title).contains(q.lower()))
        if collection_id:
            stmt = stmt.join(CollectionItem, CollectionItem.entry_id == LibraryEntry.id).where(
                CollectionItem.collection_id == collection_id)
        order = {
            "updated": LibraryEntry.updated_at.desc(),
            "added": LibraryEntry.added_at.desc(),
            "title": func.lower(LibraryEntry.title).asc(),
            "last_read": LibraryEntry.last_read_at.desc().nulls_last(),
        }.get(sort, LibraryEntry.updated_at.desc())
        return list((await self.s.execute(stmt.order_by(order))).scalars())

    async def add(self, entry: LibraryEntry) -> LibraryEntry:
        self.s.add(entry)
        await self.s.flush()
        return entry

    async def collections_for(self, entry_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
        if not entry_ids:
            return {}
        rows = await self.s.execute(
            select(CollectionItem.entry_id, CollectionItem.collection_id).where(CollectionItem.entry_id.in_(entry_ids))
        )
        out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for eid, cid in rows:
            out[eid].append(cid)
        return out

    async def unread_counts(self, user_id: uuid.UUID) -> dict[tuple[str, str], int]:
        rows = await self.s.execute(
            select(ChapterUpdate.source_id, ChapterUpdate.manga_id, func.count())
            .where(ChapterUpdate.user_id == user_id, ChapterUpdate.seen.is_(False))
            .group_by(ChapterUpdate.source_id, ChapterUpdate.manga_id)
        )
        return {(s, m): c for s, m, c in rows}

    async def set_collections(self, user_id: uuid.UUID, entry: LibraryEntry, collection_ids: list[uuid.UUID]) -> None:
        valid = set((await self.s.execute(
            select(Collection.id).where(Collection.user_id == user_id, Collection.id.in_(collection_ids))
        )).scalars()) if collection_ids else set()
        await self.s.execute(delete(CollectionItem).where(CollectionItem.entry_id == entry.id))
        for cid in valid:
            self.s.add(CollectionItem(collection_id=cid, entry_id=entry.id))


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def list(self, user_id: uuid.UUID) -> list[Collection]:
        stmt = select(Collection).where(Collection.user_id == user_id).order_by(Collection.position, Collection.name)
        return list((await self.s.execute(stmt)).scalars())

    async def get(self, user_id: uuid.UUID, cid: uuid.UUID) -> Collection | None:
        c = await self.s.get(Collection, cid)
        return c if c and c.user_id == user_id else None

    async def by_name(self, user_id: uuid.UUID, name: str) -> Collection | None:
        stmt = select(Collection).where(Collection.user_id == user_id, func.lower(Collection.name) == name.lower())
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def members(self, collection_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
        if not collection_ids:
            return {}
        rows = await self.s.execute(select(CollectionItem.collection_id, CollectionItem.entry_id)
                                    .where(CollectionItem.collection_id.in_(collection_ids)))
        out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for cid, eid in rows:
            out[cid].append(eid)
        return out

    async def add_items(self, collection: Collection, entry_ids: list[uuid.UUID]) -> None:
        existing = set((await self.s.execute(
            select(CollectionItem.entry_id).where(CollectionItem.collection_id == collection.id)
        )).scalars())
        owned = set((await self.s.execute(
            select(LibraryEntry.id).where(LibraryEntry.user_id == collection.user_id, LibraryEntry.id.in_(entry_ids))
        )).scalars())
        for eid in owned - existing:
            self.s.add(CollectionItem(collection_id=collection.id, entry_id=eid))

    async def remove_items(self, collection: Collection, entry_ids: list[uuid.UUID]) -> None:
        await self.s.execute(delete(CollectionItem).where(
            CollectionItem.collection_id == collection.id, CollectionItem.entry_id.in_(entry_ids)))
