from __future__ import annotations

from datetime import datetime

from .common import Schema


class GenreOut(Schema):
    id: str
    name: str


class CapabilitiesOut(Schema):
    search: bool
    genre_filter: bool
    genre_exclude: bool
    type_filter: bool
    status_filter: bool
    sort_orders: list[str]


class SourceOut(Schema):
    id: str
    name: str
    base_url: str
    lang: str
    is_nsfw: bool
    version: str
    notes: str
    origin: str
    capabilities: CapabilitiesOut
    genres: list[GenreOut]


class SourceHealthOut(Schema):
    id: str
    ok: bool
    latency_ms: int | None = None
    error_code: str | None = None
    error: str | None = None
    checked_at: datetime


class MangaCard(Schema):
    source_id: str
    id: str
    title: str
    cover_url: str
    type: str | None = None


class MangaList(Schema):
    source_id: str
    page: int
    has_next: bool
    items: list[MangaCard]


class ChapterOut(Schema):
    id: str
    title: str
    number: float | None = None
    volume: str | None = None
    uploaded_at: datetime | None = None
    scanlator: str | None = None


class MangaDetailsOut(Schema):
    source_id: str
    source_name: str
    id: str
    title: str
    alt_titles: list[str]
    cover_url: str
    description: str
    genres: list[str]
    authors: list[str]
    artists: list[str]
    status: str
    type: str | None
    web_url: str
    age_rating: str | None
    chapters_count: int


class TitleOut(Schema):
    manga: MangaDetailsOut
    chapters: list[ChapterOut]


class PageOut(Schema):
    index: int
    url: str


class PagesOut(Schema):
    source_id: str
    chapter_id: str
    pages: list[PageOut]


class SearchSourceHit(Schema):
    source_id: str
    source_name: str
    id: str
    title: str
    cover_url: str


class SearchItem(Schema):
    key: str
    title: str
    cover_url: str
    type: str | None
    score: float
    sources: list[SearchSourceHit]


class SourceErrorOut(Schema):
    source_id: str
    code: str
    message: str


class SearchOut(Schema):
    query: str
    page: int
    has_next: bool
    items: list[SearchItem]
    errors: list[SourceErrorOut]
    sources_queried: list[str]


class HomeSection(Schema):
    key: str
    title: str
    items: list[MangaCard]


class HomeOut(Schema):
    source_id: str
    sections: list[HomeSection]
    errors: list[SourceErrorOut]
