from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_

from mangaflow_sources import SourceRegistry

from ..core.config import get_settings
from ..core.logging import configure_logging, get_logger
from ..db.session import make_engine, make_sessionmaker
from ..main import build_cache, source_configs
from ..models import RefreshSession
from ..services.sources import SourceService
from ..services.updates import check_updates
from .celery_app import celery

log = get_logger(__name__)


async def _check(user_id: str | None) -> dict[str, int]:
    s = get_settings()
    engine = make_engine(s.database_url)
    cache = build_cache(s)
    registry = SourceRegistry.create(source_configs(s), disabled=s.sources_disabled)
    try:
        import uuid

        return await check_updates(make_sessionmaker(engine), SourceService(registry, cache, s),
                                   user_id=uuid.UUID(user_id) if user_id else None)
    finally:
        await registry.aclose()
        await cache.close()
        await engine.dispose()


@celery.task(name="mangaflow.check_updates")
def check_updates_task(user_id: str | None = None) -> dict[str, int]:
    configure_logging(get_settings().log_level, get_settings().log_json)
    return asyncio.run(_check(user_id))


async def _purge() -> int:
    s = get_settings()
    engine = make_engine(s.database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            cutoff = datetime.now(UTC) - timedelta(days=7)
            res = await session.execute(delete(RefreshSession).where(or_(
                RefreshSession.expires_at < datetime.now(UTC), RefreshSession.revoked_at < cutoff)))
            await session.commit()
            return res.rowcount or 0
    finally:
        await engine.dispose()


@celery.task(name="mangaflow.purge_sessions")
def purge_sessions_task() -> int:
    return asyncio.run(_purge())
