from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, JSONType, UUIDPk, utcnow

LIBRARY_STATUSES = ("reading", "planned", "completed", "on_hold", "dropped")


class LibraryEntry(UUIDPk, Base):
    """A title in a user's library. Only a display snapshot is stored — the
    catalog itself lives in the sources."""

    __tablename__ = "library_entries"
    __table_args__ = (UniqueConstraint("user_id", "source_id", "manga_id", name="uq_library_user_manga"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    manga_id: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    cover_url: Mapped[str] = mapped_column(String(2048), default="")
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    reader_settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    total_chapters: Mapped[int | None] = mapped_column(Integer)
    last_chapter_number: Mapped[float | None] = mapped_column(Float)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # client-supplied modification time; last-write-wins
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    server_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Collection(UUIDPk, Base):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_collection_user_name"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CollectionItem(Base):
    __tablename__ = "collection_items"

    collection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("library_entries.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
