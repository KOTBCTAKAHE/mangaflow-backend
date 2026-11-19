from __future__ import annotations

import json

import httpx
import pytest
from mangaflow_sources import encode_ref
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.services.lookup import ImageLookup, chapter_from_part, parse_saucenao
from app.services.trackers import Candidate, TrackerService, best_match

from .conftest import H, register

OP = encode_ref("/one-piece")


async def _two(client):
    a = await register(client, "alice")
    b = await register(client, "bob")
    return ({"Authorization": f"Bearer {a['tokens']['access_token']}"},
            {"Authorization": f"Bearer {b['tokens']['access_token']}"}, a, b)


async def test_friends_ratings_recommendations(client):
    ha, hb, a, b = await _two(client)
    assert (await client.post("/api/v1/friends", json={"username": "nobody"}, headers=ha)).status_code == 404
    assert (await client.post("/api/v1/friends", json={"username": "alice"}, headers=ha)).status_code == 409
    r = await client.post("/api/v1/friends", json={"username": "@Bob"}, headers=ha)
    assert r.status_code == 201
    inc = (await client.get("/api/v1/friends", headers=hb)).json()
    assert [x["user"]["username"] for x in inc["incoming"]] == ["alice"] and not inc["friends"]
    # profile is private until accepted
    bob_id = b["user"]["id"]
    assert (await client.get(f"/api/v1/friends/{bob_id}/profile", headers=ha)).status_code == 403
    assert (await client.post(f"/api/v1/friends/requests/{inc['incoming'][0]['id']}/accept", headers=hb)).status_code == 200

    # bob reads and rates; alice sees it
    await client.post("/api/v1/history", headers=hb, json={"items": [{
        "id": "2b0a4c9e-6c1f-4c55-9a43-5d0c6b0f7a11", "source_id": "fake", "manga_id": OP, "manga_title": "Ван-Пис",
        "chapter_id": "c1", "chapter_title": "Глава 1", "chapter_number": 1, "started_at": "2026-11-18T10:00:00Z",
        "read_at": "2026-11-18T10:10:00Z"}]})
    r = await client.put("/api/v1/ratings", headers=hb, json={"source_id": "fake", "manga_id": OP,
                                                             "manga_title": "Ван-Пис", "score": 9, "review": "Шедевр"})
    assert r.status_code == 200 and r.json()["score"] == 9
    fr = (await client.get("/api/v1/friends", headers=ha)).json()["friends"]
    assert fr[0]["user"]["username"] == "bob" and fr[0]["reading"]["manga_title"] == "Ван-Пис"
    prof = (await client.get(f"/api/v1/friends/{bob_id}/profile", headers=ha)).json()
    assert prof["ratings"][0]["score"] == 9 and prof["reading"][0]["chapter_title"] == "Глава 1"
    feed = (await client.get("/api/v1/friends/activity", headers=ha)).json()["items"]
    assert {i["kind"] for i in feed} == {"read", "rated"}
    tr = (await client.get("/api/v1/ratings/title", headers=ha, params={"source_id": "fake", "manga_id": OP})).json()
    assert tr["mine"] is None and tr["friends"][0]["score"] == 9 and tr["average"] == 9.0

    # recommendations only to friends
    r = await client.post("/api/v1/recommendations", headers=ha, json={
        "to_user_ids": [bob_id], "source_id": "fake", "manga_id": OP, "manga_title": "Ван-Пис", "message": "Читай!"})
    assert r.status_code == 200
    recs = (await client.get("/api/v1/recommendations", headers=hb)).json()
    assert recs["unseen"] == 1 and recs["items"][0]["from"]["username"] == "alice"
    await client.post("/api/v1/recommendations/seen", headers=hb)
    assert (await client.get("/api/v1/recommendations", headers=hb)).json()["unseen"] == 0
    stranger = await register(client, "carol")
    hc = {"Authorization": f"Bearer {stranger['tokens']['access_token']}"}
    r = await client.post("/api/v1/recommendations", headers=hc, json={
        "to_user_ids": [bob_id], "source_id": "fake", "manga_id": OP})
    assert r.status_code == 403

    await client.delete(f"/api/v1/friends/{bob_id}", headers=ha)
    assert (await client.get("/api/v1/friends", headers=ha)).json()["friends"] == []


