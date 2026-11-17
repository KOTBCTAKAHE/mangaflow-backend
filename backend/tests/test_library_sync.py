from __future__ import annotations

import uuid

from mangaflow_sources import Chapter, encode_ref

from app.services.stats import streaks
from app.services.updates import check_updates

from .conftest import FakeAdapter, H, register

OP = encode_ref("/one-piece")


def entry(**kw):
    base = {"source_id": "fake", "manga_id": OP, "title": "Ван-Пис", "cover_url": "c", "status": "planned",
            "updated_at": "2026-01-01T10:00:00Z"}
    base.update(kw)
    return base


async def test_library_crud_and_lww(client, auth):
    r = await client.put("/api/v1/library", json=entry(), headers=H(auth))
    assert r.status_code == 200 and r.json()["status"] == "planned"
    newer = await client.post("/api/v1/library/sync", headers=H(auth),
                              json={"items": [entry(status="reading", updated_at="2026-01-02T00:00:00Z")]})
    assert newer.json()["items"][0]["status"] == "reading"
    stale = await client.post("/api/v1/library/sync", headers=H(auth),
                              json={"items": [entry(status="dropped", updated_at="2025-12-01T00:00:00Z")]})
    assert stale.json()["items"][0]["status"] == "reading"

    patched = await client.patch(f"/api/v1/library/fake/{OP}", headers=H(auth), json={"is_favorite": True})
    assert patched.json()["is_favorite"] is True
    favs = await client.get("/api/v1/library", params={"favorite": True}, headers=H(auth))
    assert len(favs.json()["items"]) == 1

    server_time = favs.json()["server_time"]
    assert (await client.delete(f"/api/v1/library/fake/{OP}", headers=H(auth))).status_code == 204
    assert (await client.get("/api/v1/library", headers=H(auth))).json()["items"] == []
    delta = await client.get("/api/v1/library", params={"since": server_time}, headers=H(auth))
    assert delta.json()["items"][0]["deleted"] is True  # tombstone delivered to other devices


async def test_users_are_isolated(client, auth):
    await client.put("/api/v1/library", json=entry(), headers=H(auth))
    other = await register(client, "intruder")
    hdr = {"Authorization": f"Bearer {other['tokens']['access_token']}"}
    assert (await client.get("/api/v1/library", headers=hdr)).json()["items"] == []
    assert (await client.get(f"/api/v1/library/fake/{OP}", headers=hdr)).status_code == 404


async def test_collections(client, auth):
    e = (await client.put("/api/v1/library", json=entry(), headers=H(auth))).json()
    c = await client.post("/api/v1/collections", json={"name": "Сёнэн"}, headers=H(auth))
    assert c.status_code == 201
    cid = c.json()["id"]
    dup = await client.post("/api/v1/collections", json={"name": "сёнэн"}, headers=H(auth))
    assert dup.status_code == 409
    added = await client.post(f"/api/v1/collections/{cid}/items", json={"entry_ids": [e["id"]]}, headers=H(auth))
    assert added.json()["entry_ids"] == [e["id"]]
    lib = await client.get("/api/v1/library", params={"collection_id": cid}, headers=H(auth))
    assert lib.json()["items"][0]["collection_ids"] == [cid]
    await client.delete(f"/api/v1/collections/{cid}", headers=H(auth))
    assert (await client.get("/api/v1/collections", headers=H(auth))).json() == []


async def test_progress_monotonic_and_continue(client, auth):
    await client.put("/api/v1/library", json=entry(), headers=H(auth))
    p = {"source_id": "fake", "manga_id": OP, "chapter_id": "c1", "chapter_number": 1, "page_index": 5,
         "page_count": 20, "completed": True, "updated_at": "2026-02-01T00:00:00Z"}
    await client.put("/api/v1/progress", json=p, headers=H(auth))
    older = {**p, "page_index": 2, "completed": False, "updated_at": "2026-01-01T00:00:00Z"}
    r = await client.post("/api/v1/progress/batch", json={"items": [older]}, headers=H(auth))
    assert r.json()["items"][0]["page_index"] == 5 and r.json()["items"][0]["completed"] is True
    reopened = {**p, "page_index": 0, "completed": False, "updated_at": "2026-03-01T00:00:00Z"}
    r = await client.put("/api/v1/progress", json=reopened, headers=H(auth))
    assert r.json()["completed"] is True  # completion is never lost
    lib = (await client.get(f"/api/v1/library/fake/{OP}", headers=H(auth))).json()
    assert lib["status"] == "reading" and lib["last_chapter_number"] == 1
    cont = (await client.get("/api/v1/progress/continue", headers=H(auth))).json()
    assert cont[0]["chapter_id"] == "c1" and cont[0]["title"] == "Ван-Пис"


