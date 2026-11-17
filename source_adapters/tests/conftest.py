from __future__ import annotations

from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Router:
    """Tiny httpx MockTransport router: (method, substring) -> response factory."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, object]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, contains: str, response: object) -> None:
        self.routes.append((method, contains, response))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, contains, resp in self.routes:
            if request.method == method and contains in url:
                if callable(resp):
                    return resp(request)
                return resp  # type: ignore[return-value]
        return httpx.Response(404, text="not routed: " + url)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def router() -> Router:
    return Router()