async def test_comments(client):
    ha, hb, *_ = await _two(client)
    q = {"source_id": "fake", "chapter_id": "c1"}
    c = (await client.post("/api/v1/comments", headers=ha, json={**q, "manga_id": OP, "text": " Вау! ", "page_index": 3})).json()
    assert c["text"] == "Вау!" and c["mine"]
    lst = (await client.get("/api/v1/comments", headers=hb, params=q)).json()
    assert lst["total"] == 1 and not lst["items"][0]["mine"] and lst["items"][0]["user"]["username"] == "alice"
    assert (await client.post(f"/api/v1/comments/{c['id']}/like", headers=hb)).json() == {"likes": 1, "liked": True}
    assert (await client.post(f"/api/v1/comments/{c['id']}/like", headers=hb)).json() == {"likes": 0, "liked": False}
    assert (await client.delete(f"/api/v1/comments/{c['id']}", headers=hb)).status_code == 404  # not mine
    assert (await client.delete(f"/api/v1/comments/{c['id']}", headers=ha)).status_code == 204
    assert (await client.get("/api/v1/comments/count", headers=hb, params=q)).json() == {"count": 0}


def test_best_match_prefers_exact_titles():
    cands = [Candidate("1", "One Punch-Man", ["One Punch-Man", "Ванпанчмен"]),
             Candidate("2", "One Piece", ["One Piece", "Ван-Пис", "ワンピース"])]
    assert best_match(["Ван Пис", "One Piece"], cands).remote_id == "2"
    assert best_match(["Совсем другое"], cands) is None


async def test_tracker_oauth_and_push(client, auth, app):
    calls: list[tuple[str, str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.content.decode() if req.content else ""))
        p = req.url.path
        if p == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 86400})
        if p == "/api/users/whoami":
            return httpx.Response(200, json={"id": 77, "nickname": "neko"})
        if p == "/api/mangas":
            return httpx.Response(200, json=[{"id": 13, "name": "One Piece", "russian": "Ван-Пис", "url": "/mangas/13",
                                              "chapters": 1100, "image": {"preview": "/p.jpg"}}])
        if p == "/api/v2/user_rates" and req.method == "GET":
            return httpx.Response(200, json=[])
        if p == "/api/v2/user_rates" and req.method == "POST":
            return httpx.Response(201, json={"id": 5})
        return httpx.Response(404)

    app.state.settings.shikimori_client_id = "cid"
    app.state.settings.shikimori_client_secret = "sec"
    app.state.trackers = TrackerService(app.state.settings, transport=httpx.MockTransport(handler))
    h = H(auth)
    lst = (await client.get("/api/v1/trackers", headers=h)).json()["items"]
    shiki = next(i for i in lst if i["service"] == "shikimori")
    assert shiki["configured"] and not shiki["connected"]
    assert not next(i for i in lst if i["service"] == "mal")["configured"]
    url = (await client.post("/api/v1/trackers/shikimori/authorize", headers=h)).json()["url"]
    state = httpx.URL(url).params["state"]
    assert (await client.get("/api/v1/trackers/shikimori/callback", params={"code": "x", "state": "bad"})).status_code == 400
    r = await client.get("/api/v1/trackers/shikimori/callback", params={"code": "c0de", "state": state})
    assert r.status_code == 200 and "neko" in r.text
    assert next(i for i in (await client.get("/api/v1/trackers", headers=h)).json()["items"]
                if i["service"] == "shikimori")["username"] == "neko"

    # finishing chapter 3 pushes progress to the auto-matched entry
    await client.put("/api/v1/progress", headers=h, json={
        "source_id": "fake", "manga_id": OP, "chapter_id": "c3", "chapter_number": 3, "page_index": 9,
        "page_count": 10, "completed": True, "updated_at": "2026-11-19T10:00:00Z"})
    posted = [c for c in calls if c[0] == "POST" and c[1] == "/api/v2/user_rates"]
    assert posted and json.loads(posted[0][2])["user_rate"]["chapters"] == 3
    links = (await client.get("/api/v1/trackers/links", headers=h, params={"source_id": "fake", "manga_id": OP})).json()
    assert links["items"][0]["remote_id"] == "13" and links["items"][0]["chapters_sent"] == 3
    # the same chapter again: nothing new is sent
    n = len(calls)
    await client.put("/api/v1/progress", headers=h, json={
        "source_id": "fake", "manga_id": OP, "chapter_id": "c3", "chapter_number": 3, "page_index": 9,
        "page_count": 10, "completed": True, "updated_at": "2026-11-19T10:05:00Z"})
    assert len(calls) == n
    found = (await client.get("/api/v1/trackers/shikimori/search", headers=h, params={"q": "ван"})).json()["items"]
    assert found[0]["title"] == "Ван-Пис" and found[0]["cover"].endswith("/p.jpg")
    assert (await client.delete("/api/v1/trackers/shikimori", headers=h)).status_code == 204


