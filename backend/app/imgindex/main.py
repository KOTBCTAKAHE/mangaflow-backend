"""`imgindex` service: uvicorn app.imgindex.main:app --port 8100 (one process: it owns the index)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from mangaflow_sources import SourceRegistry

from ..core.cache import Cache
from ..core.config import Settings, get_settings
from ..core.logging import configure_logging, get_logger
from ..main import build_cache, source_configs
from ..services.images import ImageProxy
from ..services.sources import SourceService
from .embedder import Embedder, OnnxEmbedder
from .service import MAX_QUERY_BYTES, ImageIndex, ensure_model, ensure_text_model
from .store import Store
from .text import OnnxTextEmbedder, TextEmbedder

log = get_logger(__name__)


class SeedIn(BaseModel):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    title: str = Field(default="", max_length=512)
    weight: float = Field(default=1.0, ge=-5, le=10)


class PlotSearchIn(BaseModel):
    q: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=20, ge=1, le=60)
    exclude: list[str] = Field(default_factory=list, max_length=500)


class SimilarIn(BaseModel):
    seeds: list[SeedIn] = Field(min_length=1, max_length=40)
    limit: int = Field(default=20, ge=1, le=60)
    exclude: list[str] = Field(default_factory=list, max_length=500)


class EnqueueIn(BaseModel):
    source_id: str = Field(max_length=64)
    manga_id: str = Field(max_length=512)
    chapter_id: str = Field(max_length=1024)


def create_index_app(settings: Settings | None = None, *, registry: SourceRegistry | None = None,
                     cache: Cache | None = None, embedder: Embedder | None = None,
                     text_embedder: TextEmbedder | None = None,
                     image_transport=None, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        root = Path(settings.imgindex_dir)
        emb = embedder or OnnxEmbedder(await ensure_model(settings, root), settings.imgindex_threads)
        store = await asyncio.to_thread(Store, root, emb.dim)
        reg = registry or SourceRegistry.create(source_configs(settings), disabled=settings.sources_disabled)
        sources = SourceService(reg, cache or build_cache(settings), settings)
        images = ImageProxy(reg, settings, transport=image_transport)
        text = text_embedder
        if text is None and settings.imgindex_plot:
            try:
                model, tok = await ensure_text_model(settings, root)
                text = OnnxTextEmbedder(model, tok, settings.imgindex_threads)
            except Exception as exc:  # noqa: BLE001 - image search still works without plot search
                log.warning("imgindex_text_model_unavailable", error=repr(exc))
        app.state.index = ImageIndex(settings, sources, images, emb, store, text)
        tasks = [asyncio.create_task(app.state.index.run())] if start_worker else []
        if start_worker and app.state.index.plots is not None:
            tasks.append(asyncio.create_task(app.state.index.run_plots()))
        log.info("imgindex_ready", vectors=len(store))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await images.aclose()
            if app.state.index.plots is not None:
                await asyncio.to_thread(app.state.index.plots.save, True)
            await asyncio.to_thread(store.close)

    app = FastAPI(title="MangaFlow image index", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.post("/search")
    async def search(request: Request, limit: int = 10) -> Any:
        body = await request.body()
        if not body or len(body) > MAX_QUERY_BYTES:
            raise HTTPException(400, "image body required (≤12 MB)")
        try:
            hits = await request.app.state.index.search(body, min(limit, 30))
        except OSError as exc:  # PIL: not an image
            raise HTTPException(400, f"not an image: {exc}") from None
        return {"hits": hits, "vectors": len(request.app.state.index.store)}

    @app.post("/plot/search")
    async def plot_search(body: PlotSearchIn, request: Request) -> Any:
        idx: ImageIndex = request.app.state.index
        if idx.plots is None:
            raise HTTPException(503, "plot search is off")
        return {"hits": await idx.plot_search(body.q, body.limit, set(body.exclude)), **idx.plots.count()}

    @app.post("/plot/similar")
    async def plot_similar(body: SimilarIn, request: Request) -> Any:
        idx: ImageIndex = request.app.state.index
        if idx.plots is None:
            raise HTTPException(503, "plot search is off")
        return await idx.plot_similar([s.model_dump() for s in body.seeds], body.limit, set(body.exclude))

    @app.post("/enqueue", status_code=202)
    async def enqueue(body: EnqueueIn, request: Request) -> Any:
        request.app.state.index.enqueue(body.source_id, body.manga_id, body.chapter_id)
        return {"ok": True}

    @app.get("/stats")
    async def stats(request: Request) -> Any:
        return request.app.state.index.stats()

    @app.get("/health")
    async def health() -> Any:
        return {"ok": True}

    return app


app = create_index_app()
