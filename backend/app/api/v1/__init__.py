from fastapi import APIRouter

from . import auth, catalog, health, library, reading, users

api_router = APIRouter(prefix="/api/v1")
for r in (
    auth.router,
    users.router,
    catalog.sources_router,
    catalog.manga_router,
    catalog.chapters_router,
    catalog.search_router,
    catalog.images_router,
    library.router,
    library.collections_router,
    reading.progress_router,
    reading.history_router,
    reading.downloads_router,
    reading.updates_router,
):
    api_router.include_router(r)

health_router = health.router
