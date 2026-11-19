"""MangaPill — English HTML site. Port of keiyoushi src/en/mangapill (Apache-2.0)."""
from __future__ import annotations

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
from ..utils import clean, parse_float, type_from_text


class MangaPillAdapter(SourceAdapter):
    info = SourceInfo(
        id="mangapill",
        name="MangaPill",
        base_url="https://mangapill.com",
        lang="en",
        capabilities=SourceCapabilities(search=True, sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED)),
        notes="Английские сканы, быстрое обновление. Популярное = «Trending» с главной.",
        origin="keiyoushi/extensions-source",
    )

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.base_url + "/"}

    def _cards(self, nodes) -> list[MangaSummary]:
        out, seen = [], set()
        for card in nodes:
            a = card.css_first("a[href^='/manga/']")
            t = card.css_first("div.line-clamp-2")
            if a is None or t is None:
                continue
            href = a.attributes.get("href") or ""
            if href in seen:
                continue
            seen.add(href)
            img = card.css_first("img")
            out.append(MangaSummary(id=href, title=clean(t.text()),
                                    cover_url=(img.attributes.get("data-src") or img.attributes.get("src") or "") if img else ""))
        return out

    @staticmethod
    def _grid_cards(tree):
        # ".grid > div:not([class])"
        for grid in tree.css(".grid"):
            for child in grid.iter():
                if child.tag == "div" and not child.attributes.get("class"):
                    yield child

    async def popular(self, page: int = 1) -> MangaListPage:
        if page > 1:
            return MangaListPage(items=[], has_next=False)
        tree = HTMLParser(await self.http.get_text(f"{self.base_url}/"))
        nodes = []
        for h4 in tree.css("h4"):
            if "Trending" in h4.text():
                section = h4.parent
                while section is not None and section.css_first(".grid") is None:
                    section = section.parent
                if section is not None:
                    nodes = list(self._grid_cards(section))
                break
        return MangaListPage(items=self._cards(nodes), has_next=False)

    async def latest(self, page: int = 1) -> MangaListPage:
        if page > 1:
            return MangaListPage(items=[], has_next=False)
        tree = HTMLParser(await self.http.get_text(f"{self.base_url}/chapters"))
        return MangaListPage(items=self._cards(self._grid_cards(tree)), has_next=False)

    async def search(self, query: SearchQuery) -> MangaListPage:
        if not query.query.strip():
            return await (self.latest(query.page) if query.sort == SortOrder.UPDATED else self.popular(query.page))
        tree = HTMLParser(await self.http.get_text(
            f"{self.base_url}/search?page={query.page}&q={quote(query.query.strip())}"))
        items = self._cards(self._grid_cards(tree))
        return MangaListPage(items=items, has_next=tree.css_first("a.btn.btn-sm") is not None and bool(items))

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        tree = HTMLParser(await self.http.get_text(self.abs_url(manga_id)))
        h1 = tree.css_first("h1")
        if h1 is None:
            raise SourceParseError(self.info.id, "title not found")
        img = tree.css_first("div.container img")
        desc = tree.css_first("div.container p.text-sm") or tree.css_first("div.container p")
        labels: dict[str, str] = {}
        for label in tree.css("label.text-secondary"):
            sib = label.next
            while sib is not None and sib.tag == "-text":
                sib = sib.next
            labels[clean(label.text()).lower()] = clean(sib.text()) if sib is not None else ""
        st = labels.get("status", "").lower()
        status = (PublicationStatus.ONGOING if "publishing" in st
                  else PublicationStatus.COMPLETED if "finished" in st else PublicationStatus.UNKNOWN)
        alt = tree.css_first("div.container h1 + div")
        details = MangaDetails(
            id=manga_id,
            title=clean(h1.text()),
            cover_url=(img.attributes.get("data-src") or img.attributes.get("src") or "") if img else "",
            alt_titles=[clean(alt.text())] if alt is not None and clean(alt.text()) else [],
            description=desc.text().strip() if desc is not None else "",
            genres=[clean(a.text()) for a in tree.css("a[href*='genre']") if clean(a.text())],
            status=status,
            type=type_from_text(labels.get("type")),
            web_url=self.abs_url(manga_id),
        )
        chapters = []
        for a in tree.css("#chapters a"):
            name = clean(a.text())
            num = parse_float(name.split("Chapter", 1)[-1]) if "Chapter" in name else None
            chapters.append(Chapter(id=a.attributes.get("href") or "", title=name, number=num))
        return details, chapters

    async def details(self, manga_id: str) -> MangaDetails:
        return (await self.title(manga_id))[0]

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return (await self.title(manga_id))[1]

    async def pages(self, chapter_id: str) -> list[PageImage]:
        tree = HTMLParser(await self.http.get_text(self.abs_url(chapter_id)))
        urls = [img.attributes.get("data-src") or img.attributes.get("src") or "" for img in tree.css("picture img")]
        return [PageImage(index=i, url=u, headers=self.image_headers(u)) for i, u in enumerate(x for x in urls if x)]
