"""Acomics — Russian web comics (HTML). Port of sources/ru/manga/acomics/acomics.js (Apache-2.0).

Each "chapter" is a single issue page with one image.
"""
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
    MangaType,
    PageImage,
    SearchQuery,
    SortOrder,
    SourceCapabilities,
    SourceInfo,
)
from ..utils import clean

PAGE_SIZE = 10


class AcomicsAdapter(SourceAdapter):
    info = SourceInfo(
        id="acomics",
        name="Acomics",
        base_url="https://acomics.ru",
        capabilities=SourceCapabilities(search=True, sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED)),
        notes="Русские веб-комиксы; один выпуск = одна страница.",
    )

    def default_headers(self) -> dict[str, str]:
        # without the age cookie 18+ issues come back as an age gate with no image
        return {**super().default_headers(), "Cookie": "ageRestrict=18"}

    def parse_list(self, html: str) -> MangaListPage:
        tree = HTMLParser(html)
        items = []
        for card in tree.css("section.serial-card"):
            a = card.css_first("a.cover") or card.css_first("h2.title a")
            href = (a.attributes.get("href") if a is not None else "") or ""
            if "/~" not in href or (href.startswith("http") and "acomics.ru" not in href):
                continue
            img = card.css_first("img")
            cover = ""
            if img is not None:
                cover = img.attributes.get("data-real-src") or img.attributes.get("src") or ""
                if "catalog-stub" in cover:
                    cover = ""
            title_el = card.css_first("h2.title a")
            name = clean(title_el.text()) if title_el else (a.attributes.get("title") or "")
            if name:
                slug = re.search(r"~([^/?#]+)", href)
                items.append(MangaSummary(id=slug.group(1) if slug else href, title=name,
                                          cover_url=self.abs_url(cover), type=MangaType.COMIC))
        return MangaListPage(items=items, has_next=len(items) >= PAGE_SIZE)

    async def popular(self, page: int = 1) -> MangaListPage:
        skip = (page - 1) * PAGE_SIZE
        return self.parse_list(await self.http.get_text(f"{self.base_url}/comics?skip={skip}"))

    async def latest(self, page: int = 1) -> MangaListPage:
        skip = (page - 1) * PAGE_SIZE
        return self.parse_list(await self.http.get_text(
            f"{self.base_url}/comics?ratings[]=1&ratings[]=2&ratings[]=3&ratings[]=4&type=0&sort=last_update&skip={skip}"))

    async def search(self, query: SearchQuery) -> MangaListPage:
        if not query.query:
            return await (self.latest(query.page) if query.sort == SortOrder.UPDATED else self.popular(query.page))
        if query.page > 1:
            return MangaListPage(items=[], has_next=False)
        return self.parse_list(await self.http.get_text(f"{self.base_url}/search?keyword={quote(query.query)}"))

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        html = await self.http.get_text(f"{self.base_url}/~{manga_id}/about")
        return self.parse_details(manga_id, html), self.parse_chapters(manga_id, html)

    async def details(self, manga_id: str) -> MangaDetails:
        return (await self.title(manga_id))[0]

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return (await self.title(manga_id))[1]

    def parse_details(self, slug: str, html: str) -> MangaDetails:
        tree = HTMLParser(html)
        h1 = tree.css_first("h1")
        img = (tree.css_first("img.serial-header-image") or tree.css_first(".serial-header img")
               or tree.css_first("img[src^='/upload/header/']"))
        desc = (tree.css_first("p.serial-about-text") or tree.css_first("div.serial-about-text")
                or tree.css_first("section.serial-description"))
        authors_el = tree.css_first(".serial-about-authors")
        return MangaDetails(
            id=slug,
            title=clean(h1.text()) if h1 else slug,
            cover_url=self.abs_url((img.attributes.get("data-real-src") or img.attributes.get("src") or "") if img else ""),
            description=desc.text().strip() if desc else "",
            authors=[clean(a.text()) for a in authors_el.css("a")] if authors_el else [],
            genres=[clean(a.text()) for a in tree.css("a.tag, a[href*='/serial-category/']") if clean(a.text())],
            type=MangaType.COMIC,
            web_url=f"{self.base_url}/~{slug}",
        )

    def parse_chapters(self, slug: str, html: str) -> list[Chapter]:
        tree = HTMLParser(html)
        body = tree.body.text() if tree.body else ""
        m = re.search(r"Количество выпусков:\s*(\d+)", body)
        total = int(m.group(1)) if m else 0
        if not total:
            for a in tree.css(f"a[href^='/~{slug}/']"):
                tail = (a.attributes.get("href") or "").split(f"/~{slug}/", 1)[-1]
                num = re.match(r"\d+", tail)
                if num:
                    total = max(total, int(num.group(0)))
        return [Chapter(id=f"/~{slug}/{i}", title=f"Выпуск {i}", number=float(i)) for i in range(total, 0, -1)]

    async def pages(self, chapter_id: str) -> list[PageImage]:
        tree = HTMLParser(await self.http.get_text(self.abs_url(chapter_id)))
        img = (tree.css_first("img.issue") or tree.css_first("img#mainImage")
               or tree.css_first("section.issue img") or tree.css_first("div.issue img"))
        if img is None:
            raise SourceParseError(self.info.id, "issue image not found")
        url = self.abs_url(img.attributes.get("src") or img.attributes.get("data-real-src") or "")
        return [PageImage(index=0, url=url, headers=self.image_headers(url))]
