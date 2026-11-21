"""Desu (public JSON API). Port of sources/ru/manga/desu/desu.js (Apache-2.0)."""
from __future__ import annotations

from typing import Any

from ..base import SourceAdapter
from ..errors import SourceParseError
from ..models import (
    Chapter,
    MangaDetails,
    MangaListPage,
    MangaSummary,
    MangaType,
    PageImage,
    PublicationStatus,
    SearchQuery,
    SortOrder,
    SourceCapabilities,
    SourceInfo,
)
from ..utils import chapter_title, from_unix, parse_float, strip_html

_SORT = {SortOrder.POPULAR: "popular", SortOrder.RATING: "popular", SortOrder.UPDATED: "updated",
         SortOrder.CREATED: "id", SortOrder.NAME: "name"}
_KINDS = {MangaType.MANGA: "manga", MangaType.MANHWA: "manhwa", MangaType.MANHUA: "manhua"}
_KIND_BACK = {v: k for k, v in _KINDS.items()}
_STATUS = {"ongoing": PublicationStatus.ONGOING, "released": PublicationStatus.COMPLETED,
           "copyrighted": PublicationStatus.CANCELLED, "anons": PublicationStatus.ANNOUNCED}
_STATUS_FWD = {PublicationStatus.ONGOING: "ongoing", PublicationStatus.COMPLETED: "released",
               PublicationStatus.ANNOUNCED: "anons"}


class DesuAdapter(SourceAdapter):
    info = SourceInfo(
        id="desu",
        name="Desu",
        base_url="https://desu.uno",
        capabilities=SourceCapabilities(
            search=True, type_filter=True, status_filter=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.NAME),
        ),
        notes="Публичный API /api/manga (старый /manga/api отключён). Домен менялся: desu.me → desu.city → desu.uno.",
    )

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Referer": (self.config.base_url or self.info.base_url) + "/"}

    @property
    def api(self) -> str:
        # the old /manga/api answers 410 "api_deprecated" since late 2026
        return f"{self.base_url}/api/manga"

    @staticmethod
    def _cover(m: dict[str, Any]) -> str:
        c = m.get("cover") or m.get("image") or {}
        return c.get("original") or c.get("preview") or c.get("snippet") or c.get("x120") or ""

    def _parse_list(self, j: dict[str, Any]) -> MangaListPage:
        items = []
        for m in j.get("mangas") or []:
            items.append(MangaSummary(
                id=str(m.get("id")),
                title=m.get("russian") or m.get("name") or "",
                alt_titles=[x for x in (m.get("name"),) if isinstance(x, str) and x],
                cover_url=self._cover(m),
                type=_KIND_BACK.get(m.get("kind") or ""),
            ))
        nav = j.get("pagination") or {}
        return MangaListPage(items=items, has_next=int(nav.get("current_page") or 1) < int(nav.get("last_page") or 1))

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        params: dict[str, Any] = {"limit": 20, "page": query.page, "order_by": _SORT.get(query.sort, "popular")}
        if query.query:
            params["search"] = query.query
        kinds = [_KINDS[t] for t in query.types if t in _KINDS]
        if kinds:
            params["kinds"] = ",".join(kinds)
        statuses = [_STATUS_FWD[s] for s in query.statuses if s in _STATUS_FWD]
        if statuses:
            params["status"] = ",".join(statuses)
        return self._parse_list(await self.http.get_json(f"{self.api}/catalog/", params=params))

    async def _manga(self, manga_id: str) -> dict[str, Any]:
        j = await self.http.get_json(f"{self.api}/{manga_id}/")
        m = j.get("manga")
        if not isinstance(m, dict):
            raise SourceParseError(self.info.id, "unexpected payload")
        return m

    def _details(self, manga_id: str, m: dict[str, Any]) -> MangaDetails:
        genres = m.get("genres")
        if isinstance(genres, str):
            glist = [g.strip() for g in genres.split(",") if g.strip()]
        else:
            glist = [g.get("russian") or g.get("name") or "" for g in genres or [] if isinstance(g, dict)]
        synonyms = m.get("synonyms") or []
        if isinstance(synonyms, str):
            synonyms = [x.strip() for x in synonyms.split(",")]
        status = {"continued": PublicationStatus.ONGOING, "completed": PublicationStatus.COMPLETED}.get(
            m.get("trans_status") or "", _STATUS.get(m.get("status") or "", PublicationStatus.UNKNOWN))
        rating = m.get("content_rating")
        return MangaDetails(
            id=manga_id,
            title=m.get("russian") or m.get("name") or manga_id,
            alt_titles=[x for x in [m.get("name"), *synonyms] if x and x != m.get("russian")],
            cover_url=self._cover(m),
            description=strip_html(m.get("description")),
            genres=[g for g in glist if g],
            authors=[a.get("name") or "" for a in m.get("authors") or [] if isinstance(a, dict) and a.get("name")],
            status=status,
            type=_KIND_BACK.get(m.get("kind") or ""),
            web_url=m.get("view_url") or f"{self.base_url}/manga/{manga_id}",
            age_rating=rating if rating and rating != "no" else None,
        )

    async def chapters(self, manga_id: str) -> list[Chapter]:
        j = await self.http.get_json(f"{self.api}/{manga_id}/chapters")
        out = []
        for c in j.get("chapters") or []:
            if c.get("is_readable") is False:
                continue
            number = parse_float(c.get("number"))
            vol = c.get("volume")
            out.append(Chapter(
                id=f"{manga_id}/chapters/{c.get('id')}",
                title=chapter_title(number, c.get("title") or None),
                number=number,
                volume=str(vol) if vol not in (None, 0, "0", "") else None,
                uploaded_at=from_unix(c.get("publish_date")),
            ))
        return out

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        import asyncio

        m, chs = await asyncio.gather(self._manga(manga_id), self.chapters(manga_id))
        return self._details(manga_id, m), chs

    async def details(self, manga_id: str) -> MangaDetails:
        return self._details(manga_id, await self._manga(manga_id))

    async def pages(self, chapter_id: str) -> list[PageImage]:
        if "/chapter/" in chapter_id:  # ids saved by the old API: <manga>/chapter/<id>
            chapter_id = chapter_id.replace("/chapter/", "/chapters/")
        j = await self.http.get_json(f"{self.api}/{chapter_id}")
        pages = (j.get("chapter") or {}).get("pages") or []
        return [PageImage(index=i, url=p["url"], headers=self.image_headers(p["url"]))
                for i, p in enumerate(pages) if p.get("url")]
