"""MangaFlow's own reverse image search («как SauceNAO, только по нашим источникам»).

* Chapters people read are indexed first (the API enqueues them on reading progress).
* A polite crawler covers popular titles of every non-NSFW source in the background:
  a spread of chapters per title, deduplicated across sources by title.
* ``POST /search`` with an image body → the exact source, title, chapter and page.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from ..core.config import Settings
from ..core.logging import get_logger
from ..services.images import ImageProxy
from ..services.search import normalize_title
from ..services.sources import SourceService, parse_query
from .embedder import Embedder, index_views, open_image, query_views, sha256
from .store import Store
from .text import PlotIndex, TextEmbedder, passage, query

log = get_logger(__name__)

READ_PRIORITY = 10
BUSY_PAUSE = 60  # seconds the background crawl waits when the server is busy
MAX_QUERY_BYTES = 12 * 1024 * 1024


async def ensure_file(url: str, root: Path, min_size: int = 1000, name: str | None = None) -> Path:
    """Download a model file once into the index volume (checked against its .sha256)."""
    path = root / (name or Path(url).name)
    if path.exists() and path.stat().st_size >= min_size:
        return path
    root.mkdir(parents=True, exist_ok=True)
    log.info("imgindex_model_download", url=url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20), follow_redirects=True) as c:
        expected = ""
        r = await c.get(url + ".sha256")
        if r.status_code == 200:
            expected = r.text.split()[0]
        tmp = path.with_suffix(path.suffix + ".part")
        async with c.stream("GET", url) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    f.write(chunk)
    if expected and await asyncio.to_thread(sha256, tmp) != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch: {url}")
    tmp.replace(path)
    return path


async def ensure_model(settings: Settings, root: Path) -> Path:
    return await ensure_file(settings.imgindex_model_url, root, 1_000_000)


async def ensure_text_model(settings: Settings, root: Path) -> tuple[Path, Path]:
    model = await ensure_file(settings.imgindex_text_model_url, root, 1_000_000)
    # each model release carries its own tokenizer: keep them apart when the model is switched
    tok = await ensure_file(settings.imgindex_text_model_url.rsplit("/", 1)[0] + "/tokenizer.json", root,
                            name=f"{model.stem}.tokenizer.json")
    return model, tok


class ImageIndex:
    def __init__(self, settings: Settings, sources: SourceService, images: ImageProxy,
                 embedder: Embedder, store: Store, text: TextEmbedder | None = None) -> None:
        self.settings = settings
        self.text = text
        self.plots = PlotIndex(Path(settings.imgindex_dir), store.db, store.lock, text.dim) if text else None
        self._plot_next = 0.0
        self._plot_rr = 0
        self._plot_retry: dict[str, float] = {}
        self.sources = sources
        self.images = images
        self.embedder = embedder
        self.store = store
        self.wake = asyncio.Event()
        self.state = "idle"
        self.started = time.time()
        self.indexed_pages = 0
        self._next_fetch = 0.0
        self._retry_at: dict[str, float] = {}

    # ------------------------------------------------------------ load
    def busy(self) -> bool:
        """The server has better things to do than background crawling (load average per core)."""
        limit = self.settings.imgindex_busy_load
        if limit <= 0:
            return False
        try:
            load = os.getloadavg()[0]
        except OSError:
            return False
        cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
        return load / max(1, cores) > limit

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
                if (job is None or job["priority"] < READ_PRIORITY) and self.busy():
                    # crawling only: what people read now still goes through right away
                    job = self.store.next_chapter(READ_PRIORITY)
                    if job is None:
                        self.state = "paused (server busy)"
                        self.store.save()
                        try:
                            await asyncio.wait_for(self.wake.wait(), timeout=BUSY_PAUSE)
                        except TimeoutError:
                            pass
                        self.wake.clear()
                        continue
                if job is None and self.settings.imgindex_crawl and len(self.store) < self.settings.imgindex_max_vectors:
                    self.state = "planning"
                    if await self._plan_more():
                        continue
                if job is None:
                    self.state = "idle"
                    self.store.save()
                    if self.plots:
                        self.plots.save()
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
        norm = normalize_title(m["title"])
        self.store.upsert_title(source_id, manga_id, m["title"], norm, m.get("cover_url") or "")
        if self.plots is not None:
            self.plots.upsert(norm, source_id, manga_id, m["title"], list(m.get("alt_titles") or []),
                              list(m.get("genres") or []), m.get("description") or "")
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
        for pos, p in enumerate(pages):  # position in the list = what the reader opens (?page=)
            if self.store.has_page(sid, cid, pos):
                continue
            try:
                data = await self._fetch(sid, p["url"], polite=crawl)
                im = await asyncio.to_thread(open_image, data)
                vecs = await asyncio.to_thread(self.embedder.embed, index_views(im))
                await asyncio.to_thread(self.store.add_page, sid, mid, cid, pos, vecs)
                self.indexed_pages += 1
            except Exception as exc:  # noqa: BLE001 - one broken page doesn't fail the chapter
                failed += 1
                log.info("imgindex_page_failed", source=sid, error=repr(exc)[:200])
            if crawl and (self.store.queued(READ_PRIORITY) or self.busy()):
                break  # someone is reading or the server is busy: this chapter resumes later
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
        """Queue a spread of chapters for the next popular title not covered yet. False = nothing left.
        Cursor per source: catalogue page, offset in it, titles seen — page sizes differ by source."""
        limit = self.settings.imgindex_titles_per_source
        for sid in self._crawl_sources():
            page, offset, seen = (int(x) for x in (self.store.kv(f"crawl:{sid}") or "1:0:0").split(":"))
            if seen >= limit or self._retry_at.get(sid, 0) > time.monotonic():
                continue
            try:
                res = await self.sources.browse(sid, parse_query(page=page))
            except Exception as exc:  # noqa: BLE001 - source down: try it again in an hour
                log.info("imgindex_browse_failed", source=sid, error=repr(exc)[:200])
                self._retry_at[sid] = time.monotonic() + 3600
                continue
            items = res["items"]
            for card in items[offset:]:
                offset += 1
                seen += 1
                self.store.kv(f"crawl:{sid}", f"{page}:{offset}:{seen}")
                norm = normalize_title(card["title"])
                self.store.upsert_title(sid, card["id"], card["title"], norm, card.get("cover_url") or "")
                if self.store.planned_elsewhere(sid, norm) or (self.store.title(sid, card["id"]) or {}).get("planned_at"):
                    continue
                if await self._plan_title(sid, card["id"]):
                    return True
            nxt = f"{page + 1}:0:{seen}" if res.get("has_next") and items else f"{page}:{offset}:{limit}"
            self.store.kv(f"crawl:{sid}", nxt)
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

    # ------------------------------------------------------------ plots
    def _cards(self, norm: str) -> list[dict[str, Any]]:
        rows = self.store.db.execute("SELECT source_id, manga_id, title, cover_url FROM titles WHERE norm=?", (norm,)).fetchall()
        return [{"source_id": a, "manga_id": b, "title": c, "cover_url": d} for a, b, c, d in rows]

    def _with_sources(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for h in hits:
            cards = self._cards(h["norm"])
            # the source the description came from first
            cards.sort(key=lambda c: (c["source_id"], c["manga_id"]) != (h["source_id"], h["manga_id"]))
            h["sources"] = cards or [{"source_id": h["source_id"], "manga_id": h["manga_id"], "title": h["title"],
                                      "cover_url": ""}]
        return hits

    async def plot_search(self, text: str, limit: int = 20, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        if self.plots is None or self.text is None:
            return []
        vec = (await asyncio.to_thread(self.text.embed, [query(text)]))[0]
        return self._with_sources(await asyncio.to_thread(self.plots.search, vec, limit, exclude, text))

    async def _seed_vector(self, seed: dict[str, Any], fetch: bool) -> tuple[str, np.ndarray | None]:
        assert self.plots is not None and self.text is not None
        row = self.store.db.execute("SELECT norm FROM titles WHERE source_id=? AND manga_id=?",
                                    (seed["source_id"], seed["manga_id"])).fetchone()
        norm = row[0] if row else normalize_title(seed.get("title") or "")
        vec = self.plots.vector(norm) if norm else None
        if vec is None and fetch and self._info(seed["source_id"]) is not None:
            try:
                data = await asyncio.wait_for(self._title(seed["source_id"], seed["manga_id"]), 12)
                m = data["manga"]
                norm = normalize_title(m["title"])
                text = passage(m["title"], list(m.get("alt_titles") or []), list(m.get("genres") or []),
                               m.get("description") or "")
                vec = (await asyncio.to_thread(self.text.embed, [text]))[0]
            except Exception as exc:  # noqa: BLE001 - a seed we can't read is skipped
                log.info("imgindex_seed_failed", source=seed["source_id"], error=repr(exc)[:200])
        return norm, vec

    async def plot_similar(self, seeds: list[dict[str, Any]], limit: int = 20,
                           exclude: set[str] | None = None) -> dict[str, Any]:
        """Titles whose plot is close to the seeds' (weighted mean of their vectors)."""
        if self.plots is None or self.text is None or not seeds:
            return {"hits": [], "seeds": []}
        seeds = seeds[:40]
        got = []
        fetched = 0
        for s in seeds:
            norm, vec = await self._seed_vector(s, fetch=fetched < 4)
            if vec is None and norm:
                fetched += 1
            got.append((s, norm, vec))
        used = [(s, n, v) for s, n, v in got if v is not None]
        skip = set(exclude or ()) | {n for _, n, _ in got if n}
        if not used:
            return {"hits": [], "seeds": []}
        profile = np.sum([float(s.get("weight") or 1.0) * v for s, _, v in used], axis=0)
        profile /= max(float(np.linalg.norm(profile)), 1e-8)
        hits = await asyncio.to_thread(self.plots.search, profile, limit, skip)
        return {"hits": self._with_sources(hits), "seeds": [n for _, n, _ in used]}

    async def embed_pending_plots(self, batch: int = 32) -> int:
        assert self.plots is not None and self.text is not None
        pending = self.plots.pending(batch)
        if pending:
            vecs = await asyncio.to_thread(self.text.embed, [t for _, t in pending])
            await asyncio.to_thread(self.plots.set_vectors, [i for i, _ in pending], vecs)
        return len(pending)

    async def run_plots(self) -> None:
        """Embed descriptions as they arrive; between them crawl catalogues for more."""
        assert self.plots is not None and self.text is not None
        while True:
            try:
                if await self.embed_pending_plots():
                    continue
                self.plots.save()
                if self.busy():
                    await asyncio.sleep(BUSY_PAUSE)
                    continue
                if not self.settings.imgindex_plot or not await self._plot_step():
                    await asyncio.sleep(600)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                log.warning("imgindex_plot_error", error=repr(exc))
                await asyncio.sleep(30)

    async def _plot_step(self) -> bool:
        """Fetch the description of one more popular title (round-robin over sources)."""
        assert self.plots is not None
        limit = self.settings.imgindex_plot_titles_per_source
        sids = self._crawl_sources()
        for _ in range(len(sids)):
            sid = sids[self._plot_rr % len(sids)]
            self._plot_rr += 1
            if self._plot_retry.get(sid, 0) > time.monotonic():
                continue
            page, offset, seen = (int(x) for x in (self.store.kv(f"plot:{sid}") or "1:0:0").split(":"))
            if seen >= limit:
                continue
            try:
                res = await self.sources.browse(sid, parse_query(page=page))
            except Exception as exc:  # noqa: BLE001 - source down: again in an hour
                log.info("imgindex_plot_browse_failed", source=sid, error=repr(exc)[:200])
                self._plot_retry[sid] = time.monotonic() + 3600
                continue
            items = res["items"]
            for card in items[offset:]:
                offset += 1
                seen += 1
                self.store.kv(f"plot:{sid}", f"{page}:{offset}:{seen}")
                norm = normalize_title(card["title"])
                self.store.upsert_title(sid, card["id"], card["title"], norm, card.get("cover_url") or "")
                if self.plots.has_description(norm):
                    continue
                wait = self._plot_next - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._plot_next = time.monotonic() + 60 / max(1, self.settings.imgindex_plot_titles_per_minute)
                try:
                    await self._title(sid, card["id"])
                except Exception as exc:  # noqa: BLE001
                    log.info("imgindex_plot_title_failed", source=sid, error=repr(exc)[:200])
                return True
            nxt = f"{page + 1}:0:{seen}" if res.get("has_next") and items else f"{page}:{offset}:{limit}"
            self.store.kv(f"plot:{sid}", nxt)
            return True
        return False

    def stats(self) -> dict[str, Any]:
        return {**self.store.stats(), **(self.plots.count() if self.plots else {}), "state": self.state, "indexed_since_start": self.indexed_pages,
                "max_vectors": self.settings.imgindex_max_vectors, "uptime_s": int(time.time() - self.started)}
