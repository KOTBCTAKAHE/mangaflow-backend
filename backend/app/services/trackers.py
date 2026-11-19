"""Shikimori / MyAnimeList / AniList: OAuth on the server, progress pushed from sync.

The app never sees tracker tokens. When reading progress arrives (progress or
history sync) the server finds the title on every connected tracker (by its
titles and alt titles, or the user's manual choice) and moves the remote
"chapters read" forward — never backwards — and sends the user's rating.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.errors import AppError
from ..core.logging import get_logger
from ..models import TrackerAccount, TrackerLink
from .search import normalize_title

log = get_logger(__name__)

UA = "MangaFlow"
MATCH_THRESHOLD = 92


class TrackerError(AppError):
    def __init__(self, message: str, code: str = "tracker_error", status_code: int = 502) -> None:
        super().__init__(message, code=code, status_code=status_code)


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None


@dataclass
class Candidate:
    remote_id: str
    title: str
    titles: list[str]
    cover: str = ""
    chapters: int | None = None
    year: int | None = None
    url: str = ""

    def out(self) -> dict[str, Any]:
        return {"remote_id": self.remote_id, "title": self.title, "cover": self.cover, "chapters": self.chapters,
                "year": self.year, "url": self.url, "alt_titles": [t for t in self.titles if t != self.title][:3]}


class Provider:
    id = ""
    name = ""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.s = settings
        self.http = client

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    def authorize_url(self, redirect_uri: str, state: str, verifier: str) -> str:
        raise NotImplementedError

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> TokenSet:
        raise NotImplementedError

    async def refresh(self, refresh_token: str) -> TokenSet:
        raise TrackerError("Сессия трекера истекла — подключите его заново", code="tracker_expired", status_code=401)

    async def whoami(self, token: str) -> tuple[str, str]:
        raise NotImplementedError

    async def search(self, token: str, query: str) -> list[Candidate]:
        raise NotImplementedError

    async def push(self, token: str, remote_user_id: str, remote_id: str, *, chapters: int | None,
                   completed: bool, score: int | None) -> None:
        raise NotImplementedError

    @staticmethod
    def _check(r: httpx.Response, what: str) -> httpx.Response:
        if r.status_code == 401:
            raise TrackerError("Трекер отклонил доступ — подключите его заново", code="tracker_unauthorized", status_code=401)
        if r.status_code == 429:
            raise TrackerError("Трекер просит подождать", code="tracker_rate_limited", status_code=429)
        if r.status_code >= 400:
            raise TrackerError(f"{what}: HTTP {r.status_code} {r.text[:200]}")
        return r


class Shikimori(Provider):
    id = "shikimori"
    name = "Shikimori"

    @property
    def configured(self) -> bool:
        return bool(self.s.shikimori_client_id and self.s.shikimori_client_secret)

    @property
    def base(self) -> str:
        return self.s.shikimori_base_url.rstrip("/")

    def authorize_url(self, redirect_uri: str, state: str, verifier: str) -> str:
        return f"{self.base}/oauth/authorize?" + urlencode({
            "client_id": self.s.shikimori_client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "user_rates", "state": state})

    async def _token(self, data: dict[str, str]) -> TokenSet:
        r = self._check(await self.http.post(f"{self.base}/oauth/token", data={
            **data, "client_id": self.s.shikimori_client_id, "client_secret": self.s.shikimori_client_secret,
        }, headers={"User-Agent": UA}), "Shikimori token")
        j = r.json()
        return TokenSet(j["access_token"], j.get("refresh_token"), j.get("expires_in"))

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> TokenSet:
        return await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _h(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "User-Agent": UA}

    async def whoami(self, token: str) -> tuple[str, str]:
        j = self._check(await self.http.get(f"{self.base}/api/users/whoami", headers=self._h(token)), "Shikimori whoami").json()
        return str(j["id"]), j.get("nickname") or ""

    async def search(self, token: str, query: str) -> list[Candidate]:
        r = self._check(await self.http.get(f"{self.base}/api/mangas", params={"search": query, "limit": 10},
                                            headers=self._h(token)), "Shikimori search")
        out = []
        for m in r.json():
            img = (m.get("image") or {}).get("preview") or ""
            titles = [t for t in [m.get("russian"), m.get("name"), *(m.get("english") or []), *(m.get("synonyms") or [])] if t]
            out.append(Candidate(str(m["id"]), m.get("russian") or m.get("name") or "", titles,
                                 cover=f"{self.base}{img}" if img.startswith("/") else img,
                                 chapters=m.get("chapters") or None,
                                 year=int(str(m["aired_on"])[:4]) if m.get("aired_on") else None,
                                 url=f"{self.base}{m.get('url', '')}"))
        return out

    async def push(self, token: str, remote_user_id: str, remote_id: str, *, chapters: int | None,
                   completed: bool, score: int | None) -> None:
        h = self._h(token)
        r = self._check(await self.http.get(f"{self.base}/api/v2/user_rates", params={
            "user_id": remote_user_id, "target_id": remote_id, "target_type": "Manga"}, headers=h), "Shikimori rates")
        rates = r.json()
        body: dict[str, Any] = {}
        if chapters is not None:
            body["chapters"] = chapters
            body["status"] = "completed" if completed else "watching"
        if score is not None:
            body["score"] = score
        if not body:
            return
        if rates:
            self._check(await self.http.patch(f"{self.base}/api/v2/user_rates/{rates[0]['id']}",
                                              json={"user_rate": body}, headers=h), "Shikimori update")
        else:
            self._check(await self.http.post(f"{self.base}/api/v2/user_rates", json={"user_rate": {
                "user_id": int(remote_user_id), "target_id": int(remote_id), "target_type": "Manga",
                "status": "watching", **body}}, headers=h), "Shikimori create")


class AniList(Provider):
    id = "anilist"
    name = "AniList"
    api = "https://graphql.anilist.co"

    @property
    def configured(self) -> bool:
        return bool(self.s.anilist_client_id and self.s.anilist_client_secret)

    def authorize_url(self, redirect_uri: str, state: str, verifier: str) -> str:
        return "https://anilist.co/api/v2/oauth/authorize?" + urlencode({
            "client_id": self.s.anilist_client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state})

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> TokenSet:
        r = self._check(await self.http.post("https://anilist.co/api/v2/oauth/token", json={
            "grant_type": "authorization_code", "client_id": self.s.anilist_client_id,
            "client_secret": self.s.anilist_client_secret, "redirect_uri": redirect_uri, "code": code,
        }, headers={"Accept": "application/json"}), "AniList token")
        j = r.json()
        return TokenSet(j["access_token"], j.get("refresh_token"), j.get("expires_in"))

    async def _gql(self, token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        r = self._check(await self.http.post(self.api, json={"query": query, "variables": variables}, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json"}), "AniList")
        j = r.json()
        if j.get("errors"):
            raise TrackerError(f"AniList: {j['errors'][0].get('message')}")
        return j["data"]

    async def whoami(self, token: str) -> tuple[str, str]:
        v = (await self._gql(token, "query { Viewer { id name } }", {}))["Viewer"]
        return str(v["id"]), v.get("name") or ""

    async def search(self, token: str, query: str) -> list[Candidate]:
        d = await self._gql(token, """query ($q: String) { Page(perPage: 10) { media(search: $q, type: MANGA) {
            id siteUrl chapters startDate { year } coverImage { medium } synonyms
            title { romaji english native } } } }""", {"q": query})
        out = []
        for m in d["Page"]["media"]:
            t = m.get("title") or {}
            titles = [x for x in [t.get("english"), t.get("romaji"), t.get("native"), *(m.get("synonyms") or [])] if x]
            out.append(Candidate(str(m["id"]), t.get("english") or t.get("romaji") or "", titles,
                                 cover=(m.get("coverImage") or {}).get("medium") or "", chapters=m.get("chapters"),
                                 year=(m.get("startDate") or {}).get("year"), url=m.get("siteUrl") or ""))
        return out

    async def push(self, token: str, remote_user_id: str, remote_id: str, *, chapters: int | None,
                   completed: bool, score: int | None) -> None:
        variables: dict[str, Any] = {"id": int(remote_id)}
        if chapters is not None:
            variables["progress"] = chapters
            variables["status"] = "COMPLETED" if completed else "CURRENT"
        if score is not None:
            variables["score"] = score * 10
        await self._gql(token, """mutation ($id: Int, $progress: Int, $status: MediaListStatus, $score: Int) {
            SaveMediaListEntry(mediaId: $id, progress: $progress, status: $status, scoreRaw: $score) { id } }""", variables)


class MyAnimeList(Provider):
    id = "mal"
    name = "MyAnimeList"
    api = "https://api.myanimelist.net/v2"

    @property
    def configured(self) -> bool:
        return bool(self.s.mal_client_id)

    def authorize_url(self, redirect_uri: str, state: str, verifier: str) -> str:
        return "https://myanimelist.net/v1/oauth2/authorize?" + urlencode({
            "response_type": "code", "client_id": self.s.mal_client_id, "code_challenge": verifier,
            "code_challenge_method": "plain", "state": state, "redirect_uri": redirect_uri})

    async def _token(self, data: dict[str, str]) -> TokenSet:
        payload = {**data, "client_id": self.s.mal_client_id}
        if self.s.mal_client_secret:
            payload["client_secret"] = self.s.mal_client_secret
        r = self._check(await self.http.post("https://myanimelist.net/v1/oauth2/token", data=payload), "MAL token")
        j = r.json()
        return TokenSet(j["access_token"], j.get("refresh_token"), j.get("expires_in"))

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> TokenSet:
        return await self._token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                  "redirect_uri": redirect_uri})

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def whoami(self, token: str) -> tuple[str, str]:
        j = self._check(await self.http.get(f"{self.api}/users/@me", headers={"Authorization": f"Bearer {token}"}),
                        "MAL whoami").json()
        return str(j["id"]), j.get("name") or ""

    async def search(self, token: str, query: str) -> list[Candidate]:
        q = query.strip()[:64]
        if len(q) < 3:
            return []
        r = self._check(await self.http.get(f"{self.api}/manga", params={
            "q": q, "limit": 10, "fields": "num_chapters,alternative_titles,start_date,main_picture"},
            headers={"Authorization": f"Bearer {token}"}), "MAL search")
        out = []
        for row in r.json().get("data") or []:
            m = row["node"]
            alt = m.get("alternative_titles") or {}
            titles = [x for x in [m.get("title"), alt.get("en"), alt.get("ja"), *(alt.get("synonyms") or [])] if x]
            out.append(Candidate(str(m["id"]), alt.get("en") or m.get("title") or "", titles,
                                 cover=(m.get("main_picture") or {}).get("medium") or "",
                                 chapters=m.get("num_chapters") or None,
                                 year=int(m["start_date"][:4]) if m.get("start_date") else None,
                                 url=f"https://myanimelist.net/manga/{m['id']}"))
        return out

    async def push(self, token: str, remote_user_id: str, remote_id: str, *, chapters: int | None,
                   completed: bool, score: int | None) -> None:
        data: dict[str, Any] = {}
        if chapters is not None:
            data["num_chapters_read"] = chapters
            data["status"] = "completed" if completed else "reading"
        if score is not None:
            data["score"] = score
        if data:
            self._check(await self.http.patch(f"{self.api}/manga/{remote_id}/my_list_status", data=data,
                                              headers={"Authorization": f"Bearer {token}"}), "MAL update")


PROVIDERS: tuple[type[Provider], ...] = (Shikimori, MyAnimeList, AniList)


def best_match(titles: list[str], candidates: list[Candidate]) -> Candidate | None:
    """Exact (normalised) title match wins; otherwise a very close fuzzy match."""
    wanted = [n for n in (normalize_title(t) for t in titles) if n]
    best, best_score = None, 0.0
    for c in candidates:
        for ct in c.titles:
            n = normalize_title(ct)
            if not n:
                continue
            for w in wanted:
                if n == w:
                    return c
                score = fuzz.ratio(n, w)
                if score > best_score:
                    best, best_score = c, score
    return best if best_score >= MATCH_THRESHOLD else None


class TrackerService:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=True)
        self.providers = {p.id: p(settings, self.client) for p in PROVIDERS}

    async def aclose(self) -> None:
        await self.client.aclose()

    def provider(self, service: str) -> Provider:
        p = self.providers.get(service)
        if p is None:
            raise AppError("Неизвестный трекер", code="tracker_unknown", status_code=404)
        return p

    @staticmethod
    def new_verifier() -> str:
        return secrets.token_urlsafe(64)[:100]  # PKCE verifier: 43..128 chars

    async def account(self, session: AsyncSession, user_id: uuid.UUID, service: str) -> TrackerAccount | None:
        return (await session.execute(select(TrackerAccount).where(
            TrackerAccount.user_id == user_id, TrackerAccount.service == service))).scalar_one_or_none()

    async def accounts(self, session: AsyncSession, user_id: uuid.UUID) -> list[TrackerAccount]:
        return list((await session.execute(select(TrackerAccount).where(TrackerAccount.user_id == user_id))).scalars())

    async def connect(self, session: AsyncSession, user_id: uuid.UUID, service: str, code: str,
                      redirect_uri: str, verifier: str) -> TrackerAccount:
        p = self.provider(service)
        tokens = await p.exchange(code, redirect_uri, verifier)
        remote_id, name = await p.whoami(tokens.access_token)
        acc = await self.account(session, user_id, service) or TrackerAccount(user_id=user_id, service=service)
        self._apply(acc, tokens)
        acc.remote_user_id, acc.remote_username, acc.last_error = remote_id, name, None
        session.add(acc)
        await session.commit()
        return acc

    @staticmethod
    def _apply(acc: TrackerAccount, t: TokenSet) -> None:
        acc.access_token = t.access_token
        if t.refresh_token:
            acc.refresh_token = t.refresh_token
        acc.expires_at = datetime.now(UTC) + timedelta(seconds=t.expires_in) if t.expires_in else None

    async def token(self, session: AsyncSession, acc: TrackerAccount) -> str:
        exp = acc.expires_at
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp and exp - datetime.now(UTC) < timedelta(minutes=5) and acc.refresh_token:
            self._apply(acc, await self.provider(acc.service).refresh(acc.refresh_token))
            await session.commit()
        return acc.access_token

    async def link(self, session: AsyncSession, user_id: uuid.UUID, service: str, source_id: str,
                   manga_id: str) -> TrackerLink | None:
        return (await session.execute(select(TrackerLink).where(
            TrackerLink.user_id == user_id, TrackerLink.service == service,
            TrackerLink.source_id == source_id, TrackerLink.manga_id == manga_id))).scalar_one_or_none()

    async def search(self, session: AsyncSession, user_id: uuid.UUID, service: str, query: str) -> list[Candidate]:
        acc = await self.account(session, user_id, service)
        if acc is None:
            raise AppError("Трекер не подключён", code="tracker_not_connected", status_code=409)
        return await self.provider(service).search(await self.token(session, acc), query)

    async def set_link(self, session: AsyncSession, user_id: uuid.UUID, service: str, source_id: str, manga_id: str,
                       remote_id: str | None, remote_title: str = "", remote_url: str = "",
                       remote_chapters: int | None = None, *, manual: bool = True) -> TrackerLink:
        link = await self.link(session, user_id, service, source_id, manga_id) or TrackerLink(
            user_id=user_id, service=service, source_id=source_id, manga_id=manga_id)
        link.remote_id, link.remote_title, link.remote_url = remote_id, remote_title, remote_url
        link.remote_chapters, link.manual = remote_chapters, manual
        link.pushed_chapter = None  # push again to the new entry
        session.add(link)
        await session.commit()
        return link

    async def push_title(self, session: AsyncSession, user_id: uuid.UUID, source_id: str, manga_id: str, *,
                         titles: list[str], chapter: float | None = None, completed: bool = False,
                         score: int | None = None) -> dict[str, str]:
        """Push progress/score to every connected tracker. Returns service → result."""
        results: dict[str, str] = {}
        for acc in await self.accounts(session, user_id):
            p = self.providers.get(acc.service)
            if p is None or not p.configured:
                continue
            try:
                token = await self.token(session, acc)
                link = await self.link(session, user_id, acc.service, source_id, manga_id)
                if link is None:
                    cand = None
                    for q in [t for t in titles if t][:3]:
                        cand = best_match(titles, await p.search(token, q))
                        if cand:
                            break
                    link = await self.set_link(session, user_id, acc.service, source_id, manga_id,
                                               cand.remote_id if cand else None, cand.title if cand else "",
                                               cand.url if cand else "", cand.chapters if cand else None, manual=False)
                if not link.remote_id:
                    results[acc.service] = "no_match"
                    continue
                ch = int(chapter) if chapter is not None else None
                if ch is not None and link.pushed_chapter is not None and ch <= link.pushed_chapter and score is None:
                    results[acc.service] = "up_to_date"
                    continue
                done = completed or bool(link.remote_chapters and ch and ch >= link.remote_chapters)
                await p.push(token, acc.remote_user_id, link.remote_id, chapters=ch, completed=done, score=score)
                if ch is not None:
                    link.pushed_chapter = max(ch, link.pushed_chapter or 0)
                if score is not None:
                    link.pushed_score = score
                acc.last_error = None
                await session.commit()
                results[acc.service] = "ok"
            except TrackerError as exc:
                acc.last_error = exc.message
                await session.commit()
                results[acc.service] = exc.code
                log.warning("tracker_push_failed", service=acc.service, error=exc.message)
            except httpx.HTTPError as exc:
                results[acc.service] = "network"
                log.warning("tracker_push_failed", service=acc.service, error=str(exc))
        return results
