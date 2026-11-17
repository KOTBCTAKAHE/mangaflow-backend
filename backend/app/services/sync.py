"""Offline-first sync rules.

Clients keep a local database and push batches. Conflicts are resolved with
last-write-wins on the *client* ``updated_at``. Deletions are tombstones
(``deleted_at``) so other devices learn about them through ``?since=``.
Progress is monotonic per chapter: a ``completed`` chapter stays completed.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DeviceDownload, HistoryEntry, LibraryEntry, ReadingProgress
from ..repositories.library import LibraryRepository
from ..repositories.reading import DownloadRepository, HistoryRepository, ProgressRepository, UpdateRepository
from ..schemas.library import LibraryEntryIn
from ..schemas.reading import DownloadIn, HistoryIn, ProgressIn


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class SyncService:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session
        self.library = LibraryRepository(session)
        self.progress = ProgressRepository(session)
        self.history = HistoryRepository(session)
        self.downloads = DownloadRepository(session)
        self.updates = UpdateRepository(session)

    # ------------------------------------------------------------ library
    async def upsert_library(self, user_id: uuid.UUID, item: LibraryEntryIn) -> LibraryEntry:
        now = datetime.now(UTC)
        entry = await self.library.get(user_id, item.source_id, item.manga_id)
        if entry is not None and aware(entry.updated_at) >= item.updated_at:
            return entry  # server copy is newer
        if entry is None:
            entry = LibraryEntry(user_id=user_id, source_id=item.source_id, manga_id=item.manga_id,
                                 added_at=item.added_at or now)
            self.s.add(entry)
        entry.title = item.title
        entry.cover_url = item.cover_url or entry.cover_url or ""
        entry.status = item.status
        entry.is_favorite = item.is_favorite
        entry.reader_settings = item.reader_settings
        entry.total_chapters = item.total_chapters
        entry.last_chapter_number = item.last_chapter_number
        entry.last_read_at = item.last_read_at or entry.last_read_at
        entry.updated_at = item.updated_at
        entry.server_updated_at = now
        entry.deleted_at = now if item.deleted else None
        await self.s.flush()
        if item.collection_ids is not None:
            await self.library.set_collections(user_id, entry, item.collection_ids)
        return entry

    async def sync_library(self, user_id: uuid.UUID, items: list[LibraryEntryIn]) -> list[LibraryEntry]:
        return [await self.upsert_library(user_id, it) for it in items]

    # ------------------------------------------------------------ progress
    async def upsert_progress(self, user_id: uuid.UUID, item: ProgressIn) -> ReadingProgress:
        now = datetime.now(UTC)
        p = await self.progress.get(user_id, item.source_id, item.manga_id, item.chapter_id)
        if p is None:
            p = ReadingProgress(user_id=user_id, source_id=item.source_id, manga_id=item.manga_id,
                                chapter_id=item.chapter_id)
            self.progress.add(p)
        elif aware(p.updated_at) >= item.updated_at:
            if item.completed and not p.completed:
                p.completed = True
                p.server_updated_at = now
            return p
        was_completed = bool(p.completed)
        p.chapter_number = item.chapter_number
        p.chapter_title = item.chapter_title
        p.page_index = item.page_index
        p.page_count = item.page_count
        p.completed = item.completed or was_completed
        p.updated_at = item.updated_at
        p.server_updated_at = now
        # Reading a chapter marks its "new chapter" notification as seen and bumps the library entry.
        entry = await self.library.get(user_id, item.source_id, item.manga_id)
        if entry is not None and entry.deleted_at is None:
            if entry.last_read_at is None or aware(entry.last_read_at) < item.updated_at:
                entry.last_read_at = item.updated_at
                entry.server_updated_at = now
            if item.completed and item.chapter_number is not None and (
                    entry.last_chapter_number is None or item.chapter_number > entry.last_chapter_number):
                entry.last_chapter_number = item.chapter_number
            if entry.status == "planned":
                entry.status = "reading"
        await self.s.flush()
        return p

    async def sync_progress(self, user_id: uuid.UUID, items: list[ProgressIn]) -> list[ReadingProgress]:
        return [await self.upsert_progress(user_id, it) for it in items]

    # ------------------------------------------------------------ history
    async def add_history(self, user_id: uuid.UUID, items: list[HistoryIn]) -> int:
        now = datetime.now(UTC)
        added = 0
        for it in items:
            existing = await self.history.get(it.id)
            if existing is not None and existing.user_id != user_id:
                # same session id owned by another account (backup restored elsewhere): derive a stable new id
                it = it.model_copy(update={"id": uuid.uuid5(user_id, str(it.id))})
                existing = await self.history.get(it.id)
            if existing is not None:
                # session continued on the client: keep the longest version
                existing.read_at = max(aware(existing.read_at), it.read_at)
                existing.duration_seconds = max(existing.duration_seconds, it.duration_seconds)
                existing.pages_read = max(existing.pages_read, it.pages_read)
                existing.server_updated_at = now
                continue
            self.history.add(HistoryEntry(user_id=user_id, server_updated_at=now, **it.model_dump()))
            added += 1
        await self.s.flush()
        return added

    # ------------------------------------------------------------ downloads
    async def upsert_downloads(self, user_id: uuid.UUID, items: list[DownloadIn]) -> list[DeviceDownload]:
        out = []
        for it in items:
            d = await self.downloads.get(user_id, it.device_id, it.source_id, it.chapter_id)
            if d is None:
                d = DeviceDownload(user_id=user_id, **it.model_dump())
                self.downloads.add(d)
            else:
                for k, v in it.model_dump().items():
                    setattr(d, k, v)
                d.updated_at = datetime.now(UTC)
            out.append(d)
        await self.s.flush()
        return out
