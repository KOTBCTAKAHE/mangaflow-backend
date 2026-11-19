"""«Для вас»: who reads what together (MangaFlow's own readers) + what is popular this week.

Titles are keyed by normalized name, so readers on different sources still count as
reading the same manga. Nothing about a single reader is exposed: a co-reading pair
needs at least two readers behind it, a popular title at least two readers.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..models import HistoryEntry, LibraryEntry, Rating
from .search import normalize_title

STATUS_WEIGHT = {"reading": 2.0, "completed": 2.5, "on_hold": 1.0, "planned": 0.6, "dropped": -1.0}
MAX_ITEMS_PER_USER = 80
MIN_SUPPORT = 2


class CoReading:
    ttl = 1800.0

    def __init__(self) -> None:
        self.built_at = -1e9
        self._lock = asyncio.Lock()
        self.user_items: dict[uuid.UUID, dict[str, float]] = {}
        self.item_users: dict[str, dict[uuid.UUID, float]] = {}
        self.cards: dict[str, dict[str, Any]] = {}
        self.popular: list[tuple[str, int]] = []

    async def ensure(self, session) -> None:
        if time.monotonic() - self.built_at < self.ttl:
            return
        async with self._lock:
            if time.monotonic() - self.built_at < self.ttl:
                return
            await self._build(session)
            self.built_at = time.monotonic()

    async def _build(self, session) -> None:
        votes: dict[str, Counter] = defaultdict(Counter)
        items: dict[uuid.UUID, dict[str, float]] = defaultdict(dict)

        def seen(norm: str, sid: str, mid: str, title: str, cover: str) -> None:
            votes[norm][(sid, mid, title, cover)] += 1

        lib = (await session.execute(
            select(LibraryEntry.user_id, LibraryEntry.source_id, LibraryEntry.manga_id, LibraryEntry.title,
                   LibraryEntry.cover_url, LibraryEntry.status, LibraryEntry.is_favorite)
            .where(LibraryEntry.deleted_at.is_(None)).order_by(LibraryEntry.updated_at.desc()).limit(300_000))).all()
        for uid, sid, mid, title, cover, status, fav in lib:
            norm = normalize_title(title or "")
            if not norm:
                continue
            w = STATUS_WEIGHT.get(status or "", 1.0) + (1.5 if fav else 0.0)
            items[uid][norm] = max(items[uid].get(norm, -9), w)
            seen(norm, sid, mid, title, cover or "")
        rat = (await session.execute(
            select(Rating.user_id, Rating.source_id, Rating.manga_id, Rating.manga_title, Rating.cover_url, Rating.score)
            .order_by(Rating.updated_at.desc()).limit(200_000))).all()
        for uid, sid, mid, title, cover, score in rat:
            norm = normalize_title(title or "")
            if not norm:
                continue
            items[uid][norm] = items[uid].get(norm, 1.0) + (score - 5.5) / 2
            seen(norm, sid, mid, title, cover or "")
        since = datetime.now(UTC) - timedelta(days=7)
        hist = (await session.execute(
            select(HistoryEntry.user_id, HistoryEntry.source_id, HistoryEntry.manga_id, HistoryEntry.manga_title,
                   HistoryEntry.cover_url).where(HistoryEntry.read_at >= since).limit(200_000))).all()
        week: dict[str, set[uuid.UUID]] = defaultdict(set)
        for uid, sid, mid, title, cover in hist:
            norm = normalize_title(title or "")
            if not norm:
                continue
            week[norm].add(uid)
            seen(norm, sid, mid, title, cover or "")
            items[uid].setdefault(norm, 1.5)

        user_items: dict[uuid.UUID, dict[str, float]] = {}
        item_users: dict[str, dict[uuid.UUID, float]] = defaultdict(dict)
        for uid, its in items.items():
            top = dict(sorted(its.items(), key=lambda kv: abs(kv[1]), reverse=True)[:MAX_ITEMS_PER_USER])
            user_items[uid] = top
            for norm, w in top.items():
                if w > 0:
                    item_users[norm][uid] = w
        self.user_items = user_items
        self.item_users = dict(item_users)
        self.cards = {}
        for norm, c in votes.items():
            (sid, mid, title, cover), _ = c.most_common(1)[0]
            self.cards[norm] = {"source_id": sid, "manga_id": mid, "title": title, "cover_url": cover}
        self.popular = sorted(((n, len(u)) for n, u in week.items() if len(u) >= MIN_SUPPORT),
                              key=lambda x: x[1], reverse=True)[:60]

    def similar(self, seeds: dict[str, float], limit: int = 20, exclude: set[str] | None = None,
                me: uuid.UUID | None = None) -> list[tuple[str, float, int]]:
        """Item-to-item co-reading: titles that readers of the seeds also have, cosine-normalized.
        The asking reader never counts as their own evidence."""
        exclude = exclude or set()
        score: dict[str, float] = defaultdict(float)
        support: dict[str, set[uuid.UUID]] = defaultdict(set)
        for s, ws in seeds.items():
            users = self.item_users.get(s)
            if not users or ws <= 0:
                continue
            for uid, w_us in users.items():
                if uid == me:
                    continue
                for j, w_uj in self.user_items.get(uid, {}).items():
                    if w_uj <= 0 or j in seeds or j in exclude:
                        continue
                    score[j] += ws * w_us * w_uj / math.sqrt(len(users) * max(1, len(self.item_users.get(j, {}))))
                    support[j].add(uid)
        out = [(j, sc, len(support[j])) for j, sc in score.items() if len(support[j]) >= MIN_SUPPORT]
        out.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return out[:limit]
