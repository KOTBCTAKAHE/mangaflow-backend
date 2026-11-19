"""«Читать вместе» rooms + «Найти по скриншоту»."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import Field

from ...core.errors import AppError, NotFoundError
from ...core.ratelimit import RateLimit
from ...core.security import decode_access_token
from ...models import User
from ...schemas.common import Schema
from mangaflow_sources import encode_ref

from ...services.lookup import title_matches
from ...services.search import SearchService
from ...services.sources import parse_query
from ..deps import CurrentUser, PresenterDep

router = APIRouter(prefix="/rooms", tags=["rooms"])
lookup_router = APIRouter(prefix="/lookup", tags=["lookup"])

REACTIONS = {"❤️", "😂", "😮", "😭", "🔥", "👍", "😱", "🤯", "👀", "💀"}


class RoomIn(Schema):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    manga_title: str = Field(default="", max_length=512)
    cover_url: str = Field(default="", max_length=2048)
    chapter_id: str = Field(max_length=1024)
    chapter_title: str = Field(default="", max_length=512)
    page: int = Field(default=0, ge=0)


@router.post("", dependencies=[Depends(RateLimit("room_create", 20))])
async def create_room(body: RoomIn, user: CurrentUser, request: Request) -> Any:
    hub = request.app.state.rooms
    info = {**body.model_dump(exclude={"page"}), "host": user.username, "host_id": str(user.id)}
    code = await hub.create(info)
    await hub.set_state(code, {"chapter_id": body.chapter_id, "chapter_title": body.chapter_title, "page": body.page})
    return {"code": code, **info, "state": await hub.state(code)}


@router.get("/{code}")
async def get_room(code: str, user: CurrentUser, request: Request) -> Any:
    hub = request.app.state.rooms
    code = code.strip().upper()
    info = await hub.info(code)
    if info is None:
        raise NotFoundError("Комната не найдена или уже закрыта", code="room_not_found")
    return {"code": code, **info, "state": await hub.state(code), "members": await hub.members(code)}


@router.websocket("/{code}/ws")
async def room_socket(websocket: WebSocket, code: str, token: str = Query("")) -> None:
    app = websocket.app
    hub = app.state.rooms
    code = code.strip().upper()
    try:
        payload = decode_access_token(token)
        uid = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        await websocket.close(code=4401)
        return
    async with app.state.sessionmaker() as session:
        user = await session.get(User, uid)
    info = await hub.info(code)
    if user is None or info is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    name = user.display_name or user.username
    await websocket.send_text(json.dumps({"t": "hello", "room": {"code": code, **info},
                                          "state": await hub.state(code), "me": str(uid)}, ensure_ascii=False))
    await hub.join(code, websocket, str(uid), name)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=120)
            except TimeoutError:
                await websocket.send_text('{"t":"ping"}')
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("t")
            base = {"uid": str(uid), "name": name}
            if t == "page":
                state = {"chapter_id": str(msg.get("chapter_id") or "")[:1024],
                         "chapter_title": str(msg.get("chapter_title") or "")[:512],
                         "page": max(0, int(msg.get("page") or 0))}
                await hub.set_state(code, state)
                await hub.publish(code, {"t": "page", **base, **state})
            elif t == "react" and msg.get("emoji") in REACTIONS:
                await hub.publish(code, {"t": "react", **base, "emoji": msg["emoji"], "page": msg.get("page")})
            elif t == "say":
                text = str(msg.get("text") or "").strip()[:200]
                if text:
                    await hub.publish(code, {"t": "say", **base, "text": text})
            elif t == "pong":
                pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await hub.leave(code, websocket, str(uid), name)


# ------------------------------------------------------------------ lookup
@lookup_router.post("/image", summary="Найти мангу по скриншоту (тело запроса — картинка)",
                    dependencies=[Depends(RateLimit("lookup", 6))])
async def lookup_image(request: Request, user: CurrentUser, pres: PresenterDep) -> Any:
    body = await request.body()
    if not body:
        raise AppError("Пришлите картинку", code="lookup_empty")
    if len(body) > 12 * 1024 * 1024:
        raise AppError("Картинка больше 12 МБ", code="lookup_too_big", status_code=413)
    matches, engines = await request.app.state.lookup.find_all(body, request.headers.get("content-type") or "image/jpeg")
    sources = request.app.state.sources
    infos = {i.id: i for i in sources.registry.infos()}
    search: SearchService = request.app.state.search
    ids = [i for i, info in infos.items() if not info.is_nsfw]

    async def results_for(m: dict[str, Any], n_queries: int) -> dict[str, Any] | None:
        """Our sources' titles that really are this match — not whatever the text search returned."""
        names = list(dict.fromkeys(m.get("names") or [m["title"]]))
        found = await asyncio.gather(*(search.search(parse_query(q=q), ids) for q in names[:n_queries]))
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for res in found:
            for g in res["items"]:
                if g["key"] in seen or not any(title_matches(s["title"], names) for s in g["sources"]):
                    continue
                seen.add(g["key"])
                items.append(g)
        if m.get("md_id"):
            # the exact MangaDex title, even when the text search found nothing
            have = {(s["source_id"], s["id"]) for g in items for s in g["sources"]}
            for sid in ("mangadex_ru", "mangadex"):
                ref = encode_ref(m["md_id"])
                if sid in infos and (sid, ref) not in have:
                    card = {"id": ref, "title": names[0], "cover_url": "", "source_id": sid, "source_name": infos[sid].name}
                    items.insert(0, {**card, "key": f"{sid}:{ref}", "type": None, "score": 1.0, "sources": [card]})
        if m.get("chapter_id") and m.get("source_id") in infos:
            # our own index knows the exact title: it goes first, even if the text search missed it
            sid, mid = m["source_id"], m["manga_id"]
            items = [g for g in items if all(not (s["source_id"] == sid and s["id"] == mid) for s in g["sources"])]
            card = {"id": mid, "title": m["title"], "cover_url": m.get("cover_raw") or "", "source_id": sid,
                    "source_name": infos[sid].name}
            items.insert(0, {**card, "key": f"{sid}:{mid}", "type": None, "score": 1.0, "sources": [card]})
        if not items:
            return None
        return pres.search({"query": names[0], "page": 1, "has_next": False, "items": items, "errors": [],
                            "sources_queried": ids})

    for m in matches:
        if m.get("engine") == "MangaFlow" and m.get("thumbnail") and m.get("source_id"):
            m["cover_raw"] = m["thumbnail"]
            m["thumbnail"] = pres.signer.url(m["source_id"], m["thumbnail"], cover=True)
    top = matches[:3]
    per = await asyncio.gather(*(results_for(m, 3 if i == 0 else 2) for i, m in enumerate(top)))
    candidates = [{**{k: v for k, v in m.items() if k != "cover_raw"}, "results": r} for m, r in zip(top, per, strict=True)]
    matches = [{k: v for k, v in m.items() if k != "cover_raw"} for m in matches]
    results = per[0] if per else None
    return {"matches": matches[:6], "candidates": candidates, "results": results, "engines": engines}
