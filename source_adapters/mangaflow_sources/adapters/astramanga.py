"""AstraManga — JSON API at api.astramanga.org. Port of keiyoushi src/ru/astramanga (Apache-2.0)."""
from __future__ import annotations

import asyncio
import math
from typing import Any
from urllib.parse import urlparse

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
from ..utils import chapter_title, parse_float, parse_iso

PAGE_SIZE = 30
CHAPTERS_PAGE_SIZE = 5000

_TYPES = {"manga": MangaType.MANGA, "manhwa": MangaType.MANHWA, "manhua": MangaType.MANHUA, "comics": MangaType.COMIC}
_TYPE_PARAM = {v: k for k, v in _TYPES.items()}
_STATUS = {
    "ongoing": PublicationStatus.ONGOING,
    "completed": PublicationStatus.COMPLETED,
    "paused": PublicationStatus.HIATUS,
    "frozen": PublicationStatus.HIATUS,
    "discontinued": PublicationStatus.CANCELLED,
}
_STATUS_PARAM = {PublicationStatus.ONGOING: "ongoing", PublicationStatus.COMPLETED: "completed",
                 PublicationStatus.HIATUS: "paused", PublicationStatus.CANCELLED: "discontinued"}
_SORT = {SortOrder.POPULAR: "-popularity", SortOrder.UPDATED: "-updated_at", SortOrder.CREATED: "-created_at",
         SortOrder.RATING: "-rating", SortOrder.NAME: "name"}


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


class AstraMangaAdapter(SourceAdapter):
    info = SourceInfo(
        id="astramanga",
        name="AstraManga",
        base_url="https://astramanga.org",
        capabilities=SourceCapabilities(
            search=True, type_filter=True, status_filter=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.RATING, SortOrder.NAME),
        ),
        notes="JSON API api.<домен>/api/v1. Несколько веток перевода объединяются.",
        origin="keiyoushi/extensions-source",
    )

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Referer": self.base_url + "/", "Origin": self.base_url}

    @property
    def api(self) -> str:
        return f"https://api.{urlparse(self.base_url).hostname}/api/v1"

    @property
    def media(self) -> str:
        return f"{self.base_url}/media"

    def _cover(self, t: dict[str, Any]) -> str:
        versions = t.get("cover_versions") or {}
        path = _s(t.get("cover_image")) or _s(versions.get("high")) or _s(versions.get("mid"))
        if not path:
            return ""
        return path if path.startswith("http") else f"{self.media}/{path.lstrip('/')}"

    def _summary(self, t: dict[str, Any]) -> MangaSummary:
        return MangaSummary(id=_s(t.get("slug")), title=_s(t.get("name")), cover_url=self._cover(t),
                            type=_TYPES.get(_s(t.get("type"))))

    async def _catalog(self, q: SearchQuery) -> MangaListPage:
        params: list[tuple[str, str]] = [("sort", _SORT.get(q.sort, "-popularity")), ("page", str(q.page)),
                                         ("page_size", str(PAGE_SIZE))]
        if q.query.strip():
            params.append(("query", q.query.strip()))
        params += [("type", _TYPE_PARAM[t]) for t in q.types if t in _TYPE_PARAM]
        params += [("status", _STATUS_PARAM[s]) for s in q.statuses if s in _STATUS_PARAM]
        j = await self.http.get_json(f"{self.api}/search", params=params)
        data = j.get("data") or {}
        items = [self._summary(t) for t in data.get("titles") or [] if t.get("slug")]
        return MangaListPage(items=items, has_next=int(data.get("current_page") or 1) < int(data.get("total_pages") or 0))

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self._catalog(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self._catalog(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        return await self._catalog(query)

    async def _title(self, slug: str) -> dict[str, Any]:
        j = await self.http.get_json(f"{self.api}/titles/{slug}")
        d = j.get("data")
        if not isinstance(d, dict):
            raise SourceParseError(self.info.id, "no title data")
        return d

    def _details(self, d: dict[str, Any]) -> MangaDetails:
        alt = [x for x in [_s(d.get("secondary_name")), *(d.get("alternative_names") or [])] if isinstance(x, str) and x]
        publisher = (d.get("publishing_house") or {}).get("name") or next(
            (p.get("name") for p in d.get("publishers") or [] if p.get("name")), None)
        genres = [g.get("name") for g in (d.get("genres") or []) + (d.get("tags") or []) if g.get("name")]
        return MangaDetails(
            id=_s(d.get("slug")),
            title=_s(d.get("name")),
            cover_url=self._cover(d),
            alt_titles=alt,
            description=_s(d.get("description")).strip(),
            genres=list(dict.fromkeys(genres)),
            authors=[publisher] if publisher else [],
            status=_STATUS.get(_s(d.get("status")), PublicationStatus.UNKNOWN),
            type=_TYPES.get(_s(d.get("type"))),
            web_url=f"{self.base_url}/manga/{_s(d.get('slug'))}",
        )

    async def details(self, manga_id: str) -> MangaDetails:
        return self._details(await self._title(manga_id))

    async def _chapters_of(self, title_id: Any) -> list[Chapter]:
        j = await self.http.get_json(f"{self.api}/titles/{title_id}/branches")
        branches = (j.get("data") or {}).get("branches") or []

        async def branch_chapters(b: dict[str, Any]) -> list[Chapter]:
            total = max(1, math.ceil((b.get("count_chapters") or 0) / CHAPTERS_PAGE_SIZE))
            out: list[Chapter] = []
            for page in range(1, total + 1):
                cj = await self.http.get_json(f"{self.api}/branches/{b['id']}/chapters",
                                              params=[("page", str(page)), ("page_size", str(CHAPTERS_PAGE_SIZE))])
                for c in (cj.get("data") or {}).get("items") or []:
                    num = parse_float(c.get("number"))
                    name = _s(c.get("name"))
                    if name and name == chapter_title(num):
                        name = ""
                    vol = c.get("volume_number")
                    out.append(Chapter(
                        id=str(c["id"]),
                        title=(f"Том {vol} " if vol is not None else "") + chapter_title(num, name or None),
                        number=num,
                        volume=str(vol) if vol is not None else None,
                        uploaded_at=parse_iso(c.get("published_at")),
                        scanlator=_s(b.get("name")) or None,
                    ))
            return out

        chunks = await asyncio.gather(*(branch_chapters(b) for b in branches if b.get("id") is not None))
        chapters = [c for chunk in chunks for c in chunk]
        chapters.sort(key=lambda c: (c.number or 0, c.uploaded_at.timestamp() if c.uploaded_at else 0), reverse=True)
        return chapters

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        d = await self._title(manga_id)
        return self._details(d), await self._chapters_of(d.get("id"))

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return (await self.title(manga_id))[1]

    async def pages(self, chapter_id: str) -> list[PageImage]:
        j = await self.http.get_json(f"{self.api}/chapters/{chapter_id}/pages")
        pages = (j.get("data") or {}).get("pages") or []
        # page_number is not sequential (sliced webtoon strips): keep the array order
        return [PageImage(index=i, url=self.abs_url(_s(p.get("image_url"))), headers=self.image_headers(""))
                for i, p in enumerate(pages) if _s(p.get("image_url"))]
