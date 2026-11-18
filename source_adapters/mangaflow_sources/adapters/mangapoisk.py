"""MangaPoisk (HTML). Port of keiyoushi/extensions-source src/ru/mangapoisk (Apache-2.0).

The domain moves from time to time (mangapoisk.ru → .live → .me): override with
``SOURCE_MANGAPOISK_BASE_URL``. Chapters removed at the rights holder's request
are simply absent.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode

from ..base import SourceAdapter
from ..errors import SourceAuthRequiredError, SourceParseError
from ..models import (
    Chapter,
    Genre,
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
from ..utils import chapter_title, clean

# sortBy values as the site expects them (descending unless noted)
_SORT = {
    SortOrder.POPULAR: "popular",
    SortOrder.UPDATED: "-last_chapter_at",
    SortOrder.CREATED: "-published_at",
    SortOrder.NAME: "-name",  # the site inverts the prefix for name/popular: "-name" = A→Я
}
_STATUS = {PublicationStatus.ONGOING: "0", PublicationStatus.COMPLETED: "1"}
_GENRES = (
    ("Арт", "7332"), ("Боевик", "3"), ("Боевые искусства", "31"), ("Вампиры", "10"), ("Гарем", "29"),
    ("Гендерная интрига", "172"), ("Героическое фэнтези", "30"), ("Детектив", "121"), ("Дзёсэй", "230"),
    ("Додзинси", "1785"), ("Драма", "6"), ("Игра", "105"), ("Исэкай", "8120"), ("История", "123"),
    ("Киберпанк", "355"), ("Комедия", "4"), ("Кодомо", "1789"), ("Махо-сёдзё", "1472"), ("Меха", "356"),
    ("Музыка", "34948"), ("Фантастика", "171"), ("Образование", "987"), ("Омегаверс", "7514"),
    ("Пародия", "34402"), ("Повседневность", "18"), ("Постапокалиптика", "310"), ("Приключения", "1"),
    ("Психология", "38"), ("Романтика", "2"), ("Самурайский боевик", "916"), ("Сверхъестественное", "5"),
    ("Сёдзё", "57"), ("Сёдзё-ай", "147"), ("Сёнэн", "8"), ("Сэйнэн", "12"), ("Спорт", "160"),
    ("Триллер", "120"), ("Трагедия", "122"), ("Уся", "10128"), ("Ужасы", "260"), ("Фэнтези", "7"),
    ("Школа", "11"), ("Сянься", "9321"),
)
_CHAPTER_RE = re.compile(r"Глава\s+(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MAX_CHAPTER_PAGES = 60


def parse_poisk_date(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """"5 минут назад" / "3 часа назад" / "2 дня назад" / "12 марта 2024"."""
    if not text:
        return None
    now = now or datetime.now(UTC)
    t = text.strip().lower()
    m = re.match(r"(\d+)\s+(\S+)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("минут"):
            return now - timedelta(minutes=n)
        if unit.startswith("час"):
            return now - timedelta(hours=n)
        if unit.startswith(("дня", "дней", "день")):
            return now - timedelta(days=n)
    m = re.match(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", t)
    if m and m.group(2) in _MONTHS:
        try:
            return datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), tzinfo=UTC)
        except ValueError:
            return None
    if "сегодня" in t:
        return now
    if "вчера" in t:
        return now - timedelta(days=1)
    return None


class MangaPoiskAdapter(SourceAdapter):
    info = SourceInfo(
        id="mangapoisk",
        name="MangaPoisk",
        base_url="https://mangapoisk.me",
        capabilities=SourceCapabilities(
            search=True, genre_filter=True, genre_exclude=True, status_filter=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.NAME),
        ),
        genres=tuple(Genre(i, n) for n, i in _GENRES),
        notes="HTML. Домен меняется — SOURCE_MANGAPOISK_BASE_URL. Поиск по названию от 3 символов.",
        origin="keiyoushi/extensions-source",
    )

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Referer": self.base_url + "/"}

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.base_url + "/"}

    @staticmethod
    def _img(el: LexborNode | None) -> str:
        if el is None:
            return ""
        return (el.attributes.get("data-src") or el.attributes.get("src") or "").strip()

    def _ref(self, href: str) -> str:
        ref = href.replace(self.base_url, "")
        return re.sub(r"^https?://[^/]+", "", ref)

    # ------------------------------------------------------------ lists
    def parse_list(self, html: str, *, search: bool) -> MangaListPage:
        tree = HTMLParser(html)
        items: list[MangaSummary] = []
        seen: set[str] = set()
        for card in tree.css("article.card" if search else ".manga-card"):
            link = card.css_first("a.card-about") if search else card.css_first("a")
            if link is None:
                continue
            href = link.attributes.get("href") or ""
            if search:
                t = card.css_first("div.post-description p.card-title") or card.css_first("a > h2.entry-title")
                title = clean(t.text()) if t else ""
            else:
                title = (link.attributes.get("title") or "").strip()
            title = title.split("/")[0].strip()
            if not href or not title or href in seen:
                continue
            seen.add(href)
            items.append(MangaSummary(
                id=self._ref(href), title=title, cover_url=self.abs_url(self._img(card.css_first("a > img"))),
            ))
        if search:
            has_next = any(
                "Вперёд" in (a.attributes.get("aria-label") or "") and a.attributes.get("aria-disabled") != "true"
                for a in tree.css("ul.pagination li a")
            )
        else:
            has_next = any("Вперёд" in li.text() and li.css_first("a") is not None for li in tree.css("ul li"))
        return MangaListPage(items=items, has_next=has_next)

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        q = query.query.strip()
        if q:
            if len(q) < 3:
                return MangaListPage(items=[], has_next=False)
            url = f"{self.base_url}/search?{urlencode({'q': q, 'page': query.page})}"
            return self.parse_list(await self.http.get_text(url), search=True)
        params: dict[str, str] = {"sortBy": _SORT.get(query.sort, "popular"), "page": str(query.page)}
        statuses = [_STATUS[s] for s in query.statuses if s in _STATUS]
        if statuses:
            params["translated"] = f"[{','.join(statuses)}]"
        if query.genres_include:
            params["genres"] = f"[{','.join(query.genres_include)}]"
        if query.genres_exclude:
            params["genres-exclude"] = f"[{','.join(query.genres_exclude)}]"
        url = f"{self.base_url}/manga?{urlencode(params)}"
        return self.parse_list(await self.http.get_text(url), search=False)

    # ------------------------------------------------------------ title
    async def details(self, manga_id: str) -> MangaDetails:
        return self.parse_details(manga_id, await self.http.get_text(self.abs_url(manga_id)))

    def parse_details(self, manga_id: str, html: str) -> MangaDetails:
        tree = HTMLParser(html)
        info = next((c for c in tree.css("div.card") if c.css_first("header") is not None), None)
        if info is None:
            raise SourceParseError(self.info.id, "manga info not found")
        title_el = info.css_first(".text-base span")
        title = clean(title_el.text()) if title_el else ""
        if not title:
            og = tree.css_first("meta[property='og:title']")
            title = (og.attributes.get("content") if og else "") or manga_id
        genres: list[str] = []
        status = PublicationStatus.UNKNOWN
        for span in info.css("span"):
            text = span.text()
            if "Жанр:" in text and not genres:
                genres = [clean(a.text()) for a in span.css("a") if clean(a.text())]
            elif "Статус:" in text and status == PublicationStatus.UNKNOWN:
                if "Завершена" in text:
                    status = PublicationStatus.COMPLETED
                elif "Выпускается" in text:
                    status = PublicationStatus.ONGOING
        desc = info.css_first(".manga-description")
        cover = info.css_first("img.w-full")
        return MangaDetails(
            id=manga_id,
            title=title.split("/")[0].strip(),
            alt_titles=[clean(x) for x in title.split("/")[1:] if clean(x)],
            cover_url=self.abs_url(self._img(cover)),
            description=desc.text(strip=False).strip() if desc else "",
            genres=genres,
            status=status,
            web_url=self.abs_url(manga_id),
        )

    def parse_chapter_items(self, html: str) -> tuple[list[Chapter], int]:
        tree = HTMLParser(html)
        out: list[Chapter] = []
        for el in tree.css(".chapter-item"):
            title_el = el.css_first("span.chapter-title")
            link = el.css_first("a")
            if title_el is None or link is None:
                continue
            href = link.attributes.get("href") or ""
            if not href:
                continue
            raw = clean(title_el.text())
            m = _CHAPTER_RE.search(raw)
            number = float(m.group(1).replace(",", ".")) if m else None
            name = clean(link.text()) or raw
            date_el = el.css_first("span.chapter-date")
            out.append(Chapter(
                id=self._ref(href),
                title=name if number is None or "Глава" in name else chapter_title(number, name),
                number=number,
                uploaded_at=parse_poisk_date(date_el.text() if date_el else None),
            ))
        last = max((int(t) for li in tree.css("li.page-item") if (t := li.text().strip()).isdigit()), default=1)
        return out, last

    async def chapters(self, manga_id: str) -> list[Chapter]:
        base = self.abs_url(manga_id).rstrip("/")
        first, last = self.parse_chapter_items(await self.http.get_text(f"{base}/chaptersList"))
        out = list(first)
        for page in range(2, min(last, MAX_CHAPTER_PAGES) + 1):
            more, _ = self.parse_chapter_items(await self.http.get_text(f"{base}/chaptersList?page={page}"))
            out.extend(more)
        return out

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        return await self.details(manga_id), await self.chapters(manga_id)

    # ------------------------------------------------------------ reader
    async def pages(self, chapter_id: str) -> list[PageImage]:
        html = await self.http.get_text(self.abs_url(chapter_id))
        if "text-error-500-400-token" in html:
            raise SourceAuthRequiredError(self.info.id, "chapter removed at the rights holder's request")
        tree = HTMLParser(html)
        urls = [self.abs_url(self._img(img)) for img in tree.css("img.page-image")]
        urls = [u for u in urls if u]
        if not urls:
            raise SourceParseError(self.info.id, "no pages in chapter")
        return [PageImage(index=i, url=u, headers=self.image_headers(u)) for i, u in enumerate(urls)]
