"""MangaLib (Lib family JSON API).

Port of ``lib/lib_family_base.js`` (Apache-2.0). The API requires ``Site-Id``,
``Origin`` and ``Referer`` headers. 18+/Pro chapters need the operator's own
Bearer token; without it the API simply omits them.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..base import SourceAdapter
from ..errors import SourceAuthRequiredError, SourceError, SourceParseError
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

_GENRES = [
    ("Арт", 32), ("Безумие", 91), ("Боевик", 34), ("Боевые искусства", 35), ("Вампиры", 36),
    ("Военное", 89), ("Гарем", 37), ("Гендерная интрига", 38), ("Героическое фэнтези", 39),
    ("Демоны", 81), ("Детектив", 40), ("Драма", 43), ("Игра", 44), ("Исэкай", 79),
    ("История", 45), ("Киберпанк", 46), ("Комедия", 47), ("Космос", 83), ("Магия", 85),
    ("Меха", 49), ("Мистика", 50), ("Музыка", 80), ("Научная фантастика", 51),
    ("Повседневность", 52), ("Полиция", 82), ("Постапокалиптика", 53), ("Приключения", 54),
    ("Психология", 55), ("Романтика", 56), ("Сверхъестественное", 58), ("Сёдзё", 59),
    ("Спорт", 63), ("Супер сила", 87), ("Сэйнэн", 64), ("Трагедия", 65), ("Триллер", 66),
    ("Ужасы", 67), ("Фантастика", 68), ("Фэнтези", 69), ("Школа", 70),
]
_TYPES = {MangaType.MANGA: "1", MangaType.MANHWA: "5", MangaType.MANHUA: "6", MangaType.COMIC: "9"}
_TYPE_BACK = {"Манга": MangaType.MANGA, "Манхва": MangaType.MANHWA, "Маньхуа": MangaType.MANHUA,
              "Комикс": MangaType.COMIC}
_STATUSES = {
    PublicationStatus.ONGOING: "1", PublicationStatus.COMPLETED: "2",
    PublicationStatus.ANNOUNCED: "3", PublicationStatus.HIATUS: "4",
    PublicationStatus.CANCELLED: "5",
}
_STATUS_BACK = {"Онгоинг": PublicationStatus.ONGOING, "Завершён": PublicationStatus.COMPLETED,
                "Завершен": PublicationStatus.COMPLETED, "Анонс": PublicationStatus.ANNOUNCED,
                "Приостановлен": PublicationStatus.HIATUS, "Выпуск прекращён": PublicationStatus.CANCELLED}
_SORT = {SortOrder.POPULAR: "rate_avg", SortOrder.RATING: "rate_avg", SortOrder.UPDATED: "last_chapter_at",
         SortOrder.CREATED: "created_at", SortOrder.NAME: "rus_name"}
_FALLBACK_SERVERS = ["https://img3.mixlib.me", "https://img2.imglib.info"]


def prosemirror_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(prosemirror_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    inner = prosemirror_text(node.get("content") or [])
    if t in ("paragraph", "heading"):
        return inner + "\n\n"
    return inner


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, dict):
        if v.get("type") or v.get("content"):
            return prosemirror_text(v).strip()
        for k in ("text", "name", "label", "value"):
            if v.get(k) is not None:
                return str(v[k])
    return ""


class MangaLibAdapter(SourceAdapter):
    site_id = 1
    info = SourceInfo(
        id="mangalib",
        name="MangaLib",
        base_url="https://mangalib.org",
        capabilities=SourceCapabilities(
            search=True, genre_filter=True, genre_exclude=True, type_filter=True, status_filter=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED, SortOrder.NAME),
        ),
        genres=tuple(Genre(str(i), n) for n, i in _GENRES),
        notes="JSON API api2.mangalib.me. 18+ главы — только с токеном оператора (SOURCE_MANGALIB_TOKEN).",
    )
    api_url = "https://api2.mangalib.me/api"

    def default_headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "Site-Id": str(self.site_id),
            "Origin": self.info.base_url,
            "Referer": self.info.base_url + "/",
        }
        return h

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.info.base_url + "/", "Origin": self.info.base_url}

    @property
    def api(self) -> str:
        return self.config.extra.get("api_url", self.api_url).rstrip("/")

    # ------------------------------------------------------------ catalog
    def _parse_list(self, j: dict[str, Any]) -> MangaListPage:
        items = []
        for m in j.get("data") or []:
            slug = _s(m.get("slug_url") or m.get("slug"))
            if not slug:
                continue
            cover = m.get("cover") or {}
            items.append(MangaSummary(
                id=slug,
                title=_s(m.get("rus_name") or m.get("eng_name") or m.get("name")),
                cover_url=_s(cover.get("thumbnail") or cover.get("default")),
                type=_TYPE_BACK.get(_s((m.get("type") or {}).get("label"))),
            ))
        meta = j.get("meta") or {}
        if "has_next_page" in meta:
            has_next = bool(meta["has_next_page"])
        elif meta.get("current_page") is not None and meta.get("last_page") is not None:
            has_next = meta["current_page"] < meta["last_page"]
        else:
            has_next = len(items) >= 60
        return MangaListPage(items=items, has_next=has_next)

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        params: list[tuple[str, str]] = [("page", str(query.page)), ("site_id[]", str(self.site_id))]
        if query.query:
            params.append(("q", query.query))
        else:
            params.append(("sort_by", _SORT.get(query.sort, "rate_avg")))
        params += [("genres[]", g) for g in query.genres_include]
        params += [("genres_exclude[]", g) for g in query.genres_exclude]
        params += [("types[]", _TYPES[t]) for t in query.types if t in _TYPES]
        params += [("status[]", _STATUSES[s]) for s in query.statuses if s in _STATUSES]
        j = await self._get(f"{self.api}/manga", params=params)
        return self._parse_list(j)

    async def _get(self, url: str, **kw: Any) -> Any:
        if not self.config.token:
            return await self.http.get_json(url, **kw)
        try:
            return await self.http.get_json(
                url, headers={"Authorization": "Bearer " + self.config.token}, **kw
            )
        except SourceAuthRequiredError:
            # Token issued for another Lib site → "audience mismatch". Public data works without it.
            return await self.http.get_json(url, **kw)

    # ------------------------------------------------------------ title
    async def details(self, manga_id: str) -> MangaDetails:
        params = [("fields[]", f) for f in ("summary", "genres", "authors", "artists", "otherNames",
                                             "chap_count", "ageRestriction")]
        j = await self._get(f"{self.api}/manga/{quote(manga_id)}", params=params)
        d = j.get("data") or {}
        if not d:
            raise SourceParseError(self.info.id, "empty manga payload")
        cover = d.get("cover") or {}
        status_label = _s((d.get("status") or {}).get("label"))
        age = _s((d.get("ageRestriction") or {}).get("label")) or None
        return MangaDetails(
            id=manga_id,
            title=_s(d.get("rus_name") or d.get("eng_name") or d.get("name")) or manga_id,
            alt_titles=[x for x in [_s(d.get("eng_name")), _s(d.get("name"))] + [_s(o) for o in d.get("otherNames") or []] if x],
            cover_url=_s(cover.get("default") or cover.get("thumbnail")),
            description=_s(d.get("summary") or d.get("description")).strip(),
            genres=[_s(g.get("name")) for g in d.get("genres") or [] if g],
            authors=[_s(a.get("name")) for a in d.get("authors") or [] if a],
            artists=[_s(a.get("name")) for a in d.get("artists") or [] if a],
            status=_STATUS_BACK.get(status_label, PublicationStatus.UNKNOWN),
            type=_TYPE_BACK.get(_s((d.get("type") or {}).get("label"))),
            web_url=f"{self.info.base_url}/ru/manga/{manga_id}",
            age_rating=age,
        )

    async def chapters(self, manga_id: str) -> list[Chapter]:
        j = await self._get(f"{self.api}/manga/{quote(manga_id)}/chapters")
        out: list[Chapter] = []
        for c in j.get("data") or []:
            branches = c.get("branches") or [{}]
            br = branches[0] if branches else {}
            ref = f"{manga_id}|{c.get('number')}|{c.get('volume')}"
            if br.get("branch_id"):
                ref += f"|{br['branch_id']}"
            number = parse_float(c.get("number"))
            vol = _s(c.get("volume"))
            teams = ", ".join(_s(t.get("name")) for t in br.get("teams") or [] if t) or None
            out.append(Chapter(
                id=ref,
                title=chapter_title(number, _s(c.get("name")) or None),
                number=number,
                volume=vol if vol and vol != "0" else None,
                uploaded_at=parse_iso(_s(br.get("created_at"))),
                scanlator=teams,
            ))
        out.reverse()  # newest first, like every other adapter
        return out

    # ------------------------------------------------------------ pages
    async def pages(self, chapter_id: str) -> list[PageImage]:
        parts = chapter_id.split("|")
        if len(parts) < 3:
            raise SourceParseError(self.info.id, "bad chapter id")
        slug, number, volume = parts[0], parts[1], parts[2]
        params = [("number", number), ("volume", volume)]
        if len(parts) > 3 and parts[3]:
            params.append(("branch_id", parts[3]))
        j = await self._get(f"{self.api}/manga/{quote(slug)}/chapter", params=params)
        pages = (j.get("data") or {}).get("pages") or []
        if not pages:
            raise SourceAuthRequiredError(self.info.id, "chapter has no pages (paid / 18+ / removed)")
        server = await self._image_server()
        result = []
        for i, p in enumerate(pages):
            path = re.sub(r"^/+", "/", _s(p.get("url")))
            if not path:
                continue
            url = path if path.startswith("http") else server + path
            result.append(PageImage(index=i, url=url, headers=self.image_headers(url)))
        return result

    async def _image_server(self) -> str:
        preferred = self.config.extra.get("image_server", "compress")
        try:
            j = await self.http.get_json(f"{self.api}/constants", params=[("fields[]", "imageServers")])
            servers = [s for s in (j.get("data") or {}).get("imageServers") or []
                       if self.site_id in (s.get("site_ids") or []) and s.get("url")]
            for s in servers:
                if s.get("id") == preferred:
                    return s["url"].rstrip("/")
            if servers:
                return servers[0]["url"].rstrip("/")
        except SourceError:
            pass
        return _FALLBACK_SERVERS[0]
