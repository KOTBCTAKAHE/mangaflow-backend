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
        notes="Публичный API /manga/api. Домен менялся: desu.me → desu.city → desu.uno.",
    )

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Referer": (self.config.base_url or self.info.base_url) + "/"}

    @property
    def api(self) -> str:
        return f"{self.base_url}/manga/api"

    def _parse_list(self, j: dict[str, Any]) -> MangaListPage:
        items = []
        for m in j.get("response") or []:
            img = m.get("image") or {}
            items.append(MangaSummary(
                id=str(m.get("id")),
                title=m.get("russian") or m.get("name") or "",
                cover_url=img.get("original") or img.get("preview") or "",
                type=_KIND_BACK.get(m.get("kind") or ""),
            ))
        nav = j.get("pageNavParams") or {}
        page, limit, count = nav.get("page", 1), nav.get("limit", 20), nav.get("count", 0)
        return MangaListPage(items=items, has_next=page * limit < count)

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        params: dict[str, Any] = {"limit": 20, "page": query.page, "order": _SORT.get(query.sort, "popular")}
        if query.query:
            params["search"] = query.query
        kinds = [_KINDS[t] for t in query.types if t in _KINDS]
        if kinds:
            params["kinds"] = ",".join(kinds)
        statuses = [_STATUS_FWD[s] for s in query.statuses if s in _STATUS_FWD]
        if statuses:
            params["status"] = statuses[0]
        j = await self.http.get_json(f"{self.api}/", params=params)
        return self._parse_list(j)

    async def _manga(self, manga_id: str) -> dict[str, Any]:
        j = await self.http.get_json(f"{self.api}/{manga_id}")
        m = j.get("response")
        if not isinstance(m, dict):
            raise SourceParseError(self.info.id, "unexpected payload")
        return m

    def _details(self, manga_id: str, m: dict[str, Any]) -> MangaDetails:
        genres = m.get("genres")
        if isinstance(genres, str):
            glist = [g.strip() for g in genres.split(",") if g.strip()]
        else:
            glist = [g.get("russian") or g.get("text") or g.get("name") or "" for g in genres or []]
        img = m.get("image") or {}
        synonyms = m.get("synonyms") or ""
        return MangaDetails(
            id=manga_id,
            title=m.get("russian") or m.get("name") or manga_id,
            alt_titles=[x for x in [m.get("name")] + [s.strip() for s in str(synonyms).split(",")] if x],
            cover_url=img.get("original") or img.get("preview") or "",
            description=strip_html(m.get("description")),
            genres=[g for g in glist if g],
            authors=[a.get("people_name") or a.get("name") or "" for a in m.get("authors") or [] if isinstance(a, dict)],
            status=_STATUS.get(m.get("status") or "", PublicationStatus.UNKNOWN),
            type=_KIND_BACK.get(m.get("kind") or ""),
            web_url=m.get("url") or f"{self.base_url}/manga/{manga_id}",
            age_rating=m.get("age_limit") or None,
        )

    def _chapters(self, manga_id: str, m: dict[str, Any]) -> list[Chapter]:
        out = []
        for c in (m.get("chapters") or {}).get("list") or []:
            number = parse_float(c.get("ch"))
            vol = c.get("vol")
            out.append(Chapter(
                id=f"{manga_id}/chapter/{c.get('id')}",
                title=chapter_title(number, c.get("title") or None),
                number=number,
                volume=str(vol) if vol not in (None, 0, "0") else None,
                uploaded_at=from_unix(c.get("date")),
            ))
        return out

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        m = await self._manga(manga_id)
        return self._details(manga_id, m), self._chapters(manga_id, m)

    async def details(self, manga_id: str) -> MangaDetails:
        return self._details(manga_id, await self._manga(manga_id))

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return self._chapters(manga_id, await self._manga(manga_id))

    async def pages(self, chapter_id: str) -> list[PageImage]:
        j = await self.http.get_json(f"{self.api}/{chapter_id}")
        data = j.get("response") or {}
        pages = (data.get("pages") or {}).get("list") or []
        return [PageImage(index=i, url=p["img"], headers=self.image_headers(p["img"]))
                for i, p in enumerate(pages) if p.get("img")]
