from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChapterUpdate, DeviceDownload, HistoryEntry, ReadingProgress, SearchHistory


class ProgressRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, user_id: uuid.UUID, source_id: str, manga_id: str, chapter_id: str) -> ReadingProgress | None:
        stmt = select(ReadingProgress).where(
            ReadingProgress.user_id == user_id, ReadingProgress.source_id == source_id,
            ReadingProgress.manga_id == manga_id, ReadingProgress.chapter_id == chapter_id)
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def list(self, user_id: uuid.UUID, *, since: datetime | None = None, source_id: str | None = None,
                   manga_id: str | None = None) -> list[ReadingProgress]:
        stmt = select(ReadingProgress).where(ReadingProgress.user_id == user_id)
        if since is not None:
            stmt = stmt.where(ReadingProgress.server_updated_at > since)
        if source_id:
            stmt = stmt.where(ReadingProgress.source_id == source_id)
        if manga_id:
            stmt = stmt.where(ReadingProgress.manga_id == manga_id)
        return list((await self.s.execute(stmt.order_by(ReadingProgress.updated_at.desc()))).scalars())

    async def latest_per_manga(self, user_id: uuid.UUID, limit: int = 20) -> list[ReadingProgress]:
        ranked = (
            select(ReadingProgress.id,
                   func.row_number().over(partition_by=(ReadingProgress.source_id, ReadingProgress.manga_id),
                                          order_by=ReadingProgress.updated_at.desc()).label("rn"))
            .where(ReadingProgress.user_id == user_id)
            .subquery()
        )
        stmt = (select(ReadingProgress).join(ranked, ranked.c.id == ReadingProgress.id)
                .where(ranked.c.rn == 1).order_by(ReadingProgress.updated_at.desc()).limit(limit))
        return list((await self.s.execute(stmt)).scalars())

    def add(self, p: ReadingProgress) -> None:
        self.s.add(p)


class HistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, entry_id: uuid.UUID) -> HistoryEntry | None:
        return await self.s.get(HistoryEntry, entry_id)

    async def list(self, user_id: uuid.UUID, *, since: datetime | None = None, limit: int = 50,
                   offset: int = 0) -> list[HistoryEntry]:
        stmt = select(HistoryEntry).where(HistoryEntry.user_id == user_id)
        if since is not None:
            stmt = stmt.where(HistoryEntry.server_updated_at > since)
        else:
            stmt = stmt.where(HistoryEntry.deleted_at.is_(None))
        stmt = stmt.order_by(HistoryEntry.read_at.desc()).offset(offset).limit(limit)
        return list((await self.s.execute(stmt)).scalars())

    async def all_active(self, user_id: uuid.UUID) -> list[HistoryEntry]:
        stmt = select(HistoryEntry).where(HistoryEntry.user_id == user_id, HistoryEntry.deleted_at.is_(None))
        return list((await self.s.execute(stmt)).scalars())

    def add(self, h: HistoryEntry) -> None:
        self.s.add(h)

    async def soft_delete(self, user_id: uuid.UUID, now: datetime, *, entry_id: uuid.UUID | None = None,
                          source_id: str | None = None, manga_id: str | None = None) -> int:
        stmt = update(HistoryEntry).where(HistoryEntry.user_id == user_id, HistoryEntry.deleted_at.is_(None))
        if entry_id:
            stmt = stmt.where(HistoryEntry.id == entry_id)
        if source_id and manga_id:
            stmt = stmt.where(HistoryEntry.source_id == source_id, HistoryEntry.manga_id == manga_id)
        res = await self.s.execute(stmt.values(deleted_at=now, server_updated_at=now))
        return res.rowcount or 0


class DownloadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def list(self, user_id: uuid.UUID, device_id: str | None = None) -> list[DeviceDownload]:
        stmt = select(DeviceDownload).where(DeviceDownload.user_id == user_id, DeviceDownload.status != "deleted")
        if device_id:
            stmt = stmt.where(DeviceDownload.device_id == device_id)
        return list((await self.s.execute(stmt.order_by(DeviceDownload.updated_at.desc()))).scalars())

    async def get(self, user_id: uuid.UUID, device_id: str, source_id: str, chapter_id: str) -> DeviceDownload | None:
        stmt = select(DeviceDownload).where(
            DeviceDownload.user_id == user_id, DeviceDownload.device_id == device_id,
            DeviceDownload.source_id == source_id, DeviceDownload.chapter_id == chapter_id)
        return (await self.s.execute(stmt)).scalar_one_or_none()

    def add(self, d: DeviceDownload) -> None:
        self.s.add(d)

    async def delete_device(self, user_id: uuid.UUID, device_id: str) -> None:
        await self.s.execute(delete(DeviceDownload).where(DeviceDownload.user_id == user_id,
                                                          DeviceDownload.device_id == device_id))


class UpdateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def list(self, user_id: uuid.UUID, *, unseen_only: bool = False, limit: int = 100,
                   since: datetime | None = None) -> list[ChapterUpdate]:
        stmt = select(ChapterUpdate).where(ChapterUpdate.user_id == user_id)
        if unseen_only:
            stmt = stmt.where(ChapterUpdate.seen.is_(False))
        if since:
            stmt = stmt.where(ChapterUpdate.detected_at > since)
        return list((await self.s.execute(stmt.order_by(ChapterUpdate.detected_at.desc()).limit(limit))).scalars())

    async def mark_seen(self, user_id: uuid.UUID, ids: list[uuid.UUID] | None) -> int:
        stmt = update(ChapterUpdate).where(ChapterUpdate.user_id == user_id, ChapterUpdate.seen.is_(False))
        if ids is not None:
            stmt = stmt.where(ChapterUpdate.id.in_(ids))
        return (await self.s.execute(stmt.values(seen=True))).rowcount or 0

    async def mark_manga_seen(self, user_id: uuid.UUID, source_id: str, manga_id: str) -> None:
        await self.s.execute(update(ChapterUpdate).where(
            ChapterUpdate.user_id == user_id, ChapterUpdate.source_id == source_id,
            ChapterUpdate.manga_id == manga_id).values(seen=True))

    async def exists(self, user_id: uuid.UUID, source_id: str, chapter_id: str) -> bool:
        stmt = select(ChapterUpdate.id).where(ChapterUpdate.user_id == user_id, ChapterUpdate.source_id == source_id,
                                              ChapterUpdate.chapter_id == chapter_id)
        return (await self.s.execute(stmt)).first() is not None


class SearchHistoryRepository:
    KEEP = 50

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(self, user_id: uuid.UUID, query: str) -> None:
        q = query.strip()[:256]
        if not q:
            return
        await self.s.execute(delete(SearchHistory).where(SearchHistory.user_id == user_id,
                                                         func.lower(SearchHistory.query) == q.lower()))
        self.s.add(SearchHistory(user_id=user_id, query=q))
        await self.s.flush()
        old = select(SearchHistory.id).where(SearchHistory.user_id == user_id).order_by(
            SearchHistory.created_at.desc()).offset(self.KEEP)
        await self.s.execute(delete(SearchHistory).where(SearchHistory.id.in_(old)))

    async def list(self, user_id: uuid.UUID, limit: int = 20) -> list[SearchHistory]:
        stmt = select(SearchHistory).where(SearchHistory.user_id == user_id).order_by(
            SearchHistory.created_at.desc()).limit(limit)
        return list((await self.s.execute(stmt)).scalars())

    async def clear(self, user_id: uuid.UUID, query: str | None = None) -> None:
        stmt = delete(SearchHistory).where(SearchHistory.user_id == user_id)
        if query:
            stmt = stmt.where(func.lower(SearchHistory.query) == query.lower())
        await self.s.execute(stmt)
