"""Friends, ratings, recommendations and chapter comments."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from pydantic import Field
from sqlalchemy import and_, delete, func, or_, select

from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...core.ratelimit import RateLimit
from ...models import Comment, CommentLike, Friendship, HistoryEntry, Rating, Recommendation, User
from ...schemas.common import Message, Schema
from ..deps import CurrentUser, SessionDep
from .trackers import schedule_push

friends_router = APIRouter(prefix="/friends", tags=["friends"])
ratings_router = APIRouter(prefix="/ratings", tags=["ratings"])
recs_router = APIRouter(prefix="/recommendations", tags=["recommendations"])
comments_router = APIRouter(prefix="/comments", tags=["comments"])


class PublicUser(Schema):
    id: uuid.UUID
    username: str
    display_name: str | None = None


def _pub(u: User) -> dict[str, Any]:
    return {"id": u.id, "username": u.username, "display_name": u.display_name}


# ================================================================== friends
class FriendRequestIn(Schema):
    username: str = Field(min_length=1, max_length=64)


async def _friend_ids(session, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (await session.execute(select(Friendship).where(
        Friendship.status == "accepted",
        or_(Friendship.requester_id == user_id, Friendship.addressee_id == user_id)))).scalars()
    return [f.addressee_id if f.requester_id == user_id else f.requester_id for f in rows]


async def _require_friend(session, me: uuid.UUID, other: uuid.UUID) -> User:
    if other != me and other not in await _friend_ids(session, me):
        raise ForbiddenError("Это не ваш друг", code="not_friends")
    u = await session.get(User, other)
    if u is None:
        raise NotFoundError("Пользователь не найден")
    return u


def _history_out(h: HistoryEntry) -> dict[str, Any]:
    return {"source_id": h.source_id, "manga_id": h.manga_id, "manga_title": h.manga_title, "cover_url": h.cover_url,
            "chapter_id": h.chapter_id, "chapter_title": h.chapter_title, "chapter_number": h.chapter_number,
            "read_at": h.read_at}


async def _recent_titles(session, user_id: uuid.UUID, limit: int) -> list[dict[str, Any]]:
    rows = (await session.execute(select(HistoryEntry).where(
        HistoryEntry.user_id == user_id, HistoryEntry.deleted_at.is_(None)).order_by(
        HistoryEntry.read_at.desc()).limit(limit * 8))).scalars()
    seen, out = set(), []
    for h in rows:
        k = (h.source_id, h.manga_id)
        if k in seen:
            continue
        seen.add(k)
        out.append(_history_out(h))
        if len(out) >= limit:
            break
    return out


def _rating_out(r: Rating, user: User | None = None) -> dict[str, Any]:
    d = {"source_id": r.source_id, "manga_id": r.manga_id, "manga_title": r.manga_title, "cover_url": r.cover_url,
         "score": r.score, "review": r.review, "updated_at": r.updated_at}
    if user is not None:
        d["user"] = _pub(user)
    return d


@friends_router.get("", summary="Друзья, заявки и что друзья читают сейчас")
async def list_friends(user: CurrentUser, session: SessionDep) -> Any:
    rows = list((await session.execute(select(Friendship).where(
        or_(Friendship.requester_id == user.id, Friendship.addressee_id == user.id)))).scalars())
    other_ids = {f.addressee_id if f.requester_id == user.id else f.requester_id for f in rows}
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(other_ids)))).scalars()} if other_ids else {}
    friends, incoming, outgoing = [], [], []
    for f in rows:
        other = users.get(f.addressee_id if f.requester_id == user.id else f.requester_id)
        if other is None:
            continue
        if f.status == "accepted":
            now = await _recent_titles(session, other.id, 1)
            friends.append({"user": _pub(other), "since": f.accepted_at or f.created_at, "reading": now[0] if now else None})
        elif f.addressee_id == user.id:
            incoming.append({"id": f.id, "user": _pub(other), "created_at": f.created_at})
        else:
            outgoing.append({"id": f.id, "user": _pub(other), "created_at": f.created_at})
    friends.sort(key=lambda x: x["reading"]["read_at"] if x["reading"] else datetime.min.replace(tzinfo=UTC), reverse=True)
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing}


@friends_router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(RateLimit("friend_req", 20))])
async def add_friend(body: FriendRequestIn, user: CurrentUser, session: SessionDep) -> Message:
    other = (await session.execute(select(User).where(
        func.lower(User.username) == body.username.strip().lstrip("@").lower()))).scalar_one_or_none()
    if other is None or not other.is_active:
        raise NotFoundError("Нет пользователя с таким ником", code="user_not_found")
    if other.id == user.id:
        raise ConflictError("Это вы :)", code="friend_self")
    existing = (await session.execute(select(Friendship).where(or_(
        and_(Friendship.requester_id == user.id, Friendship.addressee_id == other.id),
        and_(Friendship.requester_id == other.id, Friendship.addressee_id == user.id))))).scalar_one_or_none()
    if existing is not None:
        if existing.status == "accepted":
            return Message(message="Вы уже друзья")
        if existing.addressee_id == user.id:  # they asked first: accept
            existing.status, existing.accepted_at = "accepted", datetime.now(UTC)
            await session.commit()
            return Message(message="Теперь вы друзья")
        return Message(message="Заявка уже отправлена")
    session.add(Friendship(requester_id=user.id, addressee_id=other.id))
    await session.commit()
    return Message(message="Заявка отправлена")


@friends_router.post("/requests/{request_id}/accept")
async def accept_friend(request_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Message:
    f = await session.get(Friendship, request_id)
    if f is None or f.addressee_id != user.id:
        raise NotFoundError("Заявка не найдена")
    f.status, f.accepted_at = "accepted", datetime.now(UTC)
    await session.commit()
    return Message(message="Теперь вы друзья")


@friends_router.delete("/requests/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def drop_request(request_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    f = await session.get(Friendship, request_id)
    if f is None or user.id not in (f.requester_id, f.addressee_id) or f.status != "pending":
        raise NotFoundError("Заявка не найдена")
    await session.delete(f)
    await session.commit()


@friends_router.delete("/{friend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unfriend(friend_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await session.execute(delete(Friendship).where(or_(
        and_(Friendship.requester_id == user.id, Friendship.addressee_id == friend_id),
        and_(Friendship.requester_id == friend_id, Friendship.addressee_id == user.id))))
    await session.commit()


@friends_router.get("/activity", summary="Лента друзей: что прочитали и как оценили")
async def friends_activity(user: CurrentUser, session: SessionDep, limit: int = Query(40, ge=1, le=100)) -> Any:
    ids = await _friend_ids(session, user.id)
    if not ids:
        return {"items": []}
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()}
    since = datetime.now(UTC) - timedelta(days=30)
    hist = (await session.execute(select(HistoryEntry).where(
        HistoryEntry.user_id.in_(ids), HistoryEntry.deleted_at.is_(None), HistoryEntry.read_at >= since).order_by(
        HistoryEntry.read_at.desc()).limit(limit * 6))).scalars()
    items, seen = [], set()
    for h in hist:
        k = (h.user_id, h.source_id, h.manga_id)
        if k in seen:
            continue
        seen.add(k)
        items.append({"kind": "read", "at": h.read_at, "user": _pub(users[h.user_id]), **_history_out(h)})
    for r in (await session.execute(select(Rating).where(Rating.user_id.in_(ids), Rating.updated_at >= since).order_by(
            Rating.updated_at.desc()).limit(limit))).scalars():
        items.append({"kind": "rated", "at": r.updated_at, **_rating_out(r, users[r.user_id])})
    items.sort(key=lambda x: x["at"], reverse=True)
    return {"items": items[:limit]}


@friends_router.get("/{friend_id}/profile")
async def friend_profile(friend_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Any:
    other = await _require_friend(session, user.id, friend_id)
    ratings = (await session.execute(select(Rating).where(Rating.user_id == other.id).order_by(
        Rating.score.desc(), Rating.updated_at.desc()).limit(60))).scalars()
    month = datetime.now(UTC) - timedelta(days=30)
    chapters_30d = (await session.execute(select(func.count()).select_from(HistoryEntry).where(
        HistoryEntry.user_id == other.id, HistoryEntry.deleted_at.is_(None), HistoryEntry.read_at >= month))).scalar_one()
    return {"user": _pub(other), "reading": await _recent_titles(session, other.id, 12),
            "ratings": [_rating_out(r) for r in ratings], "chapters_30d": chapters_30d}


# ================================================================== ratings
class RatingIn(Schema):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    manga_title: str = Field(default="", max_length=512)
    cover_url: str = Field(default="", max_length=2048)
    score: int = Field(ge=1, le=10)
    review: str = Field(default="", max_length=2000)


@ratings_router.get("")
async def my_ratings(user: CurrentUser, session: SessionDep) -> Any:
    rows = (await session.execute(select(Rating).where(Rating.user_id == user.id).order_by(Rating.updated_at.desc()))).scalars()
    return {"items": [_rating_out(r) for r in rows]}


@ratings_router.put("")
async def rate(body: RatingIn, user: CurrentUser, session: SessionDep, request: Request,
               background: BackgroundTasks) -> Any:
    r = (await session.execute(select(Rating).where(Rating.user_id == user.id, Rating.source_id == body.source_id,
                                                    Rating.manga_id == body.manga_id))).scalar_one_or_none()
    if r is None:
        r = Rating(user_id=user.id, source_id=body.source_id, manga_id=body.manga_id, score=body.score)
        session.add(r)
    r.score, r.review = body.score, body.review
    r.manga_title = body.manga_title or r.manga_title
    r.cover_url = body.cover_url or r.cover_url
    r.updated_at = datetime.now(UTC)
    await session.commit()
    schedule_push(request, background, user.id, {(body.source_id, body.manga_id): None}, score=body.score,
                  title_hint=body.manga_title)
    return _rating_out(r)


@ratings_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def unrate(user: CurrentUser, session: SessionDep, source_id: str, manga_id: str) -> None:
    await session.execute(delete(Rating).where(Rating.user_id == user.id, Rating.source_id == source_id,
                                               Rating.manga_id == manga_id))
    await session.commit()


@ratings_router.get("/title", summary="Моя оценка тайтла и оценки друзей")
async def title_ratings(user: CurrentUser, session: SessionDep, source_id: str, manga_id: str) -> Any:
    ids = await _friend_ids(session, user.id)
    rows = list((await session.execute(select(Rating).where(
        Rating.source_id == source_id, Rating.manga_id == manga_id,
        Rating.user_id.in_([user.id, *ids])))).scalars())
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_([r.user_id for r in rows])))).scalars()} if rows else {}
    mine = next((r for r in rows if r.user_id == user.id), None)
    friends = [_rating_out(r, users.get(r.user_id)) for r in rows if r.user_id != user.id]
    agg = (await session.execute(select(func.avg(Rating.score), func.count()).where(
        Rating.source_id == source_id, Rating.manga_id == manga_id))).one()
    return {"mine": _rating_out(mine) if mine else None, "friends": friends,
            "average": round(float(agg[0]), 1) if agg[0] is not None else None, "count": agg[1]}


# ========================================================== recommendations
class RecommendIn(Schema):
    to_user_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    manga_title: str = Field(default="", max_length=512)
    cover_url: str = Field(default="", max_length=2048)
    message: str = Field(default="", max_length=500)


@recs_router.post("", dependencies=[Depends(RateLimit("recommend", 30))])
async def recommend(body: RecommendIn, user: CurrentUser, session: SessionDep) -> Message:
    friends = set(await _friend_ids(session, user.id))
    targets = [t for t in body.to_user_ids if t in friends]
    if not targets:
        raise ForbiddenError("Советовать можно только друзьям", code="not_friends")
    for t in targets:
        session.add(Recommendation(from_user_id=user.id, to_user_id=t, source_id=body.source_id, manga_id=body.manga_id,
                                   manga_title=body.manga_title, cover_url=body.cover_url, message=body.message))
    await session.commit()
    return Message(message=f"Отправлено: {len(targets)}")


@recs_router.get("")
async def my_recommendations(user: CurrentUser, session: SessionDep, limit: int = Query(50, ge=1, le=200)) -> Any:
    rows = list((await session.execute(select(Recommendation).where(Recommendation.to_user_id == user.id).order_by(
        Recommendation.created_at.desc()).limit(limit))).scalars())
    users = {u.id: u for u in (await session.execute(select(User).where(
        User.id.in_({r.from_user_id for r in rows})))).scalars()} if rows else {}
    return {"items": [{"id": r.id, "from": _pub(users[r.from_user_id]) if r.from_user_id in users else None,
                       "source_id": r.source_id, "manga_id": r.manga_id, "manga_title": r.manga_title,
                       "cover_url": r.cover_url, "message": r.message, "created_at": r.created_at,
                       "seen": r.seen_at is not None} for r in rows],
            "unseen": sum(1 for r in rows if r.seen_at is None)}


@recs_router.post("/seen")
async def recommendations_seen(user: CurrentUser, session: SessionDep) -> Message:
    rows = (await session.execute(select(Recommendation).where(
        Recommendation.to_user_id == user.id, Recommendation.seen_at.is_(None)))).scalars()
    n = 0
    for r in rows:
        r.seen_at = datetime.now(UTC)
        n += 1
    await session.commit()
    return Message(message=f"marked {n}")


# ================================================================= comments
def chapter_key(source_id: str, chapter_id: str) -> str:
    return hashlib.sha1(f"{source_id}|{chapter_id}".encode()).hexdigest()


class CommentIn(Schema):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    chapter_id: str = Field(max_length=1024)
    text: str = Field(min_length=1, max_length=2000)
    page_index: int | None = Field(default=None, ge=0)


def _comment_out(c: Comment, u: User | None, me: uuid.UUID, liked: set[uuid.UUID]) -> dict[str, Any]:
    return {"id": c.id, "user": _pub(u) if u else None, "text": c.text, "page_index": c.page_index, "likes": c.likes,
            "liked": c.id in liked, "mine": c.user_id == me, "created_at": c.created_at}


@comments_router.get("")
async def list_comments(user: CurrentUser, session: SessionDep, source_id: str, chapter_id: str,
                        limit: int = Query(50, ge=1, le=200), before: datetime | None = None) -> Any:
    q = select(Comment).where(Comment.chapter_key == chapter_key(source_id, chapter_id), Comment.deleted_at.is_(None))
    if before:
        q = q.where(Comment.created_at < before)
    rows = list((await session.execute(q.order_by(Comment.created_at.desc()).limit(limit))).scalars())
    users = {u.id: u for u in (await session.execute(select(User).where(
        User.id.in_({c.user_id for c in rows})))).scalars()} if rows else {}
    liked = set((await session.execute(select(CommentLike.comment_id).where(
        CommentLike.user_id == user.id, CommentLike.comment_id.in_([c.id for c in rows])))).scalars()) if rows else set()
    total = (await session.execute(select(func.count()).select_from(Comment).where(
        Comment.chapter_key == chapter_key(source_id, chapter_id), Comment.deleted_at.is_(None)))).scalar_one()
    return {"items": [_comment_out(c, users.get(c.user_id), user.id, liked) for c in rows], "total": total,
            "has_more": len(rows) == limit}


@comments_router.get("/count")
async def count_comments(user: CurrentUser, session: SessionDep, source_id: str, chapter_id: str) -> Any:
    n = (await session.execute(select(func.count()).select_from(Comment).where(
        Comment.chapter_key == chapter_key(source_id, chapter_id), Comment.deleted_at.is_(None)))).scalar_one()
    return {"count": n}


@comments_router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(RateLimit("comment", 10))])
async def add_comment(body: CommentIn, user: CurrentUser, session: SessionDep) -> Any:
    text = body.text.strip()
    if not text:
        raise ConflictError("Пустой комментарий", code="comment_empty")
    c = Comment(user_id=user.id, chapter_key=chapter_key(body.source_id, body.chapter_id), source_id=body.source_id,
                manga_id=body.manga_id, chapter_id=body.chapter_id, text=text, page_index=body.page_index)
    session.add(c)
    await session.commit()
    return _comment_out(c, user, user.id, set())


@comments_router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(comment_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    c = await session.get(Comment, comment_id)
    if c is None or c.user_id != user.id or c.deleted_at is not None:
        raise NotFoundError("Комментарий не найден")
    c.deleted_at = datetime.now(UTC)
    await session.commit()


@comments_router.post("/{comment_id}/like")
async def like_comment(comment_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Any:
    c = await session.get(Comment, comment_id)
    if c is None or c.deleted_at is not None:
        raise NotFoundError("Комментарий не найден")
    like = await session.get(CommentLike, (comment_id, user.id))
    if like is None:
        session.add(CommentLike(comment_id=comment_id, user_id=user.id))
        c.likes += 1
    else:
        await session.delete(like)
        c.likes = max(0, c.likes - 1)
    await session.commit()
    return {"likes": c.likes, "liked": like is None}
