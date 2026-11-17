"""Senkuro (GraphQL). Port of sources/ru/manga/senkuro/senkuro.js (Apache-2.0)."""
from __future__ import annotations

from typing import Any

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

Q_MANGAS = """query fetchMangas($first: Int = 30, $after: String, $status: MangaStatusFilter, $type: MangaTypeFilter, $label: MangaLabelFilter, $orderField: MangaOrderField = POPULARITY_SCORE, $orderDirection: OrderDirection = DESC) {
  mangas(first: $first, after: $after, status: $status, type: $type, label: $label, orderBy: {field: $orderField, direction: $orderDirection}) {
    edges { node { id slug type rating titles { lang content } originalName { lang content } cover { main: resize(width: 300, height: 420, format: WEBP) { url } } } }
    pageInfo { hasNextPage endCursor }
  }
}"""

Q_SEARCH = """query search($query: String!, $first: Int = 30) {
  search(query: $query, type: MANGA, first: $first) {
    edges { node { __typename ... on SearchManga { id slug originalName titles { lang content } cover { id main: resize(width: 300, height: 420, format: WEBP) { url } } } } }
  }
}"""

Q_MANGA = """query fetchManga($slug: String!) {
  manga(slug: $slug) {
    id slug type status rating chapters
    originalName { lang content }
    titles { lang content }
    alternativeNames { lang content }
    localizations { lang description { __typename ... on TiptapNodeText { text } ... on TiptapNodeNestedBlock { content { __typename ... on TiptapNodeText { text } } } ... on TiptapNodeHeading { content { __typename ... on TiptapNodeText { text } } } } }
    labels { id slug titles { lang content } }
    mainStaff { roles person { name } }
    cover { main: resize(width: 600, height: 850, format: WEBP) { url } }
    branches { id lang chapters primaryBranch primaryTeamActivities { team { id name } } }
  }
}"""

Q_CHAPTERS = """query fetchMangaChapters($branchId: ID!, $after: String, $orderBy: MangaChapterOrder!) {
  mangaChapters(first: 100, branchId: $branchId, after: $after, orderBy: $orderBy) {
    edges { node { id slug name number volume createdAt } }
    pageInfo { hasNextPage endCursor }
  }
}"""

Q_CHAPTER = """query fetchMangaChapter($slug: String!, $cdnQuality: String) {
  mangaChapter(slug: $slug) {
    id slug
    pages(cdnQuality: $cdnQuality) { number image { original { url } compress: resize(width: 1200, quality: 80, format: WEBP) { url } } }
  }
}"""

GENRES = [
    ("action", "Экшен"), ("adventure", "Приключения"), ("comedy", "Комедия"), ("drama", "Драма"),
    ("fantasy", "Фэнтези"), ("historical", "Исторический"), ("horror", "Ужасы"), ("magic", "Магия"),
    ("mecha", "Меха"), ("military", "Военное"), ("music", "Музыка"), ("mystery", "Мистика"),
    ("psychological", "Психологическое"), ("reincarnation", "Реинкарнация"), ("romance", "Романтика"),
    ("school", "Школа"), ("sci_fi", "Фантастика"), ("seinen", "Сэйнэн"), ("shoujo", "Сёдзе"),
    ("shounen", "Сёнен"), ("slice_of_life", "Повседневность"), ("sport", "Спорт"),
    ("supernatural", "Сверхъестественное"), ("survival", "Выживание"), ("thriller", "Триллер"),
    ("josei", "Дзёсей"), ("crime", "Преступления"), ("space", "Космос"),
]
_SORT = {SortOrder.POPULAR: "POPULARITY_SCORE", SortOrder.RATING: "SCORE", SortOrder.UPDATED: "LAST_CHAPTER_AT",
         SortOrder.CREATED: "CREATED_AT", SortOrder.NAME: "POPULARITY_SCORE"}
_TYPES = {MangaType.MANGA: "MANGA", MangaType.MANHWA: "MANHWA", MangaType.MANHUA: "MANHUA", MangaType.COMIC: "COMICS"}
_TYPE_BACK = {v: k for k, v in _TYPES.items()}
_STATUS = {PublicationStatus.ONGOING: "ONGOING", PublicationStatus.COMPLETED: "FINISHED",
           PublicationStatus.HIATUS: "FROZEN", PublicationStatus.CANCELLED: "DROPPED",
           PublicationStatus.ANNOUNCED: "ANNOUNCEMENT"}
