"""«Найти по скриншоту»: SauceNAO reverse image search → titles → our sources."""
from __future__ import annotations

import re
from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import AppError
from ..core.logging import get_logger

log = get_logger(__name__)

SAUCENAO = "https://saucenao.com/search.php"
# SauceNAO indexes that are manga/manhwa scans (MangaDex, Madokami, MangaUpdates…)
MANGA_INDEXES = {36, 37, 371, 18, 38}
MIN_SIMILARITY = 55.0
_CHAPTER = re.compile(r"(?:chapter|ch\.?|глава|chap)\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def chapter_from_part(part: str | None) -> float | None:
    if not part:
        return None
    m = _CHAPTER.search(part)
    if m:
        return float(m.group(1))
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", part)
    return float(m.group(1)) if m else None


def parse_saucenao(j: dict[str, Any]) -> list[dict[str, Any]]:
    """Best hit per title, manga indexes first."""
    out: dict[str, dict[str, Any]] = {}
    for r in j.get("results") or []:
        h, d = r.get("header") or {}, r.get("data") or {}
        try:
            sim = float(h.get("similarity") or 0)
        except ValueError:
            continue
        if sim < MIN_SIMILARITY:
            continue
        idx = int(h.get("index_id") or 0)
        title = d.get("source") or d.get("title") or d.get("eng_name") or d.get("jp_name") or ""
        if isinstance(title, list):
            title = title[0] if title else ""
        title = str(title).strip()
        if not title or title.startswith("http"):
            continue
        part = d.get("part") or ""
        hit = {
            "title": title,
            "part": str(part).strip(" -"),
            "chapter_number": chapter_from_part(str(part)),
            "similarity": sim,
            "thumbnail": h.get("thumbnail") or "",
            "index": h.get("index_name") or "",
            "url": (d.get("ext_urls") or [""])[0],
            "is_manga": idx in MANGA_INDEXES,
        }
        key = title.lower()
        old = out.get(key)
        if old is None or (hit["is_manga"], sim) > (old["is_manga"], old["similarity"]):
            out[key] = hit
    return sorted(out.values(), key=lambda x: (x["is_manga"], x["similarity"]), reverse=True)


class ImageLookup:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=30, transport=transport)

    async def aclose(self) -> None:
        await self.client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.settings.saucenao_api_key)

    async def find(self, image: bytes, content_type: str = "image/jpeg") -> list[dict[str, Any]]:
        if not self.configured:
            raise AppError("Поиск по картинке не настроен на сервере: добавьте SAUCENAO_API_KEY в .env "
                           "(бесплатный ключ на saucenao.com)", code="lookup_not_configured", status_code=503)
        try:
            r = await self.client.post(SAUCENAO, params={
                "output_type": 2, "numres": 8, "db": 999, "api_key": self.settings.saucenao_api_key,
            }, files={"file": ("image", image, content_type)})
        except httpx.HTTPError as exc:
            raise AppError(f"Сервис поиска по картинке недоступен: {exc}", code="lookup_unavailable",
                           status_code=502) from exc
        if r.status_code == 429:
            raise AppError("Лимит поиска по картинке исчерпан — попробуйте через минуту", code="lookup_rate_limited",
                           status_code=429)
        if r.status_code >= 400:
            raise AppError(f"Сервис поиска по картинке ответил {r.status_code}", code="lookup_failed", status_code=502)
        try:
            return parse_saucenao(r.json())
        except ValueError as exc:
            raise AppError("Сервис поиска по картинке вернул непонятный ответ", code="lookup_failed",
                           status_code=502) from exc
