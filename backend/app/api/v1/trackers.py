"""Shikimori / MyAnimeList / AniList: connect, pick the matching title, push progress."""
from __future__ import annotations

import html
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import Field
from sqlalchemy import delete, select

from ...core.config import get_settings
from ...core.errors import AppError
from ...core.logging import get_logger
from ...core.security import sign_payload, verify_signed
from ...models import LibraryEntry, ReadingProgress, Rating, TrackerAccount, TrackerLink
from ...schemas.common import Message, Schema
from ...services.trackers import TrackerError, TrackerService
from ..deps import CurrentUser, SessionDep

log = get_logger(__name__)
router = APIRouter(prefix="/trackers", tags=["trackers"])


def _svc(request: Request) -> TrackerService:
    return request.app.state.trackers


def _redirect_uri(request: Request, service: str) -> str:
    base = (get_settings().public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/api/v1/trackers/{service}/callback"


@router.get("", summary="Трекеры: какие настроены на сервере и подключены у вас")
async def list_trackers(user: CurrentUser, session: SessionDep, request: Request) -> Any:
    svc = _svc(request)
    accounts = {a.service: a for a in await svc.accounts(session, user.id)}
    return {"items": [{
        "service": p.id, "name": p.name, "configured": p.configured,
        "connected": p.id in accounts, "username": accounts[p.id].remote_username if p.id in accounts else None,
        "last_error": accounts[p.id].last_error if p.id in accounts else None,
        "redirect_uri": _redirect_uri(request, p.id),
    } for p in svc.providers.values()]}


@router.post("/{service}/authorize", summary="Ссылка для входа в трекер (открыть в браузере)")
async def authorize(service: str, user: CurrentUser, request: Request) -> Any:
    p = _svc(request).provider(service)
    if not p.configured:
        raise AppError(f"{p.name} не настроен на сервере (нет client id/secret в .env)", code="tracker_not_configured",
                       status_code=503)
    verifier = TrackerService.new_verifier()
    state = sign_payload({"u": str(user.id), "s": service, "v": verifier}, ttl_seconds=900, bucket_seconds=60)
    return {"url": p.authorize_url(_redirect_uri(request, service), state, verifier)}


def _page(title: str, text: str, ok: bool) -> HTMLResponse:
    color = "#2fbf71" if ok else "#e5484d"
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;min-height:90vh;align-items:center;
justify-content:center;text-align:center;padding:24px}}h1{{color:{color};font-size:26px}}p{{opacity:.8;line-height:1.5}}</style>
</head><body><div><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></div></body></html>""",
                        status_code=200 if ok else 400)


@router.get("/{service}/callback", include_in_schema=False)
async def callback(service: str, request: Request, session: SessionDep, code: str | None = None,
                   state: str | None = None, error: str | None = None) -> HTMLResponse:
    body = verify_signed(state or "")
    if error or not code or not body or body.get("s") != service:
        return _page("Не получилось", "Вход отменён или ссылка устарела. Вернитесь в MangaFlow и попробуйте ещё раз.", False)
    svc = _svc(request)
    try:
        acc = await svc.connect(session, uuid.UUID(body["u"]), service, code, _redirect_uri(request, service), body["v"])
    except (TrackerError, Exception) as exc:  # noqa: BLE001
        log.warning("tracker_connect_failed", service=service, error=str(exc))
        return _page("Не получилось", f"{svc.provider(service).name} не принял вход: {getattr(exc, 'message', exc)}", False)
    return _page(f"{svc.provider(service).name} подключён",
                 f"Аккаунт {acc.remote_username}. Вернитесь в MangaFlow — прогресс будет отправляться сам.", True)


@router.delete("/{service}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(service: str, user: CurrentUser, session: SessionDep) -> None:
    await session.execute(delete(TrackerAccount).where(TrackerAccount.user_id == user.id, TrackerAccount.service == service))
    await session.execute(delete(TrackerLink).where(TrackerLink.user_id == user.id, TrackerLink.service == service))
    await session.commit()


@router.get("/{service}/search")
async def search(service: str, user: CurrentUser, session: SessionDep, request: Request,
                 q: str = Query(min_length=1, max_length=200)) -> Any:
    return {"items": [c.out() for c in await _svc(request).search(session, user.id, service, q)]}


@router.get("/links", summary="Как тайтл привязан к трекерам")
async def links(user: CurrentUser, session: SessionDep, request: Request, source_id: str, manga_id: str) -> Any:
    svc = _svc(request)
    accounts = await svc.accounts(session, user.id)
    out = []
    for a in accounts:
        link = await svc.link(session, user.id, a.service, source_id, manga_id)
        out.append({"service": a.service, "name": svc.provider(a.service).name,
                    "remote_id": link.remote_id if link else None, "remote_title": link.remote_title if link else "",
                    "remote_url": link.remote_url if link else "", "chapters_sent": link.pushed_chapter if link else None,
                    "manual": link.manual if link else False, "searched": link is not None})
    return {"items": out}


class LinkIn(Schema):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    remote_id: str | None = Field(default=None, max_length=64)
    remote_title: str = Field(default="", max_length=512)
    remote_url: str = Field(default="", max_length=1024)
    remote_chapters: int | None = None


@router.put("/{service}/links")
async def set_link(service: str, body: LinkIn, user: CurrentUser, session: SessionDep, request: Request,
                   background: BackgroundTasks) -> Message:
    svc = _svc(request)
    svc.provider(service)
    await svc.set_link(session, user.id, service, body.source_id, body.manga_id, body.remote_id, body.remote_title,
                       body.remote_url, body.remote_chapters, manual=True)
    if body.remote_id:
        latest = (await session.execute(select(ReadingProgress.chapter_number).where(
            ReadingProgress.user_id == user.id, ReadingProgress.source_id == body.source_id,
            ReadingProgress.manga_id == body.manga_id, ReadingProgress.completed.is_(True)).order_by(
            ReadingProgress.chapter_number.desc().nulls_last()).limit(1))).scalar_one_or_none()
        if latest is not None:
            schedule_push(request, background, user.id, {(body.source_id, body.manga_id): latest})
    return Message(message="Сохранено")


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED, summary="Отправить прогресс всей библиотеки")
async def sync_all(user: CurrentUser, session: SessionDep, request: Request, background: BackgroundTasks) -> Message:
    lib = (await session.execute(select(LibraryEntry).where(
        LibraryEntry.user_id == user.id, LibraryEntry.deleted_at.is_(None)).limit(300))).scalars()
    items: dict[tuple[str, str], float | None] = {}
    for e in lib:
        items[(e.source_id, e.manga_id)] = e.last_chapter_number
    schedule_push(request, background, user.id, items, limit=300)
    return Message(message=f"Отправляю прогресс {len(items)} тайтлов")


# ------------------------------------------------------------------ pushing
def schedule_push(request: Request, background: BackgroundTasks, user_id: uuid.UUID,
                  items: dict[tuple[str, str], float | None], *, score: int | None = None,
                  title_hint: str = "", limit: int = 30) -> None:
    """After the response: push chapters (and/or a score) of these titles to every connected tracker."""
    if not items:
        return
    background.add_task(_push, request.app, user_id, dict(list(items.items())[:limit]), score, title_hint)


async def _titles(app, session, user_id: uuid.UUID, source_id: str, manga_id: str, hint: str) -> list[str]:
    names: list[str] = []
    try:
        data = await app.state.sources.title(source_id, manga_id)
        m = data["manga"]
        names = [m.get("title") or "", *(m.get("alt_titles") or [])]
    except Exception:  # noqa: BLE001 — offline source: fall back to what we know
        pass
    if not any(names):
        e = (await session.execute(select(LibraryEntry.title).where(
            LibraryEntry.user_id == user_id, LibraryEntry.source_id == source_id,
            LibraryEntry.manga_id == manga_id))).scalar_one_or_none()
        names = [hint, e or ""]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]


async def _push(app, user_id: uuid.UUID, items: dict[tuple[str, str], float | None], score: int | None,
                hint: str) -> None:
    svc: TrackerService = app.state.trackers
    async with app.state.sessionmaker() as session:
        try:
            if not await svc.accounts(session, user_id):
                return
            for (sid, mid), chapter in items.items():
                titles = await _titles(app, session, user_id, sid, mid, hint)
                if not titles:
                    continue
                s = score
                if s is None and chapter is None:
                    s = (await session.execute(select(Rating.score).where(
                        Rating.user_id == user_id, Rating.source_id == sid, Rating.manga_id == mid))).scalar_one_or_none()
                if chapter is None and s is None:
                    continue
                await svc.push_title(session, user_id, sid, mid, titles=titles, chapter=chapter, score=s)
        except Exception as exc:  # noqa: BLE001 — background job: log, never crash the worker
            log.warning("tracker_push_job_failed", error=str(exc))