_STATUS_BACK = {v: k for k, v in _STATUS.items()}
_RATING = {"EXPLICIT": "18+", "QUESTIONABLE": "16+", "SENSITIVE": "12+", "GENERAL": "0+"}
MAX_CURSOR_WALK = 20


def _pick(titles: Any, original: Any = None) -> str:
    if isinstance(titles, list):
        for lang in ("RU", "EN"):
            for t in titles:
                if t and t.get("lang") == lang and t.get("content"):
                    return t["content"]
        if titles and titles[0].get("content"):
            return titles[0]["content"]
    if isinstance(original, dict):
        return original.get("content") or ""
    if isinstance(original, str):
        return original
    return ""


def tiptap_text(nodes: Any) -> str:
    if not nodes:
        return ""
    out = []
    for n in nodes if isinstance(nodes, list) else [nodes]:
        if not n:
            continue
        if n.get("__typename") == "TiptapNodeText" and n.get("text"):
            out.append(n["text"])
        elif isinstance(n.get("content"), list):
            inner = tiptap_text(n["content"])
            if inner:
                out.append(inner)
    return "\n".join(out).strip()


class SenkuroAdapter(SourceAdapter):
    info = SourceInfo(
        id="senkuro",
        name="Senkuro",
        base_url="https://senkuro.com",
        capabilities=SourceCapabilities(
            search=True, genre_filter=True, genre_exclude=True, type_filter=True, status_filter=True,
            sort_orders=(SortOrder.POPULAR, SortOrder.RATING, SortOrder.UPDATED, SortOrder.CREATED),
        ),
        genres=tuple(Genre(s, n) for s, n in GENRES),
        notes="GraphQL api.senkuro.com. Гео-блок для не-RU IP.",
    )
    api_url = "https://api.senkuro.com/graphql"

    def default_headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/graphql-response+json, application/json",
            "Content-Type": "application/json",
            "Origin": self.info.base_url,
            "Referer": self.info.base_url + "/",
        }
        if self.config.token:
            h["Authorization"] = "Bearer " + self.config.token.removeprefix("Bearer ").strip()
        return h

    async def gql(self, op: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        j = await self.http.post_json(self.api_url, {"operationName": op, "query": query, "variables": variables})
        if j.get("errors"):
            raise SourceParseError(self.info.id, f"GraphQL: {j['errors'][0].get('message')}")
        return j.get("data") or {}

    def _edges(self, edges: Any) -> list[MangaSummary]:
        out = []
        for e in edges or []:
            n = (e or {}).get("node") or {}
            if n.get("__typename") and n["__typename"] != "SearchManga":
                continue
            ref = n.get("slug") or n.get("id")
            title = _pick(n.get("titles"), n.get("originalName"))
            if ref and title:
                out.append(MangaSummary(id=ref, title=title,
                                        cover_url=((n.get("cover") or {}).get("main") or {}).get("url", ""),
                                        type=_TYPE_BACK.get(n.get("type") or "")))
        return out

    async def _catalog(self, variables: dict[str, Any], page: int) -> MangaListPage:
        after = None
        for i in range(1, min(page, MAX_CURSOR_WALK) + 1):
            data = await self.gql("fetchMangas", Q_MANGAS, {**variables, "first": 30, "after": after})
            conn = data.get("mangas") or {}
            info = conn.get("pageInfo") or {}
            if i == page:
                return MangaListPage(items=self._edges(conn.get("edges")), has_next=bool(info.get("hasNextPage")))
            after = info.get("endCursor")
            if not after or not info.get("hasNextPage"):
                break
        return MangaListPage(items=[], has_next=False)

    async def popular(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.POPULAR))

    async def latest(self, page: int = 1) -> MangaListPage:
        return await self.search(SearchQuery(page=page, sort=SortOrder.UPDATED))

    async def search(self, query: SearchQuery) -> MangaListPage:
        if query.query:
            if query.page > 1:
                return MangaListPage(items=[], has_next=False)
            data = await self.gql("search", Q_SEARCH, {"query": query.query, "first": 30})
            return MangaListPage(items=self._edges((data.get("search") or {}).get("edges")), has_next=False)
        variables: dict[str, Any] = {"orderField": _SORT.get(query.sort, "POPULARITY_SCORE"), "orderDirection": "DESC"}
        types = [_TYPES[t] for t in query.types if t in _TYPES]
        if types:
            variables["type"] = {"include": types}
        statuses = [_STATUS[s] for s in query.statuses if s in _STATUS]
        if statuses:
            variables["status"] = {"include": statuses}
        if query.genres_include or query.genres_exclude:
            label: dict[str, list[str]] = {}
            if query.genres_include:
                label["include"] = query.genres_include
            if query.genres_exclude:
                label["exclude"] = query.genres_exclude
            variables["label"] = label
        return await self._catalog(variables, query.page)

    async def _manga(self, slug: str) -> dict[str, Any]:
        m = (await self.gql("fetchManga", Q_MANGA, {"slug": slug})).get("manga")
        if not m:
            raise SourceParseError(self.info.id, "manga not found")
        return m

    def _details(self, slug: str, m: dict[str, Any]) -> MangaDetails:
        locs = m.get("localizations") or []
        loc = next((x for x in locs if x.get("lang") == "RU" and x.get("description")), None) or next(
            (x for x in locs if x.get("lang") == "EN" and x.get("description")), None)
        return MangaDetails(
            id=slug,
            title=_pick(m.get("titles"), m.get("originalName")) or slug,
            alt_titles=[a["content"] for a in m.get("alternativeNames") or [] if a.get("content")],
            cover_url=((m.get("cover") or {}).get("main") or {}).get("url", ""),
            description=tiptap_text(loc.get("description")) if loc else "",
            genres=[g for g in (_pick(lb.get("titles")) for lb in m.get("labels") or []) if g],
            authors=[s["person"]["name"] for s in m.get("mainStaff") or [] if (s.get("person") or {}).get("name")],
            status=_STATUS_BACK.get(m.get("status") or "", PublicationStatus.UNKNOWN),
            type=_TYPE_BACK.get(m.get("type") or ""),
            web_url=f"{self.info.base_url}/manga/{slug}",
            age_rating=_RATING.get(m.get("rating") or ""),
        )

    async def _chapters(self, m: dict[str, Any]) -> list[Chapter]:
        branches = sorted(m.get("branches") or [],
                          key=lambda b: (not b.get("primaryBranch"), -(b.get("chapters") or 0)))
        seen: set[str] = set()
        out: list[Chapter] = []
        for br in branches[:3]:
            acts = br.get("primaryTeamActivities") or []
            team = ((acts[0] if acts else {}).get("team") or {}).get("name")
            after = None
            for _ in range(50):
                data = await self.gql("fetchMangaChapters", Q_CHAPTERS, {
                    "branchId": br["id"], "after": after, "orderBy": {"field": "NUMBER", "direction": "DESC"}})
                conn = data.get("mangaChapters") or {}
                for e in conn.get("edges") or []:
                    n = e.get("node") or {}
                    ref = n.get("slug") or n.get("id")
                    if not ref or ref in seen:
                        continue
                    seen.add(ref)
                    number = parse_float(n.get("number"))
                    out.append(Chapter(id=ref, title=chapter_title(number, n.get("name") or None), number=number,
                                       volume=str(n["volume"]) if n.get("volume") is not None else None,
                                       uploaded_at=parse_iso(n.get("createdAt")), scanlator=team))
                info = conn.get("pageInfo") or {}
                if not info.get("hasNextPage"):
                    break
                after = info.get("endCursor")
        return out

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        m = await self._manga(manga_id)
        return self._details(manga_id, m), await self._chapters(m)

    async def details(self, manga_id: str) -> MangaDetails:
        return self._details(manga_id, await self._manga(manga_id))

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return await self._chapters(await self._manga(manga_id))

    async def pages(self, chapter_id: str) -> list[PageImage]:
        quality = self.config.extra.get("page_quality", "auto")
        ch = (await self.gql("fetchMangaChapter", Q_CHAPTER, {"slug": chapter_id, "cdnQuality": quality})).get("mangaChapter")
        if not ch:
            raise SourceParseError(self.info.id, "chapter not found")
        result = []
        for p in sorted(ch.get("pages") or [], key=lambda p: p.get("number") or 0):
            img = p.get("image") or {}
            url = (img.get("compress") or {}).get("url") or (img.get("original") or {}).get("url")
            if quality == "original":
                url = (img.get("original") or {}).get("url") or url
            if url:
                result.append(PageImage(index=len(result), url=url, headers=self.image_headers(url)))
        return result
