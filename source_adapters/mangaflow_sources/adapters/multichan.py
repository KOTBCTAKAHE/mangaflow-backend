"""Manga-Chan family (DLE engine): Manga-Chan, Yaoi-Chan. Port of keiyoushi lib-multisrc/multichan (Apache-2.0)."""
from __future__ import annotations

import re
from datetime import UTC, datetime
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
    PublicationStatus,
    SearchQuery,
    SortOrder,
    SourceCapabilities,
    SourceInfo,
)
from ..utils import clean, type_from_text

PAGE = 20
_NUM = re.compile(r"(глава\s|часть\s)([0-9]+\.?[0-9]*)", re.I)
_VOL = re.compile(r"(?:том|vol\.?)\s*(\d+)", re.I)
CHAN_CAPS = SourceCapabilities(search=True, sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.NAME))


class MultiChanAdapter(SourceAdapter):
    """Shared implementation; concrete sites only declare ``info``."""

    def _rel(self, href: str) -> str:
        return href[len(self.base_url):] if href.startswith(self.base_url) else href

    def parse_list(self, html: str) -> MangaListPage:
        tree = HTMLParser(html)
        items = []
        for row in tree.css("div.content_row"):
            a = row.css_first("h2 > a")
            if a is None:
                continue
            img = row.css_first("img")
            href = self._rel(a.attributes.get("href") or "")
            if not href or href.startswith("http"):
                continue
            items.append(MangaSummary(
                id=href,
                title=row.attributes.get("title") or clean(a.text()),
                cover_url=self.abs_url((img.attributes.get("src") or "") if img is not None else ""),
            ))
        nxt = any(("Вперед" in x.text()) or ("Далее" in x.text()) for x in tree.css("a"))
        return MangaListPage(items=items, has_next=nxt)

    async def popular(self, page: int = 1) -> MangaListPage:
        return self.parse_list(await self.http.get_text(f"{self.base_url}/mostfavorites?offset={PAGE * (page - 1)}"))

    async def latest(self, page: int = 1) -> MangaListPage:
        return self.parse_list(await self.http.get_text(f"{self.base_url}/manga/new?offset={PAGE * (page - 1)}"))

    async def search(self, query: SearchQuery) -> MangaListPage:
        if query.query.strip():
            url = (f"{self.base_url}/?do=search&subaction=search&story={quote(query.query.strip())}"
                   f"&search_start={max(query.page, 1)}")
            return self.parse_list(await self.http.get_text(url))
        if query.sort == SortOrder.NAME:
            return self.parse_list(await self.http.get_text(f"{self.base_url}/catalog?offset={PAGE * (query.page - 1)}"))
        if query.sort == SortOrder.UPDATED:
            return await self.latest(query.page)
        return await self.popular(query.page)

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        tree = HTMLParser(await self.http.get_text(self.abs_url(manga_id)))
        return self.parse_details(manga_id, tree), self.parse_chapters(tree)

    async def details(self, manga_id: str) -> MangaDetails:
        return (await self.title(manga_id))[0]

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return (await self.title(manga_id))[1]

    def parse_details(self, manga_id: str, tree: HTMLParser) -> MangaDetails:
        title_el = tree.css_first("title")
        name = clean(title_el.text()).split(" »")[0] if title_el is not None else ""
        if not name:
            raise SourceParseError(self.info.id, "title not found")
        info = tree.css("#info_wrap tr, #info_wrap > div")

        def row(label: str):
            return next((r for r in info if label in r.text()), None)

        author_row, type_row, status_row = row("Автор"), row("Тип"), row("Загружено")
        author = author_row.css_first(".item2") if author_row is not None else None
        type_a = type_row.css("a") if type_row is not None else []
        genres = []
        for li in tree.css(".sidetags ul"):
            links = li.css("a")
            if links:
                genres.append(clean(links[-1].text()))
        status_text = (status_row.text() if status_row is not None else "").lower()
        status = (PublicationStatus.COMPLETED if "перевод завершен" in status_text
                  else PublicationStatus.ONGOING if "перевод продолжается" in status_text else PublicationStatus.UNKNOWN)
        desc = tree.css_first("div#description")
        desc_text = ""
        if desc is not None:
            node = desc.child
            while node is not None and not desc_text:
                if node.tag == "-text":
                    desc_text = clean(node.text_content)
                node = node.next
            desc_text = desc_text or clean(desc.text())
        cover = tree.css_first("img#cover")
        return MangaDetails(
            id=manga_id,
            title=name,
            cover_url=self.abs_url((cover.attributes.get("src") or "") if cover is not None else ""),
            description=desc_text,
            genres=[g for g in genres if g],
            authors=[clean(author.text())] if author is not None and clean(author.text()) else [],
            status=status,
            type=type_from_text(" ".join(a.text() for a in type_a)) if type_a else None,
            web_url=self.abs_url(manga_id),
        )

    def parse_chapters(self, tree: HTMLParser) -> list[Chapter]:
        out = []
        for i, tr in enumerate(tree.css("table.table_cha tr")):
            if i < 2:
                continue  # header rows
            a = tr.css_first("a")
            if a is None:
                continue
            name = clean(a.text())
            m = _NUM.search(name)
            v = _VOL.search(name)
            date_el = tr.css_first("div.date")
            date = None
            if date_el is not None:
                try:
                    date = datetime.strptime(clean(date_el.text()), "%Y-%m-%d").replace(tzinfo=UTC)
                except ValueError:
                    date = None
            out.append(Chapter(id=self._rel(a.attributes.get("href") or ""), title=name,
                               number=float(m.group(2)) if m else None, volume=v.group(1) if v else None,
                               uploaded_at=date))
        return out

    async def pages(self, chapter_id: str) -> list[PageImage]:
        html = await self.http.get_text(self.abs_url(chapter_id))
        start = html.find('fullimg":[')
        if start < 0:
            raise SourceParseError(self.info.id, "fullimg not found")
        start += len('fullimg":[')
        end = html.find("]", start)
        urls = [u.strip().strip("\"'") for u in html[start:end].split(",")]
        return [PageImage(index=i, url=self.abs_url(u), headers=self.image_headers(u))
                for i, u in enumerate(x for x in urls if x)]


class MangaChanAdapter(MultiChanAdapter):
    info = SourceInfo(
        id="mangachan",
        name="Manga-Chan",
        base_url="https://im.manga-chan.me",
        capabilities=CHAN_CAPS,
        notes="HTML (DLE). Старейший каталог; домен меняется — SOURCE_MANGACHAN_BASE_URL.",
        origin="keiyoushi/extensions-source",
    )


class YaoiChanAdapter(MultiChanAdapter):
    info = SourceInfo(
        id="yaoichan",
        name="Yaoi-Chan",
        base_url="https://yaoi-chan.me",
        is_nsfw=True,
        capabilities=CHAN_CAPS,
        notes="18+, яой. Движок Manga-Chan.",
        origin="keiyoushi/extensions-source",
    )
