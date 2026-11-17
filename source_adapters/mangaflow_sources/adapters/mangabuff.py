"""MangaBuff (HTML). Port of sources/ru/manga/mangabuff/mangabuff.js (Apache-2.0)."""
from __future__ import annotations

import re
from urllib.parse import quote

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

_BG_RE = re.compile(r"background-image:\s*url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
_SORT = {SortOrder.POPULAR: "rating", SortOrder.RATING: "rating", SortOrder.UPDATED: "last_updated",
         SortOrder.CREATED: "created_at", SortOrder.NAME: "name"}


class MangaBuffAdapter(SourceAdapter):
    info = SourceInfo(
        id="mangabuff",
        name="MangaBuff",
        base_url="https://mangabuff.ru",
        capabilities=SourceCapabilities(
            search=True, sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED),
        ),
    )

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Referer": (self.config.base_url or self.info.base_url) + "/"}

    def parse_list(self, html: str) -> MangaListPage:
        tree = HTMLParser(html)
        items: list[MangaSummary] = []
        seen: set[str] = set()
        for card in tree.css("div.cards a.cards__item, a.cards__item"):
            if "cloned" in (card.attributes.get("class") or ""):
                continue
            href = card.attributes.get("href") or ""
            if not href or href in seen:
                continue
            seen.add(href)
            cover = ""
            img_div = card.css_first("div.cards__img")
            if img_div is not None:
                m = _BG_RE.search(img_div.attributes.get("style") or "")
                cover = m.group(1) if m else ""
            if not cover:
                img = card.css_first("img")
                if img is not None:
                    cover = img.attributes.get("data-src") or img.attributes.get("src") or ""
            name_el = card.css_first("div.cards__name")
            name = clean(name_el.text()) if name_el else (card.attributes.get("title") or "").strip()
            if not name:
                continue
            ref = href.replace(self.base_url, "")
            items.append(MangaSummary(id=ref, title=name, cover_url=self.abs_url(cover)))
        return MangaListPage(items=items, has_next=len(items) >= 10)

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        if query.query:
            url = f"{self.base_url}/search?query={quote(query.query)}&page={query.page}"
        else:
            url = f"{self.base_url}/manga?sort={_SORT.get(query.sort, 'rating')}&page={query.page}"
        return self.parse_list(await self.http.get_text(url))

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        html = await self.http.get_text(self.abs_url(manga_id))
        return self.parse_details(manga_id, html), self.parse_chapters(html)

    async def details(self, manga_id: str) -> MangaDetails:
        return (await self.title(manga_id))[0]

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return (await self.title(manga_id))[1]

    def parse_details(self, manga_id: str, html: str) -> MangaDetails:
        tree = HTMLParser(html)
        og_img = tree.css_first("meta[property='og:image']")
        og_title = tree.css_first("meta[property='og:title']")
        name_el = tree.css_first("h1.manga__name")
        name = clean(name_el.text()) if name_el else ((og_title.attributes.get("content") if og_title else "") or "")
        if not name:
            raise SourceParseError(self.info.id, "title not found")
        alt_el = tree.css_first("h3.manga__name-alt") or tree.css_first("div.manga__name-alt")
        desc_el = tree.css_first("div.manga__description")
        genres = [clean(a.text()) for a in tree.css("div.tags a, a.tags__item")]
        status_text, type_text = "", ""
        for row in tree.css("div.info-list__row, div.manga__middle-link"):
            text = row.text()
            if "Статус" in text:
                val = row.css_first("div.info-list__value, dd")
                status_text = val.text() if val else text
        status_el = tree.css_first("span.manga__status")
        if not status_text and status_el is not None:
            status_text = status_el.text()
        type_el = tree.css_first("a[href*='/types/']")
        if type_el is not None:
            type_text = type_el.text()
        return MangaDetails(
            id=manga_id,
            title=name,
            alt_titles=[clean(x) for x in (alt_el.text() if alt_el else "").split("/") if clean(x)],
            cover_url=self.abs_url((og_img.attributes.get("content") if og_img else "") or ""),
            description=desc_el.text(strip=False).strip() if desc_el else "",
            genres=[g for g in genres if g],
            status=status_from_ru(status_text),
            type=type_from_text(type_text),
            web_url=self.abs_url(manga_id),
        )

    def parse_chapters(self, html: str) -> list[Chapter]:
        tree = HTMLParser(html)
        els = tree.css("div.chapters__list a.chapters__item") or tree.css("a.chapters__item")
        out: list[Chapter] = []
        for i, el in enumerate(els):
            href = el.attributes.get("href") or ""
            if not href:
                continue
            name_el = el.css_first("div.chapters__name")
            value_el = el.css_first("div.chapters__value span")
            name = clean(name_el.text()) if name_el else ""
            value = clean(value_el.text()) if value_el else ""
            number = parse_float(value) if value else parse_float(href.rstrip("/").split("/")[-1])
            vol_match = re.search(r"/(\d+)/[\d.]+/?$", href)
            title = f"Глава {value}" if value else f"Глава {len(els) - i}"
            if name and name not in title:
                title += f": {name}"
            out.append(Chapter(
                id=href.replace(self.base_url, "") + ("&" if "?" in href else "?") + "style=list",
                title=title,
                number=number,
                volume=vol_match.group(1) if vol_match else None,
                uploaded_at=parse_ru_date(el.attributes.get("data-chapter-date") or ""),
            ))
        return out

    async def pages(self, chapter_id: str) -> list[PageImage]:
        return self.parse_pages(await self.http.get_text(self.abs_url(chapter_id)))

    def parse_pages(self, html: str) -> list[PageImage]:
        tree = HTMLParser(html)
        pages: dict[int, str] = {}
        for item in tree.css("div.reader__item"):
            try:
                num = int(item.attributes.get("data-page") or len(pages) + 1)
            except ValueError:
                num = len(pages) + 1
            img = item.css_first("img")
            if img is None:
                continue
            src = (img.attributes.get("data-src") or img.attributes.get("src") or "").strip()
            if src:
                pages[num] = self.abs_url(src)
        return [PageImage(index=i, url=u, headers=self.image_headers(u))
                for i, (_, u) in enumerate(sorted(pages.items()))]
