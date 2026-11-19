"""Our own reverse image search: crawl → index → find the exact page."""
from __future__ import annotations

import hashlib
import io
import tempfile

import httpx
import numpy as np
from PIL import Image, ImageDraw

from mangaflow_sources import SourceRegistry

from app.core.cache import MemoryCache
from app.core.config import Settings
from app.imgindex.embedder import TinyEmbedder, index_views, query_views, tiles, trim_borders
from app.imgindex.main import create_index_app
from app.services.lookup import ImageLookup, parse_own

from .conftest import H, BlockedAdapter, FakeAdapter, OtherAdapter


def page_png(url: str, size=(400, 600)) -> bytes:
    rng = np.random.default_rng(int(hashlib.md5(url.encode()).hexdigest()[:8], 16))
    small = (rng.random((12, 8)) * 255).astype("uint8")
    im = Image.fromarray(small, "L").resize(size, Image.Resampling.NEAREST).convert("RGB")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def pages_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, content=page_png(str(req.url)),
                                                          headers={"content-type": "image/png"}))


def test_views():
    strip = Image.new("RGB", (100, 1000))
    t = tiles(strip)
    assert len(t) > 3 and all(x.size[1] <= 140 for x in t) and t[-1].size == (100, 140)
    assert len(index_views(Image.new("RGB", (100, 140)))) == 1
    assert len(index_views(Image.new("RGB", (300, 140)))) == 3  # spread → halves
    framed = Image.new("RGB", (300, 600), "black")
    framed.paste(Image.fromarray((np.random.default_rng(1).random((300, 200)) * 255).astype("uint8")).convert("RGB"), (50, 150))
    w, h = trim_borders(framed).size
    assert 196 <= w <= 208 and 296 <= h <= 308
    assert len(query_views(framed)) >= 3


async def test_crawl_index_and_search():
    settings = Settings(environment="test", redis_url="memory://", log_json=False, log_level="WARNING",
                        imgindex_dir=tempfile.mkdtemp(prefix="mf-idx-"), image_cache_dir=tempfile.mkdtemp(),
                        imgindex_pages_per_minute=100000, imgindex_chapters_per_title=2)
    from mangaflow_sources import Chapter

    FakeAdapter.chapters_by_id = {m: [Chapter(f"{m}/c/2", "Глава 2", 2.0), Chapter(f"{m}/c/1", "Глава 1", 1.0)]
                                  for m in ("/one-piece", "/berserk", "/solo", "/naruto", "/van-pis")}
    reg = SourceRegistry.create(classes=[FakeAdapter, BlockedAdapter, OtherAdapter])
    app = create_index_app(settings, registry=reg, cache=MemoryCache(), embedder=TinyEmbedder(),
                           image_transport=pages_transport(), start_worker=False)
    async with app.router.lifespan_context(app):
        idx = app.state.index
        # a chapter someone read is queued with priority, ahead of the crawler
        from mangaflow_sources import encode_ref

        idx.enqueue("other", encode_ref("/naruto"), encode_ref("/naruto/c/1"))
        job = idx.store.next_chapter()
        assert job["priority"] == 10 and job["source_id"] == "other"
        planned = 0
        while await idx._plan_more():
            planned += 1
        assert planned >= 3  # fake: 3 titles; "Ван Пис" on `other` is a duplicate of fake's "Ван-Пис"
        while (job := idx.store.next_chapter()) is not None:
            await idx._index_chapter(job)
        st = idx.stats()
        assert st["chapters_queued"] == 0 and st["pages"] >= 9 and st["vectors"] == st["pages"]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://idx") as c:
            assert (await c.get("/stats")).json()["pages"] == st["pages"]
            # the reader's screenshot: same page, a bit darker, with a caption painted over it
            src_url = "https://img.fake/one-piece/c/2/1.jpg"
            im = Image.open(io.BytesIO(page_png(src_url))).point(lambda v: int(v * 0.9))
            ImageDraw.Draw(im).rectangle((0, 560, 120, 600), fill="white")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            r = await c.post("/search", content=buf.getvalue(), headers={"content-type": "image/jpeg"})
            hits = r.json()["hits"]
            assert hits[0]["page_index"] == 1 and hits[0]["title"] in ("Ван-Пис", "Ван Пис")
            assert hits[0]["chapter_title"] == "Глава 2" and hits[0]["similarity"] > 90
            assert (await c.post("/search", content=b"nope")).status_code == 400
            assert (await c.post("/enqueue", json={"source_id": "fake", "manga_id": "x", "chapter_id": "y"})).status_code == 202

    # restart: the index is persisted
    app2 = create_index_app(settings, registry=reg, cache=MemoryCache(), embedder=TinyEmbedder(),
                            image_transport=pages_transport(), start_worker=False)
    async with app2.router.lifespan_context(app2):
        assert len(app2.state.index.store) == st["vectors"]


def test_parse_own():
    j = {"hits": [
        {"source_id": "fake", "manga_id": "m1", "chapter_id": "c2", "page_index": 4, "title": "Берсерк",
         "chapter_title": "", "chapter_number": 12.0, "similarity": 77.0, "cover_url": "https://img/c.jpg"},
        {"source_id": "fake", "manga_id": "m1", "chapter_id": "c3", "page_index": 0, "title": "Берсерк", "similarity": 60},
        {"source_id": "fake", "manga_id": "m2", "chapter_id": "c1", "page_index": 0, "title": "Шум", "similarity": 20},
    ]}
    hits = parse_own(j)
    assert len(hits) == 1 and hits[0]["part"] == "Глава 12, стр. 5" and hits[0]["chapter_id"] == "c2"


async def test_lookup_uses_own_index_first(client, auth, app):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "imgindex":
            if req.url.path == "/enqueue":
                return httpx.Response(202, json={"ok": True})
            return httpx.Response(200, json={"vectors": 1234, "hits": [
                {"source_id": "fake", "manga_id": "L2JlcnNlcms", "chapter_id": "Yzk", "page_index": 2,
                 "title": "Берсерк", "chapter_title": "Глава 9", "chapter_number": 9, "similarity": 81.5,
                 "cover_url": "https://img.fake/berserk.jpg"}]})
        if req.url.host == "api.trace.moe":
            return httpx.Response(200, json={"result": []})
        return httpx.Response(404)

    app.state.settings.imgindex_url = "http://imgindex"
    app.state.settings.saucenao_api_key = ""
    app.state.lookup = ImageLookup(app.state.settings, transport=httpx.MockTransport(handler))
    r = await client.post("/api/v1/lookup/image", headers={**H(auth), "content-type": "image/jpeg"}, content=b"jpg")
    body = r.json()
    c = body["candidates"][0]
    assert c["engine"] == "MangaFlow" and c["confidence"] == "high" and c["chapter_id"] == "Yzk" and c["page_index"] == 2
    assert c["part"] == "Глава 9, стр. 3" and "/api/v1/images/" in c["thumbnail"] and "cover_raw" not in c
    first = c["results"]["items"][0]
    assert first["source_id"] == "fake" and first["id"] == "L2JlcnNlcms"
    own = next(e for e in body["engines"] if e["name"] == "MangaFlow")
    assert own["status"] == "ok" and "1 234" in own["detail"]
    app.state.settings.imgindex_url = ""