def test_saucenao_parsing():
    j = {"results": [
        {"header": {"similarity": "91.2", "index_id": 37, "index_name": "MangaDex", "thumbnail": "t"},
         "data": {"source": "Chainsaw Man", "part": " - Chapter 12", "ext_urls": ["https://mangadex.org/x"]}},
        {"header": {"similarity": "60", "index_id": 5, "index_name": "Pixiv"}, "data": {"title": "fanart"}},
        {"header": {"similarity": "30", "index_id": 37}, "data": {"source": "noise"}},
    ]}
    hits = parse_saucenao(j)
    assert [h["title"] for h in hits] == ["Chainsaw Man"]  # pixiv art boards are not manga
    assert hits[0]["chapter_number"] == 12 and hits[0]["is_manga"]
    assert chapter_from_part("Vol.2 Ch.15.5") == 15.5


async def test_lookup_requires_key_then_searches(client, auth, app):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.trace.moe":
            return httpx.Response(200, json={"result": []})
        assert req.url.params["api_key"] == "k"
        return httpx.Response(200, json={"results": [{"header": {"similarity": "88", "index_id": 37},
                                                      "data": {"source": "Наруто", "part": "Chapter 2"}}]})

    app.state.lookup = ImageLookup(app.state.settings, transport=httpx.MockTransport(handler))
    r = await client.post("/api/v1/lookup/image", headers={**H(auth), "content-type": "image/png"}, content=b"png")
    assert r.status_code == 200 and r.json()["matches"] == []
    assert r.json()["engines"][0]["status"] == "no_key"
    app.state.settings.saucenao_api_key = "k"
    r = await client.post("/api/v1/lookup/image", headers={**H(auth), "content-type": "image/png"}, content=b"png")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matches"][0]["title"] == "Наруто" and body["matches"][0]["chapter_number"] == 2
    assert body["results"]["items"][0]["title"] == "Наруто"
    c = body["candidates"][0]
    assert c["confidence"] == "high" and c["results"]["items"][0]["title"] == "Наруто"
    assert all(i["title"] == "Наруто" for i in c["results"]["items"])  # unrelated hits filtered out


def test_rooms_websocket(app):
    with TestClient(app) as tc:
        a = tc.post("/api/v1/auth/register", json={"email": "r1@example.com", "username": "roomer1", "password": "secret123", "device_name": "t"}).json()
        b = tc.post("/api/v1/auth/register", json={"email": "r2@example.com", "username": "roomer2", "password": "secret123", "device_name": "t"}).json()
        ta, tb = a["tokens"]["access_token"], b["tokens"]["access_token"]
        room = tc.post("/api/v1/rooms", headers={"Authorization": f"Bearer {ta}"}, json={
            "source_id": "fake", "manga_id": OP, "manga_title": "Ван-Пис", "chapter_id": "c1", "chapter_title": "Глава 1"}).json()
        code = room["code"]
        assert len(code) == 6 and tc.get(f"/api/v1/rooms/{code.lower()}", headers={"Authorization": f"Bearer {tb}"}).status_code == 200
        with tc.websocket_connect(f"/api/v1/rooms/{code}/ws?token={ta}") as wa:
            assert wa.receive_json()["t"] == "hello"
            assert wa.receive_json()["t"] == "join"
            with tc.websocket_connect(f"/api/v1/rooms/{code}/ws?token={tb}") as wb:
                hello = wb.receive_json()
                assert hello["t"] == "hello" and hello["state"]["chapter_id"] == "c1"
                assert wb.receive_json()["t"] == "join"
                joined = wa.receive_json()
                assert joined["t"] == "join" and joined["name"] == "roomer2" and len(joined["members"]) == 2
                wa.send_json({"t": "page", "chapter_id": "c1", "page": 4})
                assert wb.receive_json()["page"] == 4
                assert wa.receive_json()["page"] == 4  # echo to the sender too
                wb.send_json({"t": "react", "emoji": "🔥", "page": 4})
                assert wa.receive_json()["emoji"] == "🔥"
                wb.send_json({"t": "react", "emoji": "not-allowed"})
                wb.send_json({"t": "say", "text": "ахах"})
                assert wa.receive_json()["text"] == "ахах"
        assert tc.get(f"/api/v1/rooms/{code}", headers={"Authorization": f"Bearer {ta}"}).json()["state"]["page"] == 4
        with pytest.raises(WebSocketDisconnect) as closed, tc.websocket_connect(f"/api/v1/rooms/{code}/ws?token=bad") as bad:
            bad.receive_json()
        assert closed.value.code == 4401


