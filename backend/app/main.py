from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from mangaflow_sources import SourceConfig, SourceRegistry
from mangaflow_sources.registry import discover_adapter_classes

from .api.v1 import api_router, health_router
from .core.cache import Cache, MemoryCache, RedisCache
from .core.config import Settings, get_settings
from .core.errors import install_error_handlers
from .core.logging import configure_logging, get_logger
from .db.session import make_engine, make_sessionmaker
from .services.images import ImageProxy
from .services.lookup import ImageLookup
from .services.works import WorkService
from .services.plot import PlotClient
from .services.recs import CoReading
from .services.rooms import RoomHub
from .services.search import SearchService
from .services.sources import SourceService
from .services.trackers import TrackerService

log = get_logger(__name__)


def source_configs(settings: Settings, env: dict[str, str] | None = None) -> dict[str, SourceConfig]:
    """Read per-source overrides: ``SOURCE_<ID>_BASE_URL``, ``_TOKEN``, ``_COOKIE``, ``_EXTRA_<KEY>``."""
    env = dict(os.environ if env is None else env)
    configs: dict[str, SourceConfig] = {}
    for cls in discover_adapter_classes():
        sid = cls.info.id
        prefix = f"SOURCE_{sid.upper()}_"
        extra = {k[len(prefix) + 6:].lower(): v for k, v in env.items() if k.startswith(prefix + "EXTRA_")}
        configs[sid] = SourceConfig(
            base_url=env.get(prefix + "BASE_URL") or None,
            token=env.get(prefix + "TOKEN") or None,
            cookie=env.get(prefix + "COOKIE") or None,
            proxy=env.get(prefix + "PROXY") or settings.sources_proxy,
            timeout=settings.source_timeout,
            max_retries=settings.source_max_retries,
            rate_per_second=settings.source_rate_per_second,
            extra=extra,
        )
    return configs


def build_cache(settings: Settings) -> Cache:
    if settings.redis_url.startswith("memory://"):
        return MemoryCache()
    return RedisCache(settings.redis_url)


def create_app(
    settings: Settings | None = None,
    *,
    registry: SourceRegistry | None = None,
    cache: Cache | None = None,
    image_transport=None,
    external_transport=None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.cache = cache or build_cache(settings)
        reg = registry or SourceRegistry.create(source_configs(settings), disabled=settings.sources_disabled)
        app.state.sources = SourceService(reg, app.state.cache, settings)
        app.state.search = SearchService(app.state.sources, settings.source_search_timeout)
        app.state.works = WorkService(app.state.sources, settings.source_search_timeout)
        app.state.images = ImageProxy(reg, settings, transport=image_transport)
        app.state.trackers = TrackerService(settings, transport=external_transport)
        app.state.lookup = ImageLookup(settings, transport=external_transport)
        app.state.plots = PlotClient(settings, transport=external_transport)
        app.state.coreading = CoReading()
        app.state.rooms = RoomHub(app.state.cache,
                                  None if isinstance(app.state.cache, MemoryCache) else settings.redis_url)
        log.info("startup", sources=[i.id for i in reg.infos()], env=settings.environment)
        try:
            yield
        finally:
            await app.state.images.aclose()
            await app.state.trackers.aclose()
            await app.state.lookup.aclose()
            await app.state.plots.aclose()
            await app.state.rooms.aclose()
            await reg.aclose()
            await app.state.cache.close()
            await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description="Backend MangaFlow: источники манги, библиотека, прогресс, синхронизация.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    install_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        structlog.contextvars.bind_contextvars(request_id=rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-ID"] = rid
        if not request.url.path.startswith("/api/v1/images"):
            log.info("request", method=request.method, path=request.url.path, status=response.status_code,
                     ms=round((time.perf_counter() - started) * 1000, 1), request_id=rid)
        return response

    app.include_router(health_router)
    app.include_router(api_router)
    return app


app = create_app()
