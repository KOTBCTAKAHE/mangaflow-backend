from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with request.app.state.sessionmaker() as s:
            await s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc.__class__.__name__}"
    checks["cache"] = "ok" if await request.app.state.cache.ping() else "error"
    checks["sources"] = str(len(request.app.state.sources.registry.all()))
    healthy = checks["database"] == "ok" and checks["cache"] == "ok"
    return JSONResponse({"status": "ok" if healthy else "degraded", "checks": checks}, 200 if healthy else 503)
