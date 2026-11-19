"""TomiloLib — JSON API at /api. Port of keiyoushi src/ru/tomilolib (Apache-2.0)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from ..base import SourceAdapter
from ..errors import SourceAuthRequiredError, SourceParseError
from ..models import (
    Chapter,
    MangaDetails,
    MangaListPage,
    MangaSummary,
    PageImage,
    PublicationStatus,
    SearchQuery,
    SortOrder,
    SourceCapabilities,
    SourceInfo,
)
from ..utils import chapter_title, parse_float, parse_iso

PAGE_LIMIT = 30
CHAPTERS_PER_PAGE = 200
CDN_URL = "https://tomilolib.s3.regru.cloud"

_STATUS = {"ongoing": PublicationStatus.ONGOING, "completed": PublicationStatus.COMPLETED,
           "pause": PublicationStatus.HIATUS, "frozen": PublicationStatus.HIATUS}
_SORT = {SortOrder.POPULAR: ("views", "desc"), SortOrder.UPDATED: ("updatedAt", "desc"),
         SortOrder.CREATED: ("createdAt", "desc"), SortOrder.RATING: ("rating", "desc"), SortOrder.NAME: ("name", "asc")}


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def resolve_image_url(path: str | None, base_url: str) -> str:
    """``/uploads/...`` objects 403 on the CDN but are served from its root without that prefix."""
    if not path:
        return ""
    url = path if path.startswith("http") else CDN_URL + ("" if path.startswith("/") else "/") + path
    for prefix in (f"{CDN_URL}/uploads", f"{base_url}/uploads"):
        if url.startswith(prefix):
            return CDN_URL + url[len(prefix):]
    return url


class TomiloLibAdapter(SourceAdapter):
    info = SourceInfo(
        id="tomilolib",
        name="TomiloLib",
        base_url="https://tomilo-lib.ru",
        capabilities=SourceCapabilities(
            search=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.RATING, SortOrder.NAME),
        ),
        notes="JSON API. 18+ тайтлы скрыты (SOURCE_TOMILOLIB_EXTRA_SHOW_ADULT=1 — показать). Платные главы помечены 🔒.",
        origin="keiyoushi/extensions-source",
    )

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Referer": self.base_url + "/"}

    @property
    def api(self) -> str:
        return f"{self.base_url}/api"

    @property
    def show_adult(self) -> bool:
        return self.config.extra.get("show_adult", "") in ("1", "true", "yes")

    def _summary(self, t: dict[str, Any]) -> MangaSummary:
        return MangaSummary(id=_s(t.get("_id")), title=_s(t.get("name")).strip(),
                            cover_url=resolve_image_url(t.get("coverImage"), self.base_url))

    async def _list(self, q: SearchQuery) -> MangaListPage:
        sort, order = _SORT.get(q.sort, ("views", "desc"))
        params = [("page", str(q.page)), ("limit", str(PAGE_LIMIT)), ("sortBy", sort), ("order", order)]
        if q.query.strip():
            params.append(("search", q.query.strip()))
        j = await self.http.get_json(f"{self.api}/titles", params=params)
        data = j.get("data") or {}
        titles = [t for t in data.get("titles") or [] if t.get("_id") and (self.show_adult or not t.get("isAdult"))]
        pag = data.get("pagination") or {}
        return MangaListPage(items=[self._summary(t) for t in titles],
                             has_next=int(pag.get("page") or 1) < int(pag.get("pages") or 1))

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self._list(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self._list(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        return await self._list(query)

    async def details(self, manga_id: str) -> MangaDetails:
        j = await self.http.get_json(f"{self.api}/titles/{manga_id}")
        d = j.get("data")
        if not isinstance(d, dict):
            raise SourceParseError(self.info.id, "no title data")
        slug = _s(d.get("slug")) or manga_id
        return MangaDetails(
            id=manga_id,
            title=_s(d.get("name")).strip(),
            cover_url=resolve_image_url(d.get("coverImage"), self.base_url),
            alt_titles=[a for a in d.get("altNames") or [] if isinstance(a, str) and a.strip()],
            description=_s(d.get("description")).strip(),
            genres=[g for g in d.get("genres") or [] if isinstance(g, str)],
            authors=[_s(d.get("author"))] if d.get("author") else [],
            artists=[_s(d.get("artist"))] if d.get("artist") else [],
            status=_STATUS.get(_s(d.get("status")), PublicationStatus.UNKNOWN),
            web_url=f"{self.base_url}/titles/{slug}",
            age_rating="18+" if d.get("isAdult") else None,
        )

    async def chapters(self, manga_id: str) -> list[Chapter]:
        raw: list[dict[str, Any]] = []
        page, pages = 1, 1
        while page <= pages and page <= 50:
            j = await self.http.get_json(f"{self.api}/chapters", params=[
                ("titleId", manga_id), ("page", str(page)), ("limit", str(CHAPTERS_PER_PAGE))])
            data = j.get("data") or {}
            raw += data.get("chapters") or []
            pages = int((data.get("pagination") or {}).get("pages") or 1)
            page += 1
        now = datetime.now(UTC)
        out = []
        for c in sorted((c for c in raw if c.get("isPublished", True)), key=lambda c: c.get("chapterNumber") or 0, reverse=True):
            num = parse_float(c.get("chapterNumber"))
            free_at = parse_iso(c.get("freeAt"))
            locked = bool(c.get("isPaid")) and (c.get("unlockPrice") or 0) > 0 and (free_at is None or free_at > now)
            out.append(Chapter(id=_s(c.get("_id")), title=_s(c.get("name")) or chapter_title(num), number=num,
                               uploaded_at=parse_iso(c.get("releaseDate")), scanlator="🔒 Платно" if locked else None))
        return out

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        d, chs = await asyncio.gather(self.details(manga_id), self.chapters(manga_id))
        return d, chs

    async def pages(self, chapter_id: str) -> list[PageImage]:
        j = await self.http.get_json(f"{self.api}/chapters/{chapter_id}")
        data = j.get("data") or {}
        pages = [p for p in data.get("pages") or [] if isinstance(p, str)]
        if not pages and data.get("isPaid"):
            raise SourceAuthRequiredError(self.info.id, "глава платная и ещё не открыта бесплатно")
        return [PageImage(index=i, url=resolve_image_url(p, self.base_url), headers=self.image_headers(""))
                for i, p in enumerate(pages)]
