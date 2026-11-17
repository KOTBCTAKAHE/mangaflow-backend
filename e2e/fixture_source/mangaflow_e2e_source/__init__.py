"""E2E fixture source.

Used only by the end-to-end CI job: real sites are geo-blocked from CI
runners, so this adapter provides a deterministic catalog whose page images
are served by the `fixtures` container. It is installed as a *plugin* via the
`mangaflow.sources` entry point, which also exercises the extension mechanism.
"""
from __future__ import annotations

import os

from mangaflow_sources import (
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
    SourceAdapter,
    SourceCapabilities,
    SourceInfo,
)

IMG = os.environ.get("E2E_IMAGE_BASE", "http://fixtures:8000")

TITLES = {
    "manhwa": ("Тестовая манхва", MangaType.MANHWA, ["Экшен", "Фэнтези"]),
    "manga": ("Тестовая манга", MangaType.MANGA, ["Драма"]),
    "comic": ("Тестовый комикс", MangaType.COMIC, ["Комедия"]),
}
CHAPTERS = 3
PAGES = 5


class E2EAdapter(SourceAdapter):
    info = SourceInfo(
        id="e2e",
        name="E2E Fixtures",
        base_url="http://fixtures:8000",
        capabilities=SourceCapabilities(search=True, genre_filter=True,
                                        sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED)),
        genres=(Genre("action", "Экшен"), Genre("drama", "Драма")),
        notes="Тестовый источник для E2E",
    )

    def _cards(self, keys):
        return [MangaSummary(id=k, title=TITLES[k][0], cover_url=f"{IMG}/cover/{k}.png", type=TITLES[k][1]) for k in keys]

    async def popular(self, page: int = 1) -> MangaListPage:
        return MangaListPage(self._cards(TITLES) if page == 1 else [], has_next=False)

    async def latest(self, page: int = 1) -> MangaListPage:
        return MangaListPage(self._cards(["comic", "manhwa"]) if page == 1 else [], has_next=False)

    async def search(self, query: SearchQuery) -> MangaListPage:
        q = query.query.lower()
        keys = [k for k, v in TITLES.items() if q in v[0].lower()]
        if query.genres_include:
            keys = [k for k in keys if "action" in query.genres_include and k == "manhwa"]
        return MangaListPage(self._cards(keys) if query.page == 1 else [], has_next=False)

    async def details(self, manga_id: str) -> MangaDetails:
        title, kind, genres = TITLES[manga_id]
        return MangaDetails(id=manga_id, title=title, cover_url=f"{IMG}/cover/{manga_id}.png", type=kind,
                            genres=genres, authors=["Автор Тестов"], status=PublicationStatus.ONGOING,
                            description=f"Описание: {title}. Используется для сквозных тестов.",
                            alt_titles=[f"{manga_id} (alt)"])

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return [Chapter(id=f"{manga_id}/{n}", title=f"Глава {n}", number=float(n)) for n in range(CHAPTERS, 0, -1)]

    async def pages(self, chapter_id: str) -> list[PageImage]:
        return [PageImage(index=i, url=f"{IMG}/page/{chapter_id}/{i}.png") for i in range(PAGES)]
