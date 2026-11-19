"""Plot search, similar titles and the «Для вас» feed."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import select

from ...core.errors import AppError
from ...core.logging import get_logger
from ...core.ratelimit import RateLimit
from ...models import LibraryEntry, Rating
from ...schemas.common import Schema
from ...services.plot import PlotClient
from ...services.recs import CoReading
from ...services.search import normalize_title
from ..deps import OptionalUser, PresenterDep, SessionDep, SourcesDep
from .social import _friend_ids

log = get_logger(__name__)
router = APIRouter(tags=["discover"], dependencies=[Depends(RateLimit("catalog"))])

MIN_PLOT_SIMILARITY = 76.0  # e5 cosine ×100: unrelated plots sit around 70–75


class SeedIn(Schema):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    title: str = Field(default="", max_length=512)
    weight: float = Field(default=1.0, ge=-5, le=10)


class ForYouIn(Schema):
    seeds: list[SeedIn] = Field(default_factory=list, max_length=80)
    # whole library (also planned / dropped) — never recommended back
    known: list[str] = Field(default_factory=list, max_length=2000)
    include_nsfw: bool = False


class _Cards:
    """Builds the grouped cards the app already parses for search results."""

    def __init__(self, request: Request, pres, include_nsfw: bool) -> None:
        self.infos = {i.id: i for i in request.app.state.sources.registry.infos()}
        self.pres = pres
        self.include_nsfw = include_nsfw

    def allowed(self, sid: str) -> bool:
        info = self.infos.get(sid)
        return info is not None and (self.include_nsfw or not info.is_nsfw)

    def cover(self, sid: str, url: str) -> str:
        if not url or "/api/v1/images/" in url:
            return url  # already a signed link (stored by the app; covers are signed for a year)
        return self.pres.signer.url(sid, url, cover=True)

    def group(self, cards: list[dict[str, Any]], **extra: Any) -> dict[str, Any] | None:
        srcs = [c for c in cards if self.allowed(c["source_id"])]
        if not srcs:
            return None
        out = [{"source_id": c["source_id"], "id": c["manga_id"], "title": c["title"],
                "cover_url": self.cover(c["source_id"], c.get("cover_url") or ""),
                "source_name": self.infos[c["source_id"]].name} for c in srcs]
        first = out[0]
        cover = next((c["cover_url"] for c in out if c["cover_url"]), "")
        return {"key": f"{first['source_id']}:{first['id']}", "title": first["title"], "cover_url": cover,
                "source_id": first["source_id"], "id": first["id"], "source_name": first["source_name"],
                "type": None, "score": 1.0, "sources": out, **extra}


def _plots(request: Request) -> PlotClient:
    return request.app.state.plots


async def _safe(coro, default):
    try:
        return await coro
    except AppError as exc:
        log.info("discover_part_failed", error=exc.message)
        return default


@router.get("/search/plot", summary="Поиск по описанию сюжета")
async def plot_search(request: Request, pres: PresenterDep,
                      q: str = Query(min_length=3, max_length=500), limit: int = Query(20, ge=1, le=40),
                      include_nsfw: bool = False) -> Any:
    data = await _plots(request).search(q, limit)
    cards = _Cards(request, pres, include_nsfw)
    items = []
    for h in data.get("hits") or []:
        g = cards.group(h["sources"], similarity=h["similarity"], snippet=h.get("description") or "",
                        genres=h.get("genres") or [])
        if g:
            items.append(g)
    return {"query": q, "page": 1, "has_next": False, "items": items, "errors": [],
            "indexed": int(data.get("plots") or 0)}


@router.get("/manga/{source_id}/{manga_id}/similar", summary="Похожие тайтлы: по сюжету и по читателям")
async def similar(source_id: str, manga_id: str, request: Request, pres: PresenterDep, session: SessionDep,
                  svc: SourcesDep, title: str = Query("", max_length=512), include_nsfw: bool = False,
                  limit: int = Query(15, ge=1, le=30)) -> Any:
    svc.adapter(source_id)  # unknown source → 404
    cards = _Cards(request, pres, include_nsfw)
    co: CoReading = request.app.state.coreading
    await co.ensure(session)
    norm = normalize_title(title)
    plot_task = _safe(_plots(request).similar([{"source_id": source_id, "manga_id": manga_id, "title": title}],
                                              limit + 5), {"hits": []})
    plot = await plot_task
    norm = norm or (plot.get("seeds") or [""])[0]
    items: list[dict[str, Any]] = []
    seen = {norm} if norm else set()
    together = co.similar({norm: 1.0}, limit) if norm else []
    plot_hits = [h for h in plot.get("hits") or [] if h["similarity"] >= MIN_PLOT_SIMILARITY]
    # interleave: readers' picks and plot neighbours
    for i in range(max(len(together), len(plot_hits))):
        if i < len(together):
            n, _, users = together[i]
            c = co.cards.get(n)
            if c and n not in seen and (g := cards.group([c], reason=f"Читают вместе: {users}")):
                items.append(g)
                seen.add(n)
        if i < len(plot_hits):
            h = plot_hits[i]
            if h["norm"] not in seen and (g := cards.group(h["sources"], reason="Похожий сюжет")):
                items.append(g)
                seen.add(h["norm"])
    return {"items": items[:limit]}


async def _server_seeds(session, user) -> tuple[list[dict[str, Any]], list[str]]:
    rows = (await session.execute(select(LibraryEntry).where(
        LibraryEntry.user_id == user.id, LibraryEntry.deleted_at.is_(None)).order_by(
        LibraryEntry.last_read_at.desc().nulls_last()).limit(500))).scalars().all()
    seeds = [{"source_id": e.source_id, "manga_id": e.manga_id, "title": e.title,
              "weight": 3.0 if e.is_favorite else 2.0 if e.status in ("reading", "completed") else 0.0}
             for e in rows]
    return [s for s in seeds if s["weight"] > 0][:40], [e.title for e in rows]


@router.post("/discover/for-you", summary="Лента «Для вас»")
async def for_you(body: ForYouIn, request: Request, pres: PresenterDep, session: SessionDep, user: OptionalUser) -> Any:
    cards = _Cards(request, pres, body.include_nsfw)
    co: CoReading = request.app.state.coreading
    await co.ensure(session)
    seeds = [s.model_dump() for s in body.seeds if s.weight > 0]
    known_titles = list(body.known)
    if user is not None and not seeds:
        seeds, known_titles = await _server_seeds(session, user)
    known = {normalize_title(t) for t in known_titles + [s["title"] for s in seeds] if t}
    known.discard("")
    shown: set[str] = set(known)
    sections: list[dict[str, Any]] = []

    def add(key: str, title: str, pairs: list[tuple[str, dict[str, Any] | None]], min_items: int = 3) -> None:
        items, taken = [], []
        for norm, g in pairs:
            if g is None or norm in shown:
                continue
            shown.add(norm)
            taken.append(norm)
            items.append(g)
            if len(items) >= 20:
                break
        if len(items) >= min_items:
            sections.append({"key": key, "title": title, "items": items})
        else:
            shown.difference_update(taken)  # too few for a row: later rows may use them

    plots = _plots(request)
    recent = seeds[:2]
    profile, *because = await asyncio.gather(
        _safe(plots.similar(seeds[:40], 30, list(known)[:500]), {"hits": []}) if seeds else asyncio.sleep(0, {"hits": []}),
        *[_safe(plots.similar([s], 12, list(known)[:500]), {"hits": []}) for s in recent])

    def plot_pairs(res: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
        return [(h["norm"], cards.group(h["sources"], reason="Похожий сюжет"))
                for h in res.get("hits") or [] if h["similarity"] >= MIN_PLOT_SIMILARITY]

    add("for_you", "Подобрано для вас", plot_pairs(profile))
    for s, res in zip(recent, because, strict=True):
        if s.get("title"):
            add(f"because:{s['source_id']}:{s['manga_id']}", f"Потому что вы читали «{s['title']}»", plot_pairs(res))

    seed_w = {normalize_title(s["title"]): float(s["weight"]) for s in seeds if s.get("title")}
    together = co.similar(seed_w, 30, known, me=user.id if user else None)
    add("together", "Читают вместе с вашими",
        [(n, cards.group([co.cards[n]], reason=f"У {u} читателей с похожей библиотекой")) for n, _, u in together
         if n in co.cards])

    if user is not None:
        friends = await _friend_ids(session, user.id)
        if friends:
            since = datetime.now(UTC) - timedelta(days=120)
            rows = (await session.execute(select(Rating).where(
                Rating.user_id.in_(friends), Rating.score >= 8, Rating.updated_at >= since)
                .order_by(Rating.score.desc(), Rating.updated_at.desc()).limit(60))).scalars().all()
            add("friends", "Высоко оценили друзья",
                [(normalize_title(r.manga_title), cards.group(
                    [{"source_id": r.source_id, "manga_id": r.manga_id, "title": r.manga_title, "cover_url": r.cover_url}],
                    reason=f"Оценка друга: {r.score}/10")) for r in rows], min_items=1)

    add("popular", "Популярно в MangaFlow на этой неделе",
        [(n, cards.group([co.cards[n]], reason=f"Читали {u} человек")) for n, u in co.popular if n in co.cards])
    return {"sections": sections, "seeds": len(seeds)}