async def test_history_idempotent_stats_and_backup(client, auth):
    hid = str(uuid.uuid4())
    h = {"id": hid, "source_id": "fake", "manga_id": OP, "chapter_id": "c1", "chapter_title": "Глава 1",
         "started_at": "2026-02-01T10:00:00Z", "read_at": "2026-02-01T10:05:00Z", "duration_seconds": 300,
         "pages_read": 20}
    await client.post("/api/v1/history", json={"items": [h]}, headers=H(auth))
    await client.post("/api/v1/history", json={"items": [{**h, "duration_seconds": 400}]}, headers=H(auth))
    items = (await client.get("/api/v1/history", headers=H(auth))).json()["items"]
    assert len(items) == 1 and items[0]["duration_seconds"] == 400

    await client.put("/api/v1/library", json=entry(status="reading"), headers=H(auth))
    stats = (await client.get("/api/v1/users/me/stats", headers=H(auth))).json()
    assert stats["total_seconds"] == 400 and stats["titles_read"] == 1 and stats["by_status"] == {"reading": 1}
    assert stats["activity"][0]["chapters"] == 1

    backup = (await client.get("/api/v1/users/me/export", headers=H(auth))).json()
    assert backup["format"] == "mangaflow-backup" and len(backup["library"]) == 1

    other = await register(client, "restorer")
    hdr = {"Authorization": f"Bearer {other['tokens']['access_token']}"}
    res = await client.post("/api/v1/users/me/import", json=backup, headers=hdr)
    assert res.status_code == 200 and res.json()["history"] == 1
    assert len((await client.get("/api/v1/library", headers=hdr)).json()["items"]) == 1

    await client.delete("/api/v1/history", headers=H(auth))
    assert (await client.get("/api/v1/history", headers=H(auth))).json()["items"] == []


async def test_downloads_registry(client, auth):
    d = {"device_id": "phone-1", "source_id": "fake", "manga_id": OP, "chapter_id": "c1", "page_count": 3}
    await client.post("/api/v1/downloads", json={"items": [d]}, headers=H(auth))
    await client.post("/api/v1/downloads", json={"items": [{**d, "size_bytes": 999}]}, headers=H(auth))
    items = (await client.get("/api/v1/downloads", params={"device_id": "phone-1"}, headers=H(auth))).json()
    assert len(items) == 1 and items[0]["size_bytes"] == 999


async def test_update_checker(client, app, auth):
    await client.put("/api/v1/library", json=entry(status="reading"), headers=H(auth))
    maker, sources = app.state.sessionmaker, app.state.sources
    first = await check_updates(maker, sources)
    assert first["new_chapters"] == 0  # baseline only
    FakeAdapter.chapters_by_id["/one-piece"] = [Chapter("/one-piece/c/3", "Глава 3", 3.0),
                                                Chapter("/one-piece/c/2", "Глава 2", 2.0),
                                                Chapter("/one-piece/c/1", "Глава 1", 1.0)]
    second = await check_updates(maker, sources)
    assert second["new_chapters"] == 1
    feed = (await client.get("/api/v1/updates", params={"unseen": True}, headers=H(auth))).json()
    assert feed[0]["chapter_title"] == "Глава 3"
    lib = (await client.get(f"/api/v1/library/fake/{OP}", headers=H(auth))).json()
    assert lib["unread_updates"] == 1 and lib["total_chapters"] == 3
    await client.post("/api/v1/updates/seen", json={"all": True}, headers=H(auth))
    assert (await client.get("/api/v1/updates", params={"unseen": True}, headers=H(auth))).json() == []


def test_streaks():
    from datetime import date
    days = {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 10), date(2026, 1, 11)}
    assert streaks(days, date(2026, 1, 11)) == (2, 3)
    assert streaks(days, date(2026, 1, 13)) == (0, 3)