async def test_lookup_trace_moe_anime_frame_finds_the_manga(client, auth, app):
    from app.services.lookup import parse_trace

    assert parse_trace({"result": [{"similarity": 0.5, "anilist": {"id": 1, "title": {"romaji": "x"}}}]}) == []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.trace.moe":
            assert "cutBorders" in str(req.url)
            return httpx.Response(200, json={"result": [{"similarity": 0.95, "episode": 3, "image": "t.jpg",
                                                         "anilist": {"id": 20, "title": {"romaji": "Naruto"}}}]})
        if req.url.host == "graphql.anilist.co":
            return httpx.Response(200, json={"data": {"Media": {"relations": {"edges": [
                {"relationType": "SOURCE", "node": {"type": "MANGA", "title": {"romaji": "NARUTO"}, "synonyms": ["Наруто"]}}]}}}})
        return httpx.Response(404)

    app.state.settings.saucenao_api_key = ""
    app.state.lookup = ImageLookup(app.state.settings, transport=httpx.MockTransport(handler))
    r = await client.post("/api/v1/lookup/image", headers={**H(auth), "content-type": "image/jpeg"}, content=b"jpg")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {e["name"]: e["status"] for e in body["engines"]} == {"SauceNAO": "no_key", "trace.moe": "ok"}
    m = body["matches"][0]
    assert m["engine"] == "trace.moe" and m["names"][0] == "Наруто" and "серия 3" in m["part"]
    assert body["results"]["items"][0]["title"] == "Наруто"


async def test_lookup_mangadex_hit_gets_russian_titles(client, auth, app):
    def handler(req: httpx.Request) -> httpx.Response:
        h = req.url.host
        if h == "saucenao.com":
            return httpx.Response(200, json={"results": [{"header": {"similarity": "55", "index_id": 37, "index_name": "MangaDex"},
                                                          "data": {"source": "One Piece", "part": "Chapter 5",
                                                                   "md_id": "a1c7c817-4e59-43b7-9365-09675a149a6f"}}]})
        if h == "api.trace.moe":
            return httpx.Response(200, json={"result": []})
        if h == "api.mangadex.org":
            return httpx.Response(200, json={"data": {"attributes": {"title": {"en": "One Piece"},
                                                                     "altTitles": [{"ru": "Наруто"}, {"ja": "x"}]}}})
        return httpx.Response(404)

    app.state.settings.saucenao_api_key = "k"
    app.state.lookup = ImageLookup(app.state.settings, transport=httpx.MockTransport(handler))
    r = await client.post("/api/v1/lookup/image", headers={**H(auth), "content-type": "image/jpeg"}, content=b"jpg")
    body = r.json()
    assert body["matches"][0]["similarity"] == 55 and body["matches"][0]["names"][0] == "Наруто"
    assert body["results"]["items"][0]["title"] == "Наруто"
    assert {e["name"]: e["status"] for e in body["engines"]} == {"SauceNAO": "ok", "trace.moe": "nothing"}


def test_lookup_precision_helpers():
    from app.services.lookup import confidence, merge_matches, parse_trace, title_matches

    names = ["Наруто", "Naruto"]
    assert title_matches("Наруто", names) and title_matches("NARUTO", names)
    assert title_matches("Наруто: Ураганные хроники", ["Наруто: Ураганные"])
    assert not title_matches("Боруто", names) and not title_matches("Ван-Пис", names)
    assert not title_matches("Нар", names)
    assert parse_trace({"result": [{"similarity": 0.89, "anilist": {"id": 1, "title": {"romaji": "x"}}}]}) == []
    assert confidence("trace.moe", 96, False) == 2 and confidence("SauceNAO", 58, True) == 0
    m = merge_matches([
        {"engine": "SauceNAO", "similarity": 70.0, "is_manga": True, "title": "Naruto", "names": ["Наруто", "Naruto"], "part": "Ch 3"},
        {"engine": "trace.moe", "similarity": 93.0, "is_manga": False, "title": "NARUTO", "names": ["Naruto"], "part": "аниме"},
        {"engine": "SauceNAO", "similarity": 52.0, "is_manga": True, "title": "Bleach", "names": ["Bleach"], "part": ""},
    ])
    # two engines agree → high confidence, the weak unrelated hit is dropped
    assert [x["title"] for x in m] == ["Naruto"] and m[0]["confidence"] == "high"
    assert m[0]["engines"] == ["SauceNAO", "trace.moe"] and m[0]["part"] == "Ch 3"
