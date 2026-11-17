"""Global search across all enabled sources.

Sources are queried in parallel with a per-source timeout; one slow or
blocked source never fails the whole search — it is reported in ``errors``.
Results are deduplicated by normalized title (fuzzy) and ranked by
similarity to the query plus the number of sources that have the title.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import replace
from typing import Any

from rapidfuzz import fuzz

from mangaflow_sources import SearchQuery, SourceError

from ..core.logging import get_logger
from .sources import SourceService

log = get_logger(__name__)

MERGE_THRESHOLD = 90

_BRACKETS = re.compile(r"[\(\[\{].*?[\)\]\}]")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title).lower().replace("ё", "е")
    t = _BRACKETS.sub(" ", t)
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split())


class SearchService:
    def __init__(self, sources: SourceService, timeout: float) -> None:
        self.sources = sources
        self.timeout = timeout

    async def search(self, query: SearchQuery, source_ids: list[str]) -> dict[str, Any]:
        async def one(sid: str) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
            adapter = self.sources.adapter(sid)
            caps = adapter.info.capabilities
            q = replace(
                query,
                genres_exclude=query.genres_exclude if caps.genre_exclude else [],
                genres_include=query.genres_include if caps.genre_filter else [],
                types=query.types if caps.type_filter else [],
                statuses=query.statuses if caps.status_filter else [],
            )
            try:
                res = await asyncio.wait_for(self.sources.browse(sid, q), self.timeout)
                return sid, res, None
            except TimeoutError:
                return sid, None, {"source_id": sid, "code": "timeout", "message": "Источник не ответил вовремя"}
            except SourceError as exc:
                return sid, None, {"source_id": sid, "code": exc.code, "message": exc.message}
            except Exception as exc:  # noqa: BLE001
                log.exception("search_source_failed", source=sid)
                return sid, None, {"source_id": sid, "code": "internal_error", "message": str(exc)}

        results = await asyncio.gather(*(one(sid) for sid in source_ids))
        errors = [e for _, _, e in results if e]
        has_next = any(r["has_next"] for _, r, _ in results if r)
        hits: list[tuple[str, dict[str, Any]]] = [
            (sid, card) for sid, r, _ in results if r for card in r["items"]
        ]
        items = self.merge(query.query, hits)
        return {"query": query.query, "page": query.page, "has_next": has_next, "items": items,
                "errors": errors, "sources_queried": source_ids}

    def merge(self, query: str, hits: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        nq = normalize_title(query)
        for sid, card in hits:
            norm = normalize_title(card["title"])
            if not norm:
                continue
            target = None
            for g in groups:
                if g["_norm"] == norm or fuzz.token_sort_ratio(g["_norm"], norm) >= MERGE_THRESHOLD:
                    if all(s["source_id"] != sid for s in g["sources"]):
                        target = g
                        break
            hit = {"source_id": sid, "source_name": self.sources.adapter(sid).info.name,
                   "id": card["id"], "title": card["title"], "cover_url": card["cover_url"]}
            if target is None:
                groups.append({"_norm": norm, "key": f"{sid}:{card['id']}", "title": card["title"],
                               "cover_url": card["cover_url"], "type": card.get("type"), "sources": [hit]})
            else:
                target["sources"].append(hit)
                if not target["cover_url"]:
                    target["cover_url"] = card["cover_url"]
                target["type"] = target["type"] or card.get("type")
        for g in groups:
            rel = max(fuzz.WRatio(nq, normalize_title(s["title"])) for s in g["sources"]) / 100 if nq else 0.5
            g["score"] = round(rel + 0.04 * (len(g["sources"]) - 1), 4)
        groups.sort(key=lambda g: g["score"], reverse=True)
        for g in groups:
            g.pop("_norm")
        return groups
