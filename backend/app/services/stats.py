"""Reading statistics derived from history sessions."""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import LibraryEntry, ReadingProgress
from ..repositories.reading import HistoryRepository


def streaks(days: set[date], today: date) -> tuple[int, int]:
    if not days:
        return 0, 0
    longest = run = 0
    prev: date | None = None
    for d in sorted(days):
        run = run + 1 if prev and d - prev == timedelta(days=1) else 1
        longest = max(longest, run)
        prev = d
    current = 0
    cursor = today if today in days else today - timedelta(days=1)
    while cursor in days:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


async def compute_stats(session: AsyncSession, user_id: uuid.UUID, *, days: int = 365) -> dict:
    history = await HistoryRepository(session).all_active(user_id)
    completed = (await session.execute(
        select(func.count()).select_from(ReadingProgress).where(ReadingProgress.user_id == user_id,
                                                                ReadingProgress.completed.is_(True))
    )).scalar_one()
    by_status = dict((await session.execute(
        select(LibraryEntry.status, func.count()).where(LibraryEntry.user_id == user_id,
                                                         LibraryEntry.deleted_at.is_(None))
        .group_by(LibraryEntry.status)
    )).all())

    today = datetime.now(UTC).date()
    since = today - timedelta(days=days - 1)
    per_day_chapters: dict[date, set[str]] = defaultdict(set)
    per_day_seconds: Counter[date] = Counter()
    titles: set[tuple[str, str]] = set()
    total_seconds = 0
    for h in history:
        read_at = h.read_at if h.read_at.tzinfo else h.read_at.replace(tzinfo=UTC)
        d = read_at.date()
        per_day_chapters[d].add(f"{h.source_id}:{h.chapter_id}")
        per_day_seconds[d] += h.duration_seconds
        titles.add((h.source_id, h.manga_id))
        total_seconds += h.duration_seconds

    current, longest = streaks(set(per_day_chapters), today)
    activity = [
        {"day": d, "chapters": len(per_day_chapters[d]), "seconds": per_day_seconds[d]}
        for d in sorted(per_day_chapters) if d >= since
    ]
    return {
        "chapters_read": max(int(completed), len({c for s in per_day_chapters.values() for c in s})),
        "titles_read": len(titles),
        "total_seconds": total_seconds,
        "library_total": sum(by_status.values()),
        "by_status": by_status,
        "current_streak_days": current,
        "longest_streak_days": longest,
        "activity": activity,
    }
