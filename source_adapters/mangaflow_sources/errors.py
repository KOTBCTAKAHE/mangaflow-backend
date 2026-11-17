"""Exception hierarchy shared by all adapters.

The backend maps these to HTTP errors; adapters must never leak httpx or
parser exceptions.
"""
from __future__ import annotations


class SourceError(Exception):
    code = "source_error"

    def __init__(self, source_id: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"[{source_id}] {message}")
        self.source_id = source_id
        self.message = message
        self.status = status


class SourceUnavailableError(SourceError):
    """Network failure, timeout or 5xx after all retries."""

    code = "source_unavailable"


class SourceBlockedError(SourceError):
    """403/451 — usually geo-blocking or anti-bot protection. We do not bypass it."""

    code = "source_blocked"


class SourceAuthRequiredError(SourceError):
    """Content requires a logged-in account / token (18+, paid chapters)."""

    code = "source_auth_required"


class SourceNotFoundError(SourceError):
    code = "source_not_found"


class SourceRateLimitedError(SourceError):
    code = "source_rate_limited"


class SourceParseError(SourceError):
    """Site markup/API changed and the adapter could not understand it."""

    code = "source_parse_error"


class UnknownSourceError(KeyError):
    def __init__(self, source_id: str) -> None:
        super().__init__(source_id)
        self.source_id = source_id
