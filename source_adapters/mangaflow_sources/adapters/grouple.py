"""Grouple engine (ReadManga, MintManga, SelfManga).

Port of ``lib/grouple_base.js`` from redcatpet/mangayomi-ru-sources (Apache-2.0).
All mirrors share one HTML template; domains rotate because of blocks, so the
base URL is configurable through :class:`SourceConfig`.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from ..base import SourceAdapter
from ..errors import SourceParseError
from ..models import (
    Chapter,
    Genre,
    MangaDetails,
    MangaListPage,
    MangaSummary,
    PageImage,
    SearchQuery,
    SortOrder,
    SourceCapabilities,
)
from ..utils import chapter_title, clean, parse_ru_date, split_volume_chapter, status_from_ru

PAGE_SIZE = 50

GROUPLE_GENRES: tuple[Genre, ...] = tuple(
    Genre(slug, name)
    for name, slug in [
        ("Арт", "art"), ("Боевик", "action"), ("Боевые искусства", "martial_arts"),
        ("Вампиры", "vampires"), ("Гарем", "harem"), ("Гендерная интрига", "gender_intriga"),
        ("Героическое фэнтези", "heroic_fantasy"), ("Детектив", "detective"),
        ("Дзёсэй", "josei"), ("Драма", "drama"), ("Игра", "game"), ("Исэкай", "isekai"),
        ("История", "historical"), ("Киберпанк", "cyberpunk"), ("Кодомо", "codomo"),
        ("Комедия", "comedy"), ("Махо-сёдзё", "maho-shoujo"), ("Меха", "mecha"),
        ("Мистика", "mystery"), ("Музыка", "music"), ("Научная фантастика", "sci_fi"),
        ("Повседневность", "slice_of_life"), ("Постапокалиптика", "post_apocalyptic"),
        ("Приключения", "adventure"), ("Психология", "psychological"), ("Романтика", "romance"),
        ("Сверхъестественное", "supernatural"), ("Сёдзё", "shoujo"), ("Сёнэн", "shounen"),
        ("Спорт", "sports"), ("Сэйнэн", "seinen"), ("Трагедия", "tragedy"),
        ("Триллер", "thriller"), ("Ужасы", "horror"), ("Фантастика", "fantasy"),
        ("Фэнтези", "fantastic"), ("Школа", "school"),
    ]
)

_SORT = {
    SortOrder.POPULAR: "RATING",
    SortOrder.RATING: "RATING",
    SortOrder.UPDATED: "UPDATED",
    SortOrder.CREATED: "CREATED",
    SortOrder.NAME: "NAME",
}

GROUPLE_CAPS = SourceCapabilities(
    search=True,
    genre_filter=True,
    sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.NAME),
)

_ENTRY_RE = re.compile(
    r"""\[\s*(['"])([^'"]*)\1\s*,\s*(['"])([^'"]*)\3\s*,\s*(['"])([^'"]*)\5\s*(?:,\s*[^\]]+)?\]"""
)


def _attr(node: Node | None, *names: str) -> str:
    if node is None:
        return ""
    for n in names:
        v = node.attributes.get(n)
        if v:
            return v.strip()
    return ""


class GroupleAdapter(SourceAdapter):
    """Shared implementation; concrete sources only declare ``info``."""

    def default_headers(self) -> dict[str, str]:
        h = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            # Mirrors reject unknown referers; the upstream extension uses google.
            "Referer": "https://www.google.com/",
        }
        if self.config.cookie:
            h["Cookie"] = self.config.cookie
        return h

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.base_url + "/"}

    # ---------------------------------------------------------------- lists
    def parse_list(self, html: str) -> MangaListPage:
        tree = HTMLParser(html)
        tiles = tree.css("div.tile")
        items: list[MangaSummary] = []
        for tile in tiles:
            link = (
                tile.css_first("div.img a.non-hover")
                or tile.css_first("div.img a")
                or tile.css_first("h3 a")
            )
            href = _attr(link, "href")
            if not href:
                continue
            img = tile.css_first("div.img img") or tile.css_first("img")
            cover = _attr(img, "data-original", "original", "data-src", "src")
            name = _attr(img, "title")
            if not name:
                h3a = tile.css_first("h3 a")
                name = _attr(h3a, "title") or (clean(h3a.text()) if h3a else "")
            if not name and link is not None:
                name = _attr(link, "title") or clean(link.text())
            if not name:
                continue
            if href.startswith(self.base_url):
                href = href[len(self.base_url):]
            if href.startswith("http"):
                continue  # external link (e.g. sister site)
            items.append(MangaSummary(id=href, title=name, cover_url=self.abs_url(cover)))
        return MangaListPage(items=items, has_next=len(tiles) >= PAGE_SIZE)

    def _list_url(self, q: SearchQuery) -> str:
        offset = (max(q.page, 1) - 1) * PAGE_SIZE
        sort = _SORT.get(q.sort, "RATING")
        genre = q.genres_include[0] if q.genres_include else ""
        if q.query:
            url = (
                f"{self.base_url}/search/advancedResults?q={quote(q.query)}"
                f"&offset={offset}&sortType={sort}"
            )
            if genre:
                url += f"&el_{genre}=1"
            return url
        if genre:
            return f"{self.base_url}/list/genre/{genre}?sortType={sort}&offset={offset}"
        return f"{self.base_url}/list?sortType={sort}&offset={offset}"

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        html = await self.http.get_text(self._list_url(query))
        return self.parse_list(html)

    # ---------------------------------------------------------------- title
    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        html = await self.http.get_text(self.abs_url(manga_id))
        return self.parse_details(manga_id, html), self.parse_chapters(html)

    async def details(self, manga_id: str) -> MangaDetails:
        return (await self.title(manga_id))[0]

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return (await self.title(manga_id))[1]

    def parse_details(self, manga_id: str, html: str) -> MangaDetails:
        tree = HTMLParser(html)
        name_el = (
            tree.css_first("h1.cr-hero-names__main")
            or tree.css_first("h1.names span.name")
            or tree.css_first("span.name")
            or tree.css_first("h1")
        )
        if name_el is None:
            raise SourceParseError(self.info.id, f"no title on page {manga_id}")
        alt = [clean(n.text()) for n in tree.css("span.eng-name, span.original-name, .cr-hero-names__alt")]
        cover_el = (
            tree.css_first("img.cr-hero-poster__img")
            or tree.css_first("div.picture-fotorama img")
            or tree.css_first("img.manga-cover")
        )
        cover = self.abs_url(_attr(cover_el, "data-src", "data-full", "data-thumb", "src"))

        description = _attr(tree.css_first("meta[itemprop=description]"), "content")
        if not description:
            d = tree.css_first("div.cr-description__content") or tree.css_first("div.manga-description")
            description = d.text(strip=True) if d else ""

        persons: list[str] = []
        for a in tree.css("a[href*='/list/person/']"):
            p = clean(a.text())
            if p and p not in persons and len(p) < 60 and not re.search(r"\d", p):
                persons.append(p)
        artists = [clean(a.text()) for a in tree.css("span.elem_illustrator a.person-link")]

        genres: list[str] = []
        for a in tree.css("a[href*='/list/category/'], a[href*='/list/genre/']"):
            g = clean(a.text())
            if g and g not in genres:
                genres.append(g)

        status_el = tree.css_first(".cr-info-details-item__status")
        status_text = status_el.text() if status_el else ""
        if not status_text:
            for m in tree.css("p.subject-meta"):
                if "Статус" in m.text():
                    status_text = m.text()
                    break

        return MangaDetails(
            id=manga_id,
            title=clean(name_el.text()),
            alt_titles=[a for a in alt if a],
            cover_url=cover,
            description=description.strip(),
            genres=genres,
            authors=persons,
            artists=[a for a in artists if a],
            status=status_from_ru(status_text),
            web_url=self.abs_url(manga_id),
        )

    def parse_chapters(self, html: str) -> list[Chapter]:
        tree = HTMLParser(html)
        rows = tree.css("tr.item-row") or tree.css("div.chapters-link table tr, table.table-chapters tr")
        chapters: list[Chapter] = []
        for row in rows:
            a = (
                row.css_first("a.chapter-link.cp-l")
                or row.css_first("a.chapter-link")
                or row.css_first("a[href*='/vol']")
                or row.css_first("a")
            )
            href = _attr(a, "href")
            if not href or href == "/" or href.startswith("#"):
                continue
            raw = clean(a.text().replace(" новое", "")) if a else ""
            if not raw:
                continue
            volume, number, sub = split_volume_chapter(raw)
            title = chapter_title(number, sub or None) if number is not None else raw
            date_cell = row.css_first("td[data-date-raw]") or row.css_first("td.date")
            date_txt = _attr(date_cell, "data-date-raw", "data-date") or (
                date_cell.text() if date_cell else ""
            )
            if not date_txt:
                tds = row.css("td")
                date_txt = tds[-1].text() if tds else ""
            scanlator = _attr(a, "title").replace(" (Переводчик)", "").strip() or None
            chapters.append(
                Chapter(
                    id=href + ("&" if "?" in href else "?") + "mtr=true",
                    title=title,
                    number=number,
                    volume=volume if volume and volume != "0" else None,
                    uploaded_at=parse_ru_date(date_txt),
                    scanlator=scanlator,
                )
            )
        return chapters

    # ---------------------------------------------------------------- pages
    async def pages(self, chapter_id: str) -> list[PageImage]:
        html = await self.http.get_text(self.abs_url(chapter_id))
        return self.parse_pages(html)

    def parse_pages(self, body: str) -> list[PageImage]:
        idx = -1
        for marker in ("readerInit(", "rm_h.initReader(", "rm_h.readerDoInit("):
            idx = body.find(marker)
            if idx >= 0:
                break
        if idx < 0:
            raise SourceParseError(self.info.id, "reader script not found (auth required or layout changed)")
        start = body.find("[[", idx)
        end = body.find("]]", start)
        if start < 0 or end < 0:
            raise SourceParseError(self.info.id, "page array not found")
        inner = body[start + 1 : end + 1]
        pages: list[PageImage] = []
        for m in _ENTRY_RE.finditer(inner):
            host, extra, path = m.group(2), m.group(4), m.group(6)
            if extra.startswith("/manga/"):
                full = host + path
            elif not host and path.startswith("/static/"):
                full = self.base_url + path
            elif host:
                full = host + extra + path
            else:
                full = path
            if full.startswith("//"):
                full = "https:" + full
            elif not full.startswith("http"):
                full = "https:" + full
            if "one-way.work" in full and "?" in full:
                full = full.split("?", 1)[0]
            pages.append(PageImage(index=len(pages), url=full, headers=self.image_headers(full)))
        return pages
