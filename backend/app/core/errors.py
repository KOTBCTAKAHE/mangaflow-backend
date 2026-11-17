"""Unified error model.

Every error response has the shape::

    {"error": {"code": "not_found", "message": "...", "details": {...}, "request_id": "..."}}
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mangaflow_sources import (
    SourceAuthRequiredError,
    SourceBlockedError,
    SourceError,
    SourceNotFoundError,
    SourceParseError,
    SourceRateLimitedError,
    UnknownSourceError,
)

from .logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


_SOURCE_STATUS: list[tuple[type[SourceError], int]] = [
    (SourceNotFoundError, 404),
    (SourceAuthRequiredError, 403),
    (SourceBlockedError, 502),
    (SourceRateLimitedError, 503),
    (SourceParseError, 502),
    (SourceError, 503),
]


def _body(request: Request, code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {},
                      "request_id": getattr(request.state, "request_id", None)}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = {"Retry-After": str(exc.details.get("retry_after"))} if exc.details.get("retry_after") else None
        return JSONResponse(_body(request, exc.code, exc.message, exc.details), exc.status_code, headers=headers)

    @app.exception_handler(SourceError)
    async def _source_error(request: Request, exc: SourceError) -> JSONResponse:
        status = next(s for cls, s in _SOURCE_STATUS if isinstance(exc, cls))
        log.warning("source_error", source=exc.source_id, code=exc.code, error=exc.message)
        return JSONResponse(_body(request, exc.code, exc.message, {"source_id": exc.source_id}), status)

    @app.exception_handler(UnknownSourceError)
    async def _unknown_source(request: Request, exc: UnknownSourceError) -> JSONResponse:
        return JSONResponse(_body(request, "unknown_source", f"Источник '{exc.source_id}' не найден"), 404)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse(_body(request, "validation_error", "Некорректные данные запроса", {"errors": errors}), 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized"}.get(exc.status_code, "http_error")
        return JSONResponse(_body(request, code, str(exc.detail)), exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return JSONResponse(_body(request, "internal_error", "Внутренняя ошибка сервера"), 500)
