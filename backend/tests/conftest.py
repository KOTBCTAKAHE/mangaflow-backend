from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from mangaflow_sources import (
    Chapter,
    Genre,
    MangaDetails,
    MangaListPage,
    MangaSummary,
    MangaType,
    PageImage,
    PublicationStatus,
    SearchQuery,
    SortOrder,
    SourceAdapter,
    SourceBlockedError,
    SourceCapabilities,
    SourceInfo,
    SourceRegistry,
)

from app.core.cache import MemoryCache
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.main import create_app

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://mangaflow:mangaflow@localhost:5432/mangaflow_test")


class FakeAdapter(SourceAdapter):
    """Deterministic in-memory source used as a test double for the adapter layer."""

    info = SourceInfo(
        id="fake", name="Fake Source", base_url="https://fake.example",
        capabilities=SourceCapabilities(search=True, genre_filter=True, genre_exclude=True, type_filter=True,
                                        sort_orders=(SortOrder.POPULAR, SortOrder.UPDATED, SortOrder.CREATED)),
        genres=(Genre("action", "Экшен"), Genre("drama", "Драма")),
    )
    titles = {
        "/one-piece": ("Ван-Пис", ["Экшен"]),
        "/berserk": ("Берсерк", ["Драма"]),
        "/solo": ("Поднятие уровня в одиночку", ["Экшен"]),
    }
    chapters_by_id: dict[str, list[Chapter]] = {}
    calls: list[str] = []

    async def popular(self, page: int = 1) -> MangaListPage:
        self.calls.append(f"popular:{page}")
        return MangaListPage([MangaSummary(k, v[0], f"https://img.fake/{k[1:]}.jpg", MangaType.MANGA)
                              for k, v in self.titles.items()], has_next=page < 2)

    async def latest(self, page: int = 1) -> MangaListPage:
        return MangaListPage([MangaSummary("/solo", "Поднятие уровня в одиночку", "")], has_next=False)

    async def search(self, query: SearchQuery) -> MangaListPage:
        self.calls.append(f"search:{query.query}:{','.join(query.genres_include)}")
        items = [MangaSummary(k, v[0], f"https://img.fake/{k[1:]}.jpg") for k, v in self.titles.items()
                 if query.query.lower() in v[0].lower().replace("-", " ") and (not query.genres_include or "action" in query.genres_include)]
        return MangaListPage(items, has_next=False)

    async def details(self, manga_id: str) -> MangaDetails:
        title, genres = self.titles[manga_id]
        return MangaDetails(id=manga_id, title=title, cover_url="https://img.fake/c.jpg", genres=genres,
                            status=PublicationStatus.ONGOING, description="desc")

    async def chapters(self, manga_id: str) -> list[Chapter]:
        return self.chapters_by_id.get(manga_id, [Chapter("/one-piece/c/2", "Глава 2", 2.0),
                                                  Chapter("/one-piece/c/1", "Глава 1", 1.0)])

    async def pages(self, chapter_id: str) -> list[PageImage]:
        return [PageImage(i, f"https://img.fake/{chapter_id.strip('/')}/{i}.jpg") for i in range(3)]


class BlockedAdapter(FakeAdapter):
    info = SourceInfo(id="blocked", name="Blocked", base_url="https://blocked.example")

    async def search(self, query: SearchQuery) -> MangaListPage:
        raise SourceBlockedError("blocked", "HTTP 403 (geo-block)")

    async def popular(self, page: int = 1) -> MangaListPage:
        raise SourceBlockedError("blocked", "HTTP 403 (geo-block)")


class OtherAdapter(FakeAdapter):
    info = SourceInfo(id="other", name="Other", base_url="https://other.example")
    titles = {"/van-pis": ("Ван Пис", ["Экшен"]), "/naruto": ("Наруто", [])}


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def _schema() -> AsyncIterator[None]:
    engine = create_async_engine(TEST_DB)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    yield


def image_transport() -> httpx.MockTransport:
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    def handler(req: httpx.Request) -> httpx.Response:
        if "missing" in str(req.url):
            return httpx.Response(404)
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    return httpx.MockTransport(handler)


@pytest.fixture
async def app(_schema, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    get_settings.cache_clear()
    settings = Settings(
        environment="test", database_url=TEST_DB, redis_url="memory://", log_json=False, log_level="WARNING",
        image_cache_dir=tempfile.mkdtemp(prefix="mf-img-"), rate_limit_default=1000, rate_limit_auth=1000,
        secret_key="test-secret-key-test-secret-key-0123456789",
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.security.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.ratelimit.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.auth.get_settings", lambda: settings)
    FakeAdapter.calls = []
    FakeAdapter.chapters_by_id = {}
    registry = SourceRegistry.create(classes=[FakeAdapter, BlockedAdapter, OtherAdapter])
    application = create_app(settings, registry=registry, cache=MemoryCache(), image_transport=image_transport())
    application.state.settings = settings
    async with application.router.lifespan_context(application):
        async with application.state.engine.begin() as conn:
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def register(client: httpx.AsyncClient, name: str = "reader") -> dict:
    r = await client.post("/api/v1/auth/register", json={
        "email": f"{name}@example.com", "username": name, "password": "secret123", "device_name": "pytest"})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def auth(client) -> dict:
    data = await register(client)
    return {"Authorization": f"Bearer {data['tokens']['access_token']}", "_data": data}


def H(auth: dict) -> dict:
    return {"Authorization": auth["Authorization"]}
