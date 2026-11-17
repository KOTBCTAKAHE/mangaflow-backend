from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .common import AwareDatetime, Schema

LibraryStatus = Literal["reading", "planned", "completed", "on_hold", "dropped"]


class LibraryEntryIn(Schema):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    title: str = Field(max_length=512)
    cover_url: str = Field(default="", max_length=2048)
    status: LibraryStatus = "planned"
    is_favorite: bool = False
    reader_settings: dict[str, Any] = Field(default_factory=dict)
    total_chapters: int | None = None
    last_chapter_number: float | None = None
    last_read_at: AwareDatetime | None = None
    added_at: AwareDatetime | None = None
    updated_at: AwareDatetime
    deleted: bool = False
    collection_ids: list[uuid.UUID] | None = None


class LibraryEntryOut(Schema):
    id: uuid.UUID
    source_id: str
    manga_id: str
    title: str
    cover_url: str
    status: str
    is_favorite: bool
    reader_settings: dict[str, Any]
    total_chapters: int | None
    last_chapter_number: float | None
    last_read_at: datetime | None
    added_at: datetime
    updated_at: datetime
    deleted: bool = False
    collection_ids: list[uuid.UUID] = Field(default_factory=list)
    unread_updates: int = 0


class LibraryPatchIn(Schema):
    status: LibraryStatus | None = None
    is_favorite: bool | None = None
    reader_settings: dict[str, Any] | None = None
    updated_at: AwareDatetime | None = None


class LibrarySyncIn(Schema):
    items: list[LibraryEntryIn] = Field(max_length=2000)


class CollectionIn(Schema):
    name: str = Field(min_length=1, max_length=128)
    position: int = 0


class CollectionOut(Schema):
    id: uuid.UUID
    name: str
    position: int
    created_at: datetime
    updated_at: datetime
    entry_ids: list[uuid.UUID] = Field(default_factory=list)


class CollectionItemsIn(Schema):
    entry_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
