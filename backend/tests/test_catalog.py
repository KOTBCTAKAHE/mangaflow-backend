from __future__ import annotations

from mangaflow_sources import encode_ref

from app.services.search import normalize_title

from .conftest import FakeAdapter, H


async def test_sources_and_errors(client):
    r = await client.get("/api/v1/sources")
    assert {s["id"] for s in r.json()} == {"fake", "blocked", "other"}
    fake = next(s for s in r.json() if s["id"] == "fake")
    assert fake["capabilities"]["genre_exclude"] is True and fake["genres"][0] == {"id": "action", "name": "Экшен"}
    r = await client.get("/api/v1/sources/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_source"
    assert r.json()["error"]["request_id"]
    health = await client.get("/api/v1/sources/blocked/health")
    assert health.json()["ok"] is False and health.json()["error_code"] == "source_blocked"


async def test_browse_title_pages_and_cache(client):
    r = await client.get("/api/v1/manga/fake", params={"page": 1})
    body = r.json()
    assert r.status_code == 200 and body["has_next"] is True and len(body["items"]) == 3
    card = body["items"][0]
    assert card["id"] == encode_ref("/one-piece")
    assert card["cover_url"].startswith("http://test/api/v1/images/")
    await client.get("/api/v1/manga/fake", params={"page": 1})
    assert FakeAdapter.calls.count("popular:1") == 1  # second call served from cache

    t = await client.get(f"/api/v1/manga/fake/{card['id']}")
    assert t.status_code == 200
    data = t.json()
    assert data["manga"]["title"] == "Ван-Пис" and data["manga"]["chapters_count"] == 2
    assert data["manga"]["status"] == "ongoing"
    chapter_id = data["chapters"][0]["id"]

    asc = await client.get(f"/api/v1/manga/fake/{card['id']}/chapters", params={"order": "asc"})
    assert [c["number"] for c in asc.json()] == [1.0, 2.0]

    p = await client.get(f"/api/v1/chapters/fake/{chapter_id}/pages")
    pages = p.json()["pages"]
    assert len(pages) == 3 and pages[0]["url"].startswith("http://test/api/v1/images/")

    img = await client.get(pages[0]["url"].replace("http://test", ""))
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert "immutable" in img.headers["cache-control"]


async def test_invalid_ids_and_filters(client):
    assert (await client.get("/api/v1/manga/fake/%%%")).status_code in (400, 404)
    r = await client.get("/api/v1/manga/fake", params={"sort": "weird"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_filter"
    forged = await client.get("/api/v1/images/abc.def")
    assert forged.status_code == 404 and forged.json()["error"]["code"] == "image_token_invalid"


async def test_source_error_is_mapped(client):
    r = await client.get("/api/v1/manga/blocked")
    assert r.status_code == 502 and r.json()["error"]["code"] == "source_blocked"


async def test_global_search_merges_and_reports_errors(client, auth):
    r = await client.get("/api/v1/search", params={"q": "ван пис"}, headers=H(auth))
    body = r.json()
    assert r.status_code == 200
    top = body["items"][0]
    assert {s["source_id"] for s in top["sources"]} == {"fake", "other"}  # "Ван-Пис" ≈ "Ван Пис"
    assert [e["source_id"] for e in body["errors"]] == ["blocked"]
    hist = await client.get("/api/v1/search/history", headers=H(auth))
    assert hist.json() == ["ван пис"]
    only = await client.get("/api/v1/search", params={"q": "наруто", "sources": ["other"]})
    assert [i["title"] for i in only.json()["items"]] == ["Наруто"]
    bad = await client.get("/api/v1/search", params={"q": "x", "sources": ["zzz"]})
    assert bad.status_code == 422


def test_normalize_title():
    assert normalize_title("Ван-Пис (One Piece)") == "ван пис"
    assert normalize_title("Ёжик  в тумане!") == "ежик в тумане"


async def test_home_feed_with_recommendations(client, auth):
    lib = {"source_id": "fake", "manga_id": encode_ref("/one-piece"), "title": "Ван-Пис",
           "updated_at": "2026-01-01T00:00:00Z", "status": "reading"}
    await client.put("/api/v1/library", json=lib, headers=H(auth))
    r = await client.get("/api/v1/manga/fake/feed/home", headers=H(auth))
    sections = {s["key"]: s for s in r.json()["sections"]}
    assert "popular" in sections and "latest" in sections
    assert "recommended" in sections
    assert all(c["id"] != encode_ref("/one-piece") for c in sections["recommended"]["items"])
    assert any(c.startswith("search::action") for c in FakeAdapter.calls)


async def test_download_manifest(client):
    ch = encode_ref("/one-piece/c/1")
    r = await client.post("/api/v1/downloads/manifest", json={"source_id": "fake", "chapter_ids": [ch, "!!bad"]})
    chapters = r.json()["chapters"]
    assert chapters[0]["ok"] and len(chapters[0]["pages"]) == 3
    assert chapters[1]["ok"] is False


async def test_health(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok"
    assert r.headers["x-request-id"]
