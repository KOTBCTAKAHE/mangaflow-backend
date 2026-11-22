"""The single interface every source implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from .http import SourceHttpClient
from .models import (
    Chapter,
    ChapterText,
    MangaDetails,
    MangaListPage,
    PageImage,
    SearchQuery,
    SortOrder,
    SourceInfo,
)


@dataclass(slots=True)
class SourceConfig:
    """Runtime configuration injected by the host application.

    ``base_url`` lets operators switch to a mirror when a domain is blocked;
    ``token``/``cookie`` are the operator's *own* credentials for sites that
    require an account (we never bypass auth or paywalls).
    """

    base_url: str | None = None
    token: str | None = None
    cookie: str | None = None
    proxy: str | None = None
    timeout: float = 15.0
    max_retries: int = 2
    rate_per_second: float = 4.0
    extra: dict[str, str] = field(default_factory=dict)


class SourceAdapter(ABC):
    """Base class. Subclasses declare ``info`` and implement the async methods.

    Adapters are stateless apart from their HTTP client and are safe to share
    between concurrent requests.
    """

    info: ClassVar[SourceInfo]

    def __init__(
        self,
        config: SourceConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or SourceConfig()
        self.base_url = (self.config.base_url or self.info.base_url).rstrip("/")
        self.http = SourceHttpClient(
            self.info.id,
            headers=self.default_headers(),
            timeout=self.config.timeout,
            max_retries=self.config.max_retries,
            rate_per_second=self.config.rate_per_second,
            transport=transport,
            proxy=self.config.proxy,
        )

    # --- hooks -----------------------------------------------------------
    def default_headers(self) -> dict[str, str]:
        return {"Referer": (self.config.base_url or self.info.base_url).rstrip("/") + "/"}

    def image_headers(self, url: str) -> dict[str, str]:
        """Headers the image CDN expects (Referer etc.). Used by the image proxy."""
        return {"Referer": self.base_url + "/"}

    # --- catalog ---------------------------------------------------------
    @abstractmethod
    async def popular(self, page: int = 1) -> MangaListPage: ...

    @abstractmethod
    async def latest(self, page: int = 1) -> MangaListPage: ...

    @abstractmethod
    async def search(self, query: SearchQuery) -> MangaListPage: ...

    # --- title -----------------------------------------------------------
    @abstractmethod
    async def details(self, manga_id: str) -> MangaDetails: ...

    @abstractmethod
    async def chapters(self, manga_id: str) -> list[Chapter]: ...

    @abstractmethod
    async def pages(self, chapter_id: str) -> list[PageImage]: ...

    async def text(self, chapter_id: str) -> ChapterText:
        """Ранобэ sources (``info.content == "text"``): the chapter as text blocks."""
        raise NotImplementedError(f"{self.info.id} has no text chapters")

    async def title(self, manga_id: str) -> tuple[MangaDetails, list[Chapter]]:
        """Details + chapters. HTML sources override this to parse one page once."""
        import asyncio

        details, chapters = await asyncio.gather(self.details(manga_id), self.chapters(manga_id))
        return details, chapters

    # --- helpers ---------------------------------------------------------
    async def browse(self, sort: SortOrder, page: int) -> MangaListPage:
        if sort == SortOrder.UPDATED:
            return await self.latest(page)
        if sort == SortOrder.POPULAR:
            return await self.popular(page)
        return await self.search(SearchQuery(sort=sort, page=page))

    def abs_url(self, url: str | None) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return self.base_url + (url if url.startswith("/") else "/" + url)

    async def aclose(self) -> None:
        await self.http.aclose()
