"""Remanga (JSON API v2). Port of sources/ru/manga/remanga/remanga.js (Apache-2.0).

Paid chapters that the account has not bought are skipped — we never try to
unlock them.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..base import SourceAdapter
from ..errors import SourceAuthRequiredError, SourceParseError
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
from ..utils import chapter_title, parse_float, parse_iso, strip_html, type_from_text

_SORT = {SortOrder.POPULAR: "-rating", SortOrder.RATING: "-avg_rating", SortOrder.UPDATED: "-chapter_date",
         SortOrder.CREATED: "-id", SortOrder.NAME: "rus_name"}
_TYPES = {MangaType.MANGA: "0", MangaType.MANHWA: "1", MangaType.MANHUA: "2", MangaType.COMIC: "4"}
_STATUSES = {PublicationStatus.COMPLETED: "0", PublicationStatus.ONGOING: "1",
             PublicationStatus.HIATUS: "2", PublicationStatus.ANNOUNCED: "3",
             PublicationStatus.CANCELLED: "5"}
_STATUS_NAME = {"Закончен": PublicationStatus.COMPLETED, "Продолжается": PublicationStatus.ONGOING,
                "Заморожен": PublicationStatus.HIATUS, "Нет переводчика": PublicationStatus.HIATUS,
                "Анонс": PublicationStatus.ANNOUNCED, "Лицензировано": PublicationStatus.CANCELLED}
MAX_CHAPTER_PAGES = 20


class RemangaAdapter(SourceAdapter):
    info = SourceInfo(
        id="remanga",
        name="Remanga",
        base_url="https://remanga.org",
        capabilities=SourceCapabilities(
            search=True, type_filter=True, status_filter=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.RATING, SortOrder.UPDATED, SortOrder.CREATED),
        ),
        notes="API v2. Платные и 18+ главы требуют токен оператора — недоступные главы скрываются.",
    )
    api_url = "https://api.remanga.org/api/v2"

    def default_headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Referer": self.info.base_url + "/"}
        if self.config.token:
            h["Authorization"] = "Bearer " + self.config.token
        return h

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.info.base_url + "/"}

    @property
    def api(self) -> str:
        return self.config.extra.get("api_url", self.api_url).rstrip("/")

    def _img(self, obj: Any) -> str:
        if not isinstance(obj, dict):
            return ""
        u = obj.get("mid") or obj.get("high") or obj.get("low") or ""
        return self.abs_url(u)

    def _parse_list(self, j: dict[str, Any]) -> MangaListPage:
        results = j.get("results") or j.get("content") or []
        items = []
        for t in results if isinstance(results, list) else []:
            ref = t.get("dir") or t.get("slug") or str(t.get("id") or "")
            if not ref:
                continue
            items.append(MangaSummary(
                id=ref,
                title=t.get("main_name") or t.get("rus_name") or t.get("secondary_name") or t.get("en_name") or "",
                cover_url=self._img(t.get("cover") or t.get("img")),
                type=type_from_text((t.get("type") or {}).get("name") if isinstance(t.get("type"), dict) else None),
            ))
        return MangaListPage(items=items, has_next=bool(j.get("next")) or len(items) >= 30)

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        if query.query:
            j = await self.http.get_json(
                f"{self.api}/search/",
                params={"query": query.query, "count": 30, "field": "titles", "page": query.page},
            )
            return self._parse_list(j)
        params: list[tuple[str, str]] = [("count", "30"), ("page", str(query.page)),
                                         ("ordering", _SORT.get(query.sort, "-rating"))]
        params += [("types", _TYPES[t]) for t in query.types if t in _TYPES]
        params += [("status", _STATUSES[s]) for s in query.statuses if s in _STATUSES]
        j = await self.http.get_json(f"{self.api}/search/catalog/", params=params)
        return self._parse_list(j)

    async def _title_json(self, manga_id: str) -> dict[str, Any]:
        body = await self.http.get_json(f"{self.api}/titles/{quote(manga_id)}/")
        t = body.get("content") or body.get("results") or body
        if not isinstance(t, dict) or not t:
            raise SourceParseError(self.info.id, "empty title payload")
        return t

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        t = await self._title_json(manga_id)
        return self._details(manga_id, t), await self._chapters(t)

    async def details(self, manga_id: str) -> MangaDetails:
        return self._details(manga_id, await self._title_json(manga_id))

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return await self._chapters(await self._title_json(manga_id))

    def _details(self, manga_id: str, t: dict[str, Any]) -> MangaDetails:
        status = t.get("status") or {}
        status_name = status.get("name") if isinstance(status, dict) else None
        type_name = (t.get("type") or {}).get("name") if isinstance(t.get("type"), dict) else None
        alt = [x for x in (t.get("secondary_name"), t.get("another_name"), t.get("en_name")) if x]
        return MangaDetails(
            id=manga_id,
            title=t.get("main_name") or t.get("rus_name") or manga_id,
            alt_titles=[a.strip() for x in alt for a in str(x).split("/") if a.strip()],
            cover_url=self._img(t.get("cover") or t.get("img")),
            description=strip_html(t.get("description")),
            genres=[g.get("name", "") for g in t.get("genres") or [] if g.get("name")],
            authors=[p.get("name", "") for p in t.get("publishers") or [] if p.get("name")],
            status=_STATUS_NAME.get(status_name or "", PublicationStatus.UNKNOWN),
            type=type_from_text(type_name),
            web_url=f"{self.info.base_url}/manga/{manga_id}",
            age_rating=((t.get("age_limit") or {}).get("name") if isinstance(t.get("age_limit"), dict) else None),
        )

    async def _chapters(self, t: dict[str, Any]) -> list[Chapter]:
        branches = t.get("branches") or []
        if not branches:
            return []
        branch_id = branches[0].get("id")
        out: list[Chapter] = []
        for page in range(1, MAX_CHAPTER_PAGES + 1):
            cj = await self.http.get_json(
                f"{self.api}/titles/chapters/",
                params={"branch_id": branch_id, "page": page, "count": 500, "ordering": "-index"},
            )
            arr = cj.get("content") or cj.get("results") or []
            if not arr:
                break
            for c in arr:
                if c.get("is_published") is False:
                    continue
                if c.get("is_paid") and not c.get("is_bought") and not c.get("is_free_today"):
                    continue  # respect paywall
                number = parse_float(c.get("chapter"))
                tome = c.get("tome")
                out.append(Chapter(
                    id=str(c.get("id")),
                    title=chapter_title(number, c.get("name") or None),
                    number=number,
                    volume=str(tome) if tome not in (None, 0, "0") else None,
                    uploaded_at=parse_iso(c.get("upload_date")),
                    scanlator=", ".join(p.get("name", "") for p in c.get("publishers") or []) or None,
                ))
            if not cj.get("next"):
                break
        return out

    async def pages(self, chapter_id: str) -> list[PageImage]:
        j = await self.http.get_json(f"{self.api}/titles/chapters/{quote(chapter_id)}/")
        data = j.get("content") or j.get("results") or j
        flat: list[dict[str, Any]] = []
        for p in data.get("pages") or []:
            if isinstance(p, list):
                flat.extend(pp for pp in p if isinstance(pp, dict) and pp.get("link"))
            elif isinstance(p, dict) and p.get("link"):
                flat.append(p)
        if not flat:
            raise SourceAuthRequiredError(self.info.id, "chapter is paid or requires login")
        return [PageImage(index=i, url=p["link"], headers=self.image_headers(p["link"])) for i, p in enumerate(flat)]
