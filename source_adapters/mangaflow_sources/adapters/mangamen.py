"""MangaMen — HTML catalogue, page list in ``window.__pg``. Port of keiyoushi src/ru/mangamen (Apache-2.0)."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..base import SourceAdapter
from ..errors import SourceParseError
from ..models import (
    Chapter,
    MangaDetails,
    MangaListPage,
    MangaSummary,
    PageImage,
    SearchQuery,
    SortOrder,
    SourceCapabilities,
    SourceInfo,
)
from ..utils import clean, parse_float, parse_ru_date, status_from_ru, type_from_text

MIN_PAGE_SIZE = 30
_SORT = {SortOrder.POPULAR: "views", SortOrder.UPDATED: "last_chapter_at", SortOrder.CREATED: "created_at",
         SortOrder.RATING: "rate", SortOrder.NAME: "name"}
_BG = re.compile(r"""url\(['"]?([^'")]+)['"]?\)""")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def parse_men_date(text: str | None, *, now: datetime | None = None) -> datetime | None:
    if not text:
        return None
    now = now or datetime.now(UTC)
    t = text.strip().lower()
    abs_ = parse_ru_date(t, now=now)
    if abs_ is not None:
        return abs_
    m = re.match(r"(\d+)", t)
    if not m:
        return None
    n = int(m.group(1))
    for key, delta in (("сек", timedelta(seconds=n)), ("мин", timedelta(minutes=n)), ("час", timedelta(hours=n)),
                       ("недел", timedelta(weeks=n)), ("месяц", timedelta(days=30 * n)),
                       ("год", timedelta(days=365 * n)), ("лет", timedelta(days=365 * n)),
                       ("дн", timedelta(days=n)), ("день", timedelta(days=n))):
        if key in t:
            return now - delta
    return None


class MangaMenAdapter(SourceAdapter):
    info = SourceInfo(
        id="mangamen",
        name="MangaMen",
        base_url="https://mangamen.com",
        capabilities=SourceCapabilities(
            search=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.RATING, SortOrder.NAME),
        ),
        notes="HTML. Каталог /manga-list, страницы из window.__pg.",
        origin="keiyoushi/extensions-source",
    )

    def default_headers(self) -> dict[str, str]:
        return {"User-Agent": _UA, "Referer": self.base_url + "/"}

    def image_headers(self, url: str) -> dict[str, str]:
        return {"User-Agent": _UA, "Referer": self.base_url + "/"}

    def _rel(self, href: str) -> str:
        return href[len(self.base_url):] if href.startswith(self.base_url) else href

    def parse_list(self, html: str) -> MangaListPage:
        tree = HTMLParser(html)
        items = []
        for a in tree.css("a.media-card[href]"):
            t = a.css_first(".media-card__title")
            if t is None:
                continue
            cover = a.attributes.get("data-src") or ""
            if not cover:
                m = _BG.search(a.attributes.get("style") or "")
                cover = m.group(1) if m else ""
            items.append(MangaSummary(id=self._rel(a.attributes.get("href") or ""), title=clean(t.text()),
                                      cover_url=self.abs_url(cover)))
        return MangaListPage(items=items, has_next=len(items) >= MIN_PAGE_SIZE)

    async def _list(self, q: SearchQuery) -> MangaListPage:
        params = [("sort", _SORT.get(q.sort, "views")), ("dir", "asc" if q.sort == SortOrder.NAME else "desc")]
        if q.query.strip():
            params.append(("q", q.query.strip()))  # `query=` is silently ignored by the site
        params.append(("page", str(q.page)))
        return self.parse_list(await self.http.get_text(f"{self.base_url}/manga-list", params=params))

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self._list(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self._list(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        return await self._list(query)

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        html = await self.http.get_text(self.abs_url(manga_id))
        tree = HTMLParser(html)
        return self.parse_details(manga_id, tree), self.parse_chapters(tree)

    async def details(self, manga_id: str) -> MangaDetails:
        return (await self.title(manga_id))[0]

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return (await self.title(manga_id))[1]

    def parse_details(self, manga_id: str, tree: HTMLParser) -> MangaDetails:
        h1 = tree.css_first("h1[itemprop=name]") or tree.css_first("h1")
        if h1 is None:
            raise SourceParseError(self.info.id, "title not found")
        img = tree.css_first(".manga__image img.manga__cover") or tree.css_first(".manga__image img")
        cover = ""
        if img is not None:
            cover = img.attributes.get("data-src") or img.attributes.get("src") or ""
            if cover.endswith("/preroll.svg"):
                cover = ""
        if not cover:
            og = tree.css_first("meta[property='og:image']")
            cover = (og.attributes.get("content") or "") if og else ""
        rows: dict[str, str] = {}
        for row in tree.css(".info-list__row"):
            k, v = row.css_first("strong"), row.css_first("span")
            if k is not None:
                rows[clean(k.text())] = clean(v.text()) if v is not None else ""
        alt = tree.css_first("h4[itemprop=alternativeHeadline]")
        desc = tree.css_first(".info-desc__content")
        genres = [clean(a.text()) for a in tree.css("a[href*='genres[include]'], a[href*='tags[include]']") if clean(a.text())]
        author = rows.get("Автор") or ""
        return MangaDetails(
            id=manga_id,
            title=clean(h1.text()),
            cover_url=self.abs_url(cover),
            alt_titles=[clean(alt.text())] if alt is not None and clean(alt.text()) else [],
            description=desc.text().strip() if desc is not None else "",
            genres=list(dict.fromkeys(genres)),
            authors=[author] if author else [],
            artists=[rows["Художник"]] if rows.get("Художник") else [],
            status=status_from_ru(rows.get("Статус тайтла")),
            type=type_from_text(rows.get("Тип")),
            web_url=self.abs_url(manga_id),
        )

    def parse_chapters(self, tree: HTMLParser) -> list[Chapter]:
        out = []
        for item in tree.css(".chapter-item"):
            link = item.css_first(".chapter-item__name a[href]")
            if link is None:
                continue
            vol = item.attributes.get("data-volume") or "1"
            num_s = item.attributes.get("data-number") or ""
            date = item.css_first(".chapter-item__date")
            who = item.css_first(".chapter-item__added span")
            out.append(Chapter(
                id=self._rel(link.attributes.get("href") or ""),
                title=clean(link.text()) or f"Том {vol}. Глава {num_s}",
                number=parse_float(num_s) if num_s else None,
                volume=vol,
                uploaded_at=parse_men_date(date.text() if date is not None else None),
                scanlator=(clean(who.text()) or None) if who is not None else None,
            ))
        return out

    async def pages(self, chapter_id: str) -> list[PageImage]:
        html = await self.http.get_text(self.abs_url(chapter_id))
        m = re.search(r"window\.__pg\s*=\s*(\[.*?\])\s*;", html, re.S)
        if not m:
            raise SourceParseError(self.info.id, "window.__pg not found")
        data = json.loads(m.group(1))
        data.sort(key=lambda p: p.get("p", 0))
        return [PageImage(index=i, url=self.abs_url(p["u"]), headers=self.image_headers(p["u"]))
                for i, p in enumerate(data) if p.get("u")]
