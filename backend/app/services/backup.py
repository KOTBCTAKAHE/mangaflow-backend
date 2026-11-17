"""Backup format shared by the server and the mobile app (``mangaflow-backup`` v1).

Import is a merge, not a replace: every record goes through the regular
last-write-wins sync rules, so importing an old backup never clobbers newer data.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from ..repositories.library import CollectionRepository, LibraryRepository
from ..repositories.reading import HistoryRepository, ProgressRepository
from ..schemas.library import LibraryEntryIn
from ..schemas.reading import HistoryIn, ProgressIn
from .sync import SyncService, aware

FORMAT = "mangaflow-backup"
VERSION = 1


class BackupCollection(BaseModel):
    name: str = Field(max_length=128)
    position: int = 0
    entries: list[list[str]] = Field(default_factory=list, description="[[source_id, manga_id], ...]")


class BackupIn(BaseModel):
    format: str = FORMAT
    version: int = VERSION
    settings: dict[str, Any] | None = None
    library: list[LibraryEntryIn] = Field(default_factory=list, max_length=20000)
    collections: list[BackupCollection] = Field(default_factory=list, max_length=500)
    progress: list[ProgressIn] = Field(default_factory=list, max_length=200000)
    history: list[HistoryIn] = Field(default_factory=list, max_length=200000)


def _iso(dt: datetime | None) -> str | None:
    return aware(dt).isoformat() if dt else None


async def export_backup(session: AsyncSession, user: User) -> dict[str, Any]:
    lib = await LibraryRepository(session).list(user.id)
    by_id = {e.id: e for e in lib}
    cols = await CollectionRepository(session).list(user.id)
    members = await CollectionRepository(session).members([c.id for c in cols])
    progress = await ProgressRepository(session).list(user.id)
    history = await HistoryRepository(session).all_active(user.id)
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "user": {"username": user.username, "email": user.email},
        "settings": user.settings or {},
        "library": [{
            "source_id": e.source_id, "manga_id": e.manga_id, "title": e.title, "cover_url": e.cover_url,
            "status": e.status, "is_favorite": e.is_favorite, "reader_settings": e.reader_settings or {},
            "total_chapters": e.total_chapters, "last_chapter_number": e.last_chapter_number,
            "last_read_at": _iso(e.last_read_at), "added_at": _iso(e.added_at), "updated_at": _iso(e.updated_at),
        } for e in lib],
        "collections": [{
            "name": c.name, "position": c.position,
            "entries": [[by_id[eid].source_id, by_id[eid].manga_id] for eid in members.get(c.id, []) if eid in by_id],
        } for c in cols],
        "progress": [{
            "source_id": p.source_id, "manga_id": p.manga_id, "chapter_id": p.chapter_id,
            "chapter_number": p.chapter_number, "chapter_title": p.chapter_title, "page_index": p.page_index,
            "page_count": p.page_count, "completed": p.completed, "updated_at": _iso(p.updated_at),
        } for p in progress],
        "history": [{
            "id": str(h.id), "source_id": h.source_id, "manga_id": h.manga_id, "manga_title": h.manga_title,
            "cover_url": h.cover_url, "chapter_id": h.chapter_id, "chapter_title": h.chapter_title,
            "chapter_number": h.chapter_number, "started_at": _iso(h.started_at), "read_at": _iso(h.read_at),
            "duration_seconds": h.duration_seconds, "pages_read": h.pages_read,
        } for h in history],
    }


async def import_backup(session: AsyncSession, user: User, data: BackupIn) -> dict[str, int]:
    sync = SyncService(session)
    if data.settings is not None and not user.settings:
        user.settings = data.settings
    lib = await sync.sync_library(user.id, data.library)
    by_key = {(e.source_id, e.manga_id): e for e in lib}
    colrepo = CollectionRepository(session)
    from ..models import Collection

    for c in data.collections:
        col = await colrepo.by_name(user.id, c.name)
        if col is None:
            col = Collection(id=uuid.uuid4(), user_id=user.id, name=c.name, position=c.position)
            session.add(col)
            await session.flush()
        ids = [by_key[(s, m)].id for s, m in (tuple(x) for x in c.entries if len(x) == 2) if (s, m) in by_key]
        if ids:
            await colrepo.add_items(col, ids)
    await sync.sync_progress(user.id, data.progress)
    added = await sync.add_history(user.id, data.history)
    await session.flush()
    return {"library": len(data.library), "collections": len(data.collections),
            "progress": len(data.progress), "history": added}
