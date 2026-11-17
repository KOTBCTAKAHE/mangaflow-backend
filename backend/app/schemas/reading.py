from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from .common import AwareDatetime, Schema


class ProgressIn(Schema):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    chapter_id: str = Field(max_length=1024)
    chapter_number: float | None = None
    chapter_title: str = Field(default="", max_length=512)
    page_index: int = Field(ge=0, default=0)
    page_count: int = Field(ge=0, default=0)
    completed: bool = False
    updated_at: AwareDatetime


class ProgressOut(Schema):
    source_id: str
    manga_id: str
    chapter_id: str
    chapter_number: float | None
    chapter_title: str
    page_index: int
    page_count: int
    completed: bool
    updated_at: datetime


class ProgressBatchIn(Schema):
    items: list[ProgressIn] = Field(max_length=5000)


class ContinueReadingOut(Schema):
    source_id: str
    manga_id: str
    title: str
    cover_url: str
    chapter_id: str
    chapter_title: str
    chapter_number: float | None
    page_index: int
    page_count: int
    updated_at: datetime


class HistoryIn(Schema):
    id: uuid.UUID
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    manga_title: str = Field(default="", max_length=512)
    cover_url: str = Field(default="", max_length=2048)
    chapter_id: str = Field(max_length=1024)
    chapter_title: str = Field(default="", max_length=512)
    chapter_number: float | None = None
    started_at: AwareDatetime
    read_at: AwareDatetime
    duration_seconds: int = Field(ge=0, le=86400, default=0)
    pages_read: int = Field(ge=0, default=0)


class HistoryOut(HistoryIn):
    pass


class HistoryBatchIn(Schema):
    items: list[HistoryIn] = Field(max_length=5000)


class DownloadIn(Schema):
    device_id: str = Field(max_length=128)
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    chapter_id: str = Field(max_length=1024)
    manga_title: str = Field(default="", max_length=512)
    chapter_title: str = Field(default="", max_length=512)
    status: str = Field(default="completed", pattern="^(queued|downloading|completed|failed|deleted)$")
    page_count: int = 0
    size_bytes: int = 0


class DownloadOut(DownloadIn):
    id: uuid.UUID
    updated_at: datetime


class DownloadBatchIn(Schema):
    items: list[DownloadIn] = Field(max_length=2000)


class ManifestIn(Schema):
    source_id: str
    chapter_ids: list[str] = Field(min_length=1, max_length=50)


class ManifestChapter(Schema):
    chapter_id: str
    ok: bool
    pages: list[dict[str, object]] = Field(default_factory=list)
    error_code: str | None = None
    error: str | None = None


class ManifestOut(Schema):
    source_id: str
    chapters: list[ManifestChapter]


class UpdateOut(Schema):
    id: uuid.UUID
    source_id: str
    manga_id: str
    manga_title: str
    cover_url: str
    chapter_id: str
    chapter_title: str
    chapter_number: float | None
    detected_at: datetime
    seen: bool


class SeenIn(Schema):
    ids: list[uuid.UUID] | None = None
    all: bool = False


class DayActivity(Schema):
    day: date
    chapters: int
    seconds: int


class StatsOut(Schema):
    chapters_read: int
    titles_read: int
    total_seconds: int
    library_total: int
    by_status: dict[str, int]
    current_streak_days: int
    longest_streak_days: int
    activity: list[DayActivity]
    top_genres: list[dict[str, object]] = Field(default_factory=list)
