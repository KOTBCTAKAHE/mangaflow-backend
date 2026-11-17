"""New-chapter detection for library titles.

Each distinct (source, manga) is fetched once per run regardless of how many
users follow it. The first check only records a baseline (no notification
flood when a title is added).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mangaflow_sources import SourceError, UnknownSourceError

from ..core.logging import get_logger
from ..models import ChapterUpdate, LibraryEntry, TrackedTitle
from .sources import SourceService

log = get_logger(__name__)

KNOWN_LIMIT = 2000
TRACKED_STATUSES = ("reading", "planned", "on_hold")


async def check_updates(maker: async_sessionmaker[AsyncSession], sources: SourceService, *,
                        user_id=None, concurrency: int = 4) -> dict[str, int]:
    async with maker() as s:
        stmt = select(LibraryEntry).where(LibraryEntry.deleted_at.is_(None),
                                          LibraryEntry.status.in_(TRACKED_STATUSES))
        if user_id is not None:
            stmt = stmt.where(LibraryEntry.user_id == user_id)
        entries = list((await s.execute(stmt)).scalars())

    followers: dict[tuple[str, str], list[LibraryEntry]] = defaultdict(list)
    for e in entries:
        followers[(e.source_id, e.manga_id)].append(e)

    sem = asyncio.Semaphore(concurrency)
    stats = {"titles": len(followers), "new_chapters": 0, "errors": 0}

    async def one(key: tuple[str, str]) -> None:
        source_id, manga_id = key
        async with sem:
            try:
                data = await sources.title(source_id, manga_id, refresh=True)
                error = None
            except (SourceError, UnknownSourceError) as exc:
                data, error = None, getattr(exc, "message", str(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("update_check_failed", source=source_id)
                data, error = None, str(exc)
        async with maker() as s:
            tracked = await s.get(TrackedTitle, (source_id, manga_id))
            first = tracked is None
            if tracked is None:
                tracked = TrackedTitle(source_id=source_id, manga_id=manga_id, known_chapter_ids=[])
                s.add(tracked)
            tracked.last_checked_at = datetime.now(UTC)
            tracked.last_error = error
            if data is None:
                stats["errors"] += 1
                await s.commit()
                return
            chapters = data["chapters"]
            known = set(tracked.known_chapter_ids or [])
            new = [c for c in chapters if c["id"] not in known]
            if not first and new:
                for e in followers[key]:
                    for c in new[:50]:
                        exists = (await s.execute(select(ChapterUpdate.id).where(
                            ChapterUpdate.user_id == e.user_id, ChapterUpdate.source_id == source_id,
                            ChapterUpdate.chapter_id == c["id"]))).first()
                        if exists:
                            continue
                        s.add(ChapterUpdate(
                            user_id=e.user_id, source_id=source_id, manga_id=manga_id, manga_title=e.title,
                            cover_url=e.cover_url, chapter_id=c["id"], chapter_title=c["title"],
                            chapter_number=c.get("number")))
                        stats["new_chapters"] += 1
                    le = await s.get(LibraryEntry, e.id)
                    if le is not None:
                        le.total_chapters = len(chapters)
                        le.server_updated_at = datetime.now(UTC)
            tracked.known_chapter_ids = [c["id"] for c in chapters][:KNOWN_LIMIT]
            tracked.chapter_count = len(chapters)
            await s.commit()

    await asyncio.gather(*(one(k) for k in followers))
    log.info("update_check_done", **stats)
    return stats
