"""MangaFlow's own reverse image search («как SauceNAO, только по нашим источникам»).

* Chapters people read are indexed first (the API enqueues them on reading progress).
* A polite crawler covers popular titles of every non-NSFW source in the background:
  a spread of chapters per title, deduplicated across sources by title.
* ``POST /search`` with an image body → the exact source, title, chapter and page.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from ..core.config import Settings
from ..core.logging import get_logger
from ..services.images import ImageProxy
from ..services.search import normalize_title
from ..services.sources import SourceService, parse_query
from .embedder import Embedder, index_views, open_image, query_views, sha256
from .store import Store

log = get_logger(__name__)

READ_PRIORITY = 10
MAX_QUERY_BYTES = 12 * 1024 * 1024


async def ensure_model(settings: Settings, root: Path) -> Path:
    """Download the ONNX model once into the index volume (checked against its .sha256)."""
    path = root / Path(settings.imgindex_model_url).name
    if path.exists() and path.stat().st_size > 1_000_000:
        return path
    root.mkdir(parents=True, exist_ok=True)
    log.info("imgindex_model_download", url=settings.imgindex_model_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20), follow_redirects=True) as c:
        expected = ""
        r = await c.get(settings.imgindex_model_url + ".sha256")
        if r.status_code == 200:
            expected = r.text.split()[0]
        tmp = path.with_suffix(".part")
        async with c.stream("GET", settings.imgindex_model_url) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    f.write(chunk)
    if expected and await asyncio.to_thread(sha256, tmp) != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("model checksum mismatch")
    tmp.replace(path)
    return path


class ImageIndex:
    def __init__(self, settings: Settings, sources: SourceService, images: ImageProxy,
                 embedder: Embedder, store: Store) -> None:
        self.settings = settings
        self.sources = sources
        self.images = images
        self.embedder = embedder
        self.store = store
        self.wake = asyncio.Event()
        self.state = "idle"
        self.started = time.time()
        self.indexed_pages = 0
        self._next_fetch = 0.0
        self._crawl_cursor: dict[str, int] = {}

    # ------------------------------------------------------------ search
    async def search(self, data: bytes, limit: int = 10) -> list[dict[str, Any]]:
        im = await asyncio.to_thread(open_image, data)
        views = query_views(im)
        vecs = await asyncio.to_thread(self.embedder.embed, views)
        hits = await asyncio.to_thread(self.store.search, vecs, 40)
        top = hits[: limit * 3]
        meta = await asyncio.to_thread(self.store.page_meta, [p for p, _ in top])
        out: list[dict[str, Any]] = []
        seen_chapters: set[tuple[str, str]] = set()
        for pid, sim in top:
            m = meta.get(pid)
            if not m or (m["source_id"], m["chapter_id"]) in seen_chapters:
                continue
            seen_chapters.add((m["source_id"], m["chapter_id"]))
            out.append({**m, "similarity": round(sim * 100, 1)})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ queue
    def enqueue(self, source_id: str, manga_id: str, chapter_id: str, priority: int = READ_PRIORITY) -> None:
        info = self._info(source_id)
        if info is None or info.is_nsfw:
            return
        self.store.enqueue(source_id, manga_id, chapter_id, priority=priority)
        self.wake.set()

    def _info(self, source_id: str):
        try:
            return self.sources.adapter(source_id).info
        except Exception:  # noqa: BLE001 - unknown / disabled source
            return None

    # ------------------------------------------------------------ worker
    async def run(self) -> None:
        while True:
            try:
                job = self.store.next_chapter()
                if job is None and self.settings.imgindex_crawl and len(self.store) < self.settings.imgindex_max_vectors:
                    self.state = "planning"
                    if await self._plan_more():
                        continue
                if job is None:
                    self.state = "idle"
                    self.store.save()
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=600)
                    except TimeoutError:
                        pass
                    continue
                if job["priority"] < READ_PRIORITY and len(self.store) >= self.settings.imgindex_max_vectors:
                    await asyncio.sleep(60)
                    continue
                self.state = f"indexing {job['source_id']}"
                await self._index_chapter(job)
                self.store.save()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                log.warning("imgindex_loop_error", error=repr(exc))
                await asyncio.sleep(10)

    async def _title(self, source_id: str, manga_id: str) -> dict[str, Any]:
        data = await self.sources.title(source_id, manga_id)
        m = data["manga"]
        self.store.upsert_title(source_id, manga_id, m["title"], normalize_title(m["title"]), m.get("cover_url") or "")
        return data

    async def _index_chapter(self, job: dict[str, Any]) -> None:
        sid, cid, mid = job["source_id"], job["chapter_id"], job["manga_id"]
        crawl = job["priority"] < READ_PRIORITY
        try:
            if self.store.title(sid, mid) is None or not job["chapter_title"]:
                data = await self._title(sid, mid)
                ch = next((c for c in data["chapters"] if c["id"] == cid), None)
                if ch:
                    self.store.enqueue(sid, mid, cid, chapter_title=ch.get("title") or "", chapter_number=ch.get("number"))
            pages = await self.sources.pages(sid, cid)
        except Exception as exc:  # noqa: BLE001 - SourceError, AppError, network
            self.store.finish_chapter(sid, cid, error=repr(exc)[:300])
            return
        failed = 0
        for p in pages:
            if self.store.has_page(sid, cid, p["index"]):
                continue
            try:
                data = await self._fetch(sid, p["url"], polite=crawl)
                im = await asyncio.to_thread(open_image, data)
                vecs = await asyncio.to_thread(self.embedder.embed, index_views(im))
                await asyncio.to_thread(self.store.add_page, sid, mid, cid, p["index"], vecs)
                self.indexed_pages += 1
            except Exception as exc:  # noqa: BLE001 - one broken page doesn't fail the chapter
                failed += 1
                log.info("imgindex_page_failed", source=sid, error=repr(exc)[:200])
            if self.store.queued(READ_PRIORITY) and crawl:
                break  # someone is reading: their chapter goes first, this one resumes later
        else:
            self.store.finish_chapter(sid, cid, error=f"{failed} pages failed" if failed == len(pages) else None)

    async def _fetch(self, source_id: str, url: str, *, polite: bool) -> bytes:
        cached = self.images.cached(url)
        if cached is not None:
            return cached
        if polite:
            wait = self._next_fetch - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_fetch = time.monotonic() + 60 / max(1, self.settings.imgindex_pages_per_minute)
        data, _ = await self.images._fetch(source_id, url)
        return data

    # ------------------------------------------------------------ crawler
    def _crawl_sources(self) -> list[str]:
        return [i.id for i in self.sources.registry.infos() if not i.is_nsfw]

    async def _plan_more(self) -> bool:
        """Queue a spread of chapters for the next popular title not covered yet. False = nothing left."""
        limit = self.settings.imgindex_titles_per_source
        for sid in self._crawl_sources():
            pos = int(self.store.kv(f"crawl:{sid}") or 0)
            if pos >= limit:
                continue
            page = pos // 20 + 1
            try:
                res = await self.sources.browse(sid, parse_query(page=page))
            except Exception as exc:  # noqa: BLE001
                log.info("imgindex_browse_failed", source=sid, error=repr(exc)[:200])
                self.store.kv(f"crawl:{sid}", str(limit))  # skip this source until restart planning
                continue
            items = res["items"]
            if not items:
                self.store.kv(f"crawl:{sid}", str(limit))
                continue
            for card in items[pos % 20:]:
                pos += 1
                self.store.kv(f"crawl:{sid}", str(pos))
                norm = normalize_title(card["title"])
                self.store.upsert_title(sid, card["id"], card["title"], norm, card.get("cover_url") or "")
                if self.store.planned_elsewhere(sid, norm) or (self.store.title(sid, card["id"]) or {}).get("planned_at"):
                    continue
                if await self._plan_title(sid, card["id"]):
                    return True
            if not res.get("has_next"):
                self.store.kv(f"crawl:{sid}", str(limit))
        return False

    async def _plan_title(self, sid: str, mid: str) -> bool:
        try:
            data = await self._title(sid, mid)
        except Exception as exc:  # noqa: BLE001
            log.info("imgindex_title_failed", source=sid, error=repr(exc)[:200])
            return False
        chapters = sorted(data["chapters"], key=lambda c: (c.get("number") is None, c.get("number") or 0))
        self.store.mark_planned(sid, mid)
        if not chapters:
            return False
        n = self.settings.imgindex_chapters_per_title
        picks = chapters if len(chapters) <= n else [chapters[round(i * (len(chapters) - 1) / (n - 1))] for i in range(n)]
        for c in picks:
            self.store.enqueue(sid, mid, c["id"], chapter_title=c.get("title") or "", chapter_number=c.get("number"))
        return True

    def stats(self) -> dict[str, Any]:
        return {**self.store.stats(), "state": self.state, "indexed_since_start": self.indexed_pages,
                "max_vectors": self.settings.imgindex_max_vectors, "uptime_s": int(time.time() - self.started)}
