"""MangaDex — official public JSON API (https://api.mangadex.org/docs/).

English by default; the translation language is configurable with
``SOURCE_MANGADEX_EXTRA_LANG`` (e.g. ``ru``). Chapters hosted off-site
(official publishers' external links) are skipped — they can't be read here.

Images come from ``uploads.mangadex.org`` (stable URLs; MangaDex@Home node
URLs expire after ~15 minutes and would break cached page lists).
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from ..base import SourceAdapter
from ..errors import SourceParseError
from ..models import (
    Chapter,
    Genre,
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
FEED_PAGE = 500
MAX_OFFSET = 10_000  # API hard limit for offset + limit
UPLOADS = "https://uploads.mangadex.org"

_SORT = {
    SortOrder.POPULAR: ("order[followedCount]", "desc"),
    SortOrder.RATING: ("order[rating]", "desc"),
    SortOrder.UPDATED: ("order[latestUploadedChapter]", "desc"),
    SortOrder.CREATED: ("order[createdAt]", "desc"),
    SortOrder.NAME: ("order[title]", "asc"),
}
_STATUS = {
    "ongoing": PublicationStatus.ONGOING,
    "completed": PublicationStatus.COMPLETED,
    "hiatus": PublicationStatus.HIATUS,
    "cancelled": PublicationStatus.CANCELLED,
}
_STATUS_BACK = {v: k for k, v in _STATUS.items()}
_ORIGIN = {
    MangaType.MANGA: ["ja"],
    MangaType.MANHWA: ["ko"],
    MangaType.MANHUA: ["zh", "zh-hk"],
    MangaType.COMIC: ["en"],
}

# Genre ids are MangaDex tag *names* (resolved to tag UUIDs at runtime via /manga/tag);
# display names are Russian so the app's global genre search maps onto them.
_GENRES = (
    ("Action", "Боевик"), ("Martial Arts", "Боевые искусства"), ("Harem", "Гарем"), ("Mystery", "Детектив"),
    ("Drama", "Драма"), ("Isekai", "Исэкай"), ("Comedy", "Комедия"), ("Magic", "Магия"), ("Mecha", "Меха"),
    ("Supernatural", "Мистика"), ("Slice of Life", "Повседневность"), ("Adventure", "Приключения"),
    ("Psychological", "Психология"), ("Romance", "Романтика"), ("Supernatural", "Сверхъестественное"),
    ("Sports", "Спорт"), ("Tragedy", "Трагедия"), ("Thriller", "Триллер"), ("Horror", "Ужасы"),
    ("Sci-Fi", "Фантастика"), ("Fantasy", "Фэнтези"), ("School Life", "Школа"), ("Historical", "История"),
    ("Crime", "Криминал"), ("Medical", "Медицина"), ("Military", "Военное"), ("Survival", "Выживание"),
    ("Reincarnation", "Перерождение"), ("Time Travel", "Путешествия во времени"), ("Monsters", "Монстры"),
    ("Cooking", "Кулинария"), ("Music", "Музыка"), ("Video Games", "Видеоигры"), ("Villainess", "Злодейка"),
    ("Boys' Love", "Яой"), ("Girls' Love", "Юри"), ("Superhero", "Супергерои"), ("Zombies", "Зомби"),
)


class MangaDexAdapter(SourceAdapter):
    info = SourceInfo(
        id="mangadex",
        name="MangaDex",
        base_url="https://mangadex.org",
        lang="en",
        capabilities=SourceCapabilities(
            search=True, genre_filter=True, genre_exclude=True, type_filter=True, status_filter=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.RATING, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.NAME),
        ),
        genres=tuple(Genre(en, ru) for en, ru in _GENRES),
        notes="Официальный API. Английские переводы (язык меняется в SOURCE_MANGADEX_EXTRA_LANG).",
        origin="mangaflow",
    )
    api_url = "https://api.mangadex.org"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tags: dict[str, str] | None = None
        self._tags_lock = asyncio.Lock()

    def default_headers(self) -> dict[str, str]:
        # MangaDex asks API clients to identify themselves.
        return {"Accept": "application/json", "User-Agent": "MangaFlow/1.0 (+https://github.com/KOTBCTAKAHE/mangaflow-backend)"}

    def image_headers(self, url: str) -> dict[str, str]:
        return {"User-Agent": self.default_headers()["User-Agent"]}

    @property
    def api(self) -> str:
        return self.config.extra.get("api_url", self.api_url).rstrip("/")

    @property
    def lang(self) -> str:
        return self.config.extra.get("lang", "en")

    def _ratings(self) -> list[tuple[str, str]]:
        ratings = ["safe", "suggestive"] + (["erotica"] if self.config.extra.get("nsfw") == "1" else [])
        return [("contentRating[]", r) for r in ratings]

    # ------------------------------------------------------------ helpers
    def _pick(self, loc: dict[str, str] | None) -> str:
        if not loc:
            return ""
        return loc.get(self.lang) or loc.get("en") or loc.get("ja-ro") or next(iter(loc.values()), "")

    def _title(self, attrs: dict[str, Any]) -> str:
        title = attrs.get("title") or {}
        if self.lang in title or "en" in title:
            return self._pick(title)
        for alt in attrs.get("altTitles") or []:
            if self.lang in alt or "en" in alt:
                return self._pick(alt)
        return self._pick(title)

    @staticmethod
    def _cover(manga_id: str, rels: list[dict[str, Any]]) -> str:
        for r in rels:
            if r.get("type") == "cover_art":
                fn = (r.get("attributes") or {}).get("fileName")
                if fn:
                    return f"{UPLOADS}/covers/{manga_id}/{fn}.512.jpg"
        return ""

    @staticmethod
    def _type(attrs: dict[str, Any]) -> MangaType:
        lang = attrs.get("originalLanguage") or ""
        if lang == "ja":
            return MangaType.MANGA
        if lang == "ko":
            return MangaType.MANHWA
        if lang.startswith("zh"):
            return MangaType.MANHUA
        if lang == "en":
            return MangaType.COMIC
        return MangaType.OTHER

    async def _tag_ids(self) -> dict[str, str]:
        """English tag name (lower-case) → UUID, fetched once per process."""
        if self._tags is not None:
            return self._tags
        async with self._tags_lock:
            if self._tags is None:
                j = await self.http.get_json(f"{self.api}/manga/tag")
                self._tags = {
                    ((t.get("attributes") or {}).get("name") or {}).get("en", "").lower(): t.get("id", "")
                    for t in j.get("data") or []
                }
        return self._tags

    async def _resolve_tags(self, names: list[str]) -> list[str]:
        if not names:
            return []
        tags = await self._tag_ids()
        return [tags[n.lower()] for n in names if n.lower() in tags]

    def _parse_list(self, j: dict[str, Any], offset: int) -> MangaListPage:
        items = []
        for m in j.get("data") or []:
            mid = m.get("id")
            if not mid:
                continue
            attrs = m.get("attributes") or {}
            items.append(MangaSummary(
                id=mid,
                title=self._title(attrs),
                cover_url=self._cover(mid, m.get("relationships") or []),
                type=self._type(attrs),
            ))
        total = int(j.get("total") or 0)
        nxt = offset + PAGE_SIZE
        return MangaListPage(items=items, has_next=nxt < total and nxt + PAGE_SIZE <= MAX_OFFSET)

    # ------------------------------------------------------------ catalog
    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        offset = (max(1, query.page) - 1) * PAGE_SIZE
        if offset + PAGE_SIZE > MAX_OFFSET:
            return MangaListPage(items=[], has_next=False)
        params: list[tuple[str, str]] = [
            ("limit", str(PAGE_SIZE)),
            ("offset", str(offset)),
            ("includes[]", "cover_art"),
            ("availableTranslatedLanguage[]", self.lang),
            ("hasAvailableChapters", "true"),
            *self._ratings(),
        ]
        if query.query:
            params.append(("title", query.query))
            params.append(("order[relevance]", "desc"))
        else:
            params.append(_SORT.get(query.sort, _SORT[SortOrder.POPULAR]))
        params += [("includedTags[]", t) for t in await self._resolve_tags(query.genres_include)]
        params += [("excludedTags[]", t) for t in await self._resolve_tags(query.genres_exclude)]
        for t in query.types:
            params += [("originalLanguage[]", lang) for lang in _ORIGIN.get(t, [])]
        params += [("status[]", _STATUS_BACK[s]) for s in query.statuses if s in _STATUS_BACK]
        j = await self.http.get_json(f"{self.api}/manga", params=params)
        return self._parse_list(j, offset)

    # ------------------------------------------------------------ title
    async def details(self, manga_id: str) -> MangaDetails:
        j = await self.http.get_json(
            f"{self.api}/manga/{quote(manga_id)}",
            params=[("includes[]", "cover_art"), ("includes[]", "author"), ("includes[]", "artist")],
        )
        m = j.get("data") or {}
        if not m:
            raise SourceParseError(self.info.id, "empty manga payload")
        attrs = m.get("attributes") or {}
        rels = m.get("relationships") or []
        title = self._title(attrs)
        alts = []
        for loc in [attrs.get("title") or {}] + list(attrs.get("altTitles") or []):
            for v in loc.values():
                if v and v != title and v not in alts:
                    alts.append(v)

        def people(kind: str) -> list[str]:
            return [n for r in rels if r.get("type") == kind and (n := (r.get("attributes") or {}).get("name"))]

        rating = attrs.get("contentRating")
        return MangaDetails(
            id=manga_id,
            title=title or manga_id,
            alt_titles=alts[:12],
            cover_url=self._cover(manga_id, rels),
            description=self._pick(attrs.get("description") or {}).strip(),
            genres=[
                self._pick((t.get("attributes") or {}).get("name") or {})
                for t in attrs.get("tags") or []
                if (t.get("attributes") or {}).get("group") in ("genre", "theme")
            ],
            authors=people("author"),
            artists=people("artist"),
            status=_STATUS.get(attrs.get("status") or "", PublicationStatus.UNKNOWN),
            type=self._type(attrs),
            web_url=f"{self.info.base_url}/title/{manga_id}",
            age_rating={"suggestive": "16+", "erotica": "18+", "pornographic": "18+"}.get(rating or ""),
        )

    async def chapters(self, manga_id: str) -> list[Chapter]:
        out: list[Chapter] = []
        seen: set[str] = set()
        offset = 0
        while offset < MAX_OFFSET:
            j = await self.http.get_json(
                f"{self.api}/manga/{quote(manga_id)}/feed",
                params=[
                    ("limit", str(FEED_PAGE)),
                    ("offset", str(offset)),
                    ("translatedLanguage[]", self.lang),
                    ("order[volume]", "desc"),
                    ("order[chapter]", "desc"),
                    ("includes[]", "scanlation_group"),
                    ("includeExternalUrl", "0"),
                    ("includeEmptyPages", "0"),
                    ("contentRating[]", "safe"), ("contentRating[]", "suggestive"),
                    ("contentRating[]", "erotica"), ("contentRating[]", "pornographic"),
                ],
            )
            data = j.get("data") or []
            for c in data:
                a = c.get("attributes") or {}
                if a.get("externalUrl") or not a.get("pages"):
                    continue
                num_raw = a.get("chapter")
                # several groups often translate the same chapter: keep the first one
                dedupe = f"{a.get('volume')}|{num_raw}" if num_raw else c.get("id", "")
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                number = parse_float(num_raw)
                groups = [
                    n for r in c.get("relationships") or []
                    if r.get("type") == "scanlation_group" and (n := (r.get("attributes") or {}).get("name"))
                ]
                out.append(Chapter(
                    id=c["id"],
                    title=chapter_title(number, a.get("title") or None, prefix="Chapter") if num_raw else (a.get("title") or "Oneshot"),
                    number=number,
                    volume=a.get("volume") or None,
                    uploaded_at=parse_iso(a.get("publishAt") or a.get("readableAt")),
                    scanlator=", ".join(groups) or None,
                ))
            offset += FEED_PAGE
            if offset >= int(j.get("total") or 0) or not data:
                break
        return out

    async def pages(self, chapter_id: str) -> list[PageImage]:
        j = await self.http.get_json(f"{self.api}/at-home/server/{quote(chapter_id)}")
        ch = j.get("chapter") or {}
        h, files = ch.get("hash"), ch.get("data") or []
        if not h or not files:
            raise SourceParseError(self.info.id, "chapter has no pages")
        return [
            PageImage(index=i, url=f"{UPLOADS}/data/{h}/{f}", headers=self.image_headers(""))
            for i, f in enumerate(files)
        ]


class MangaDexRuAdapter(MangaDexAdapter):
    """The same catalogue, Russian translations only."""

    info = SourceInfo(
        id="mangadex_ru",
        name="MangaDex (RU)",
        base_url="https://mangadex.org",
        lang="ru",
        capabilities=MangaDexAdapter.info.capabilities,
        genres=MangaDexAdapter.info.genres,
        notes="Официальный API MangaDex, только русские переводы.",
        origin="mangaflow",
    )

    @property
    def lang(self) -> str:
        return self.config.extra.get("lang", "ru")
