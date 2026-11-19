"""Social layer (ratings, friends, recommendations, chapter comments) and
external trackers (Shikimori / MyAnimeList / AniList)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, UUIDPk, utcnow


class Rating(UUIDPk, Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("user_id", "source_id", "manga_id", name="uq_rating_title"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    manga_id: Mapped[str] = mapped_column(String(512))
    manga_title: Mapped[str] = mapped_column(String(512), default="")
    cover_url: Mapped[str] = mapped_column(String(2048), default="")
    score: Mapped[int] = mapped_column(Integer)  # 1..10
    review: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True)


class Friendship(UUIDPk, Base):
    """A request from ``requester`` to ``addressee``; friends once ``accepted``."""

    __tablename__ = "friendships"
    __table_args__ = (UniqueConstraint("requester_id", "addressee_id", name="uq_friend_pair"),)

    requester_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    addressee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | accepted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Recommendation(UUIDPk, Base):
    __tablename__ = "recommendations"

    from_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    to_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    manga_id: Mapped[str] = mapped_column(String(512))
    manga_title: Mapped[str] = mapped_column(String(512), default="")
    cover_url: Mapped[str] = mapped_column(String(2048), default="")
    message: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Comment(UUIDPk, Base):
    __tablename__ = "comments"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # sha1(source|chapter): chapter ids can be long, the key stays indexable
    chapter_key: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(String(64))
    manga_id: Mapped[str] = mapped_column(String(512))
    chapter_id: Mapped[str] = mapped_column(String(1024))
    page_index: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommentLike(Base):
    __tablename__ = "comment_likes"

    comment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("comments.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class TrackerAccount(UUIDPk, Base):
    __tablename__ = "tracker_accounts"
    __table_args__ = (UniqueConstraint("user_id", "service", name="uq_tracker_account"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(16))
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_user_id: Mapped[str] = mapped_column(String(64), default="")
    remote_username: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)


class TrackerLink(UUIDPk, Base):
    """Which remote entry a title is tracked as, and what we last sent there."""

    __tablename__ = "tracker_links"
    __table_args__ = (UniqueConstraint("user_id", "service", "source_id", "manga_id", name="uq_tracker_link"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(64))
    manga_id: Mapped[str] = mapped_column(String(512))
    remote_id: Mapped[str | None] = mapped_column(String(64))  # None = searched, nothing matched
    remote_title: Mapped[str] = mapped_column(String(512), default="")
    remote_url: Mapped[str] = mapped_column(String(1024), default="")
    remote_chapters: Mapped[int | None] = mapped_column(Integer)
    pushed_chapter: Mapped[int | None] = mapped_column(Integer)
    pushed_score: Mapped[int | None] = mapped_column(Integer)
    manual: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
