"""Source-agnostic domain models returned by every adapter.

Adapters translate site-specific payloads into these dataclasses; nothing
outside an adapter should ever see raw site JSON/HTML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MangaType(str, Enum):
    MANGA = "manga"
    MANHWA = "manhwa"
    MANHUA = "manhua"
    COMIC = "comic"
    OTHER = "other"


class PublicationStatus(str, Enum):
    ONGOING = "ongoing"
    COMPLETED = "completed"
    HIATUS = "hiatus"
    CANCELLED = "cancelled"
    ANNOUNCED = "announced"
    UNKNOWN = "unknown"


class SortOrder(str, Enum):
    POPULAR = "popular"
    RATING = "rating"
    UPDATED = "updated"
    CREATED = "created"
    NAME = "name"


@dataclass(frozen=True, slots=True)
class Genre:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    search: bool = True
    genre_filter: bool = False
    genre_exclude: bool = False
    type_filter: bool = False
    status_filter: bool = False
    sort_orders: tuple[SortOrder, ...] = (SortOrder.POPULAR, SortOrder.UPDATED)


@dataclass(frozen=True, slots=True)
class SourceInfo:
    id: str
    name: str
    base_url: str
    lang: str = "ru"
    is_nsfw: bool = False
    version: str = "0.1.0"
    notes: str = ""
    capabilities: SourceCapabilities = field(default_factory=SourceCapabilities)
    genres: tuple[Genre, ...] = ()
    origin: str = "redcatpet/mangayomi-ru-sources"


@dataclass(slots=True)
class MangaSummary:
    id: str
    title: str
    cover_url: str = ""
    type: MangaType | None = None
    # other names the list already gives (English, original): lets search merge one title
    # that every site calls differently
    alt_titles: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MangaListPage:
    items: list[MangaSummary]
    has_next: bool


@dataclass(slots=True)
class MangaDetails:
    id: str
    title: str
    cover_url: str = ""
    alt_titles: list[str] = field(default_factory=list)
    description: str = ""
    genres: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    status: PublicationStatus = PublicationStatus.UNKNOWN
    type: MangaType | None = None
    web_url: str = ""
    age_rating: str | None = None


@dataclass(slots=True)
class Chapter:
    id: str
    title: str
    number: float | None = None
    volume: str | None = None
    uploaded_at: datetime | None = None
    scanlator: str | None = None


@dataclass(slots=True)
class PageImage:
    index: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SearchQuery:
    query: str = ""
    page: int = 1
    sort: SortOrder = SortOrder.POPULAR
    genres_include: list[str] = field(default_factory=list)
    genres_exclude: list[str] = field(default_factory=list)
    types: list[MangaType] = field(default_factory=list)
    statuses: list[PublicationStatus] = field(default_factory=list)
