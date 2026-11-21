"""One title, every source: finds the same title on the other sources and merges their chapters.

A title stays anchored to the source the user opened it from (library, progress and history are
keyed by it). On top of that:

* ``links`` — the same title on the other sources of the same language, matched by any of its
  names (Russian, English, original) or a near-identical title. Cached for a day.
* ``merged`` — one chapter list: every chapter of the anchor source, plus the chapters only other
  sources have (by number). A chapter that exists on several sources carries the others as
  ``alts`` so the app can fall back when a site is down. Foreign chapters get ids of the form
  ``@<source>:<chapter id>`` — stable, so progress and downloads keep working.
"""
from __future__ import annotations

import asyncio
from typing import Any

from rapidfuzz import fuzz

from mangaflow_sources import SearchQuery

from ..core.logging import get_logger
from .search import normalize_title, same_work, title_names
from .sources import SourceService

log = get_logger(__name__)

LINK_TTL = 24 * 3600
MERGED_TTL = 15 * 60
LINK_FUZZ = 93
MAX_ALTS = 3


def foreign_id(source_id: str, chapter_id: str) -> str:
    return f"@{source_id}:{chapter_id}"


def real_chapter(source_id: str, chapter_id: str) -> tuple[str, str]:
    """``@other:abc`` read through the anchor title → (``other``, ``abc``)."""
    if chapter_id.startswith("@") and ":" in chapter_id:
        sid, cid = chapter_id[1:].split(":", 1)
        return sid, cid
    return source_id, chapter_id


def _numbers_ok(chapters: list[dict[str, Any]]) -> bool:
    """Merging by number only makes sense when numbers are unique and mostly present."""
    nums = [c.get("number") for c in chapters]
    present = [n for n in nums if n is not None]
    if not chapters or len(present) < 0.9 * len(chapters):
        return False
    return len(set(present)) >= 0.97 * len(present)


class WorkService:
    def __init__(self, sources: SourceService, timeout: float) -> None:
        self.sources = sources
        self.timeout = timeout

    # ------------------------------------------------------------------ links
    async def links(self, source_id: str, manga_id: str) -> list[dict[str, Any]]:
        key = f"links2:{source_id}:{manga_id}"  # v2: spin-offs are no longer linked
        cached = await self.sources.cache.get(key)
        if cached is not None:
            return cached
        data = await self.sources.title(source_id, manga_id)
        manga = data["manga"]
        names = title_names(manga)
        main = normalize_title(manga.get("title") or "")
        anchor = self.sources.adapter(source_id).info
        candidates = [
            i for i in self.sources.registry.infos()
            if i.id != source_id and i.lang == anchor.lang and (anchor.is_nsfw or not i.is_nsfw)
            and i.capabilities.search
        ]
        queries = [manga.get("title") or ""]
        # an English name finds the title on sites that list it under it
        queries += [a for a in manga.get("alt_titles") or [] if a and a.isascii()][:1]

        async def find(sid: str) -> dict[str, Any] | None:
            for q in queries:
                if not q.strip():
                    continue
                try:
                    res = await asyncio.wait_for(self.sources.browse(sid, SearchQuery(query=q)), self.timeout)
                except Exception:  # noqa: BLE001 — a dead site simply has no link
                    return None
                best, best_score = None, 0.0
                for card in res.get("items") or []:
                    if not same_work(manga, card, fuzzy=LINK_FUZZ):
                        continue  # sequels and side stories («…: История Химено») are other works
                    score = 101.0 if title_names(card) & names else fuzz.token_sort_ratio(main, normalize_title(card.get("title") or ""))
                    if score > best_score:
                        best, best_score = card, score
                if best is not None and best_score >= LINK_FUZZ:
                    return {"source_id": sid, "source_name": self.sources.adapter(sid).info.name,
                            "manga_id": best["id"], "title": best["title"]}
            return None

        found = await asyncio.gather(*(find(i.id) for i in candidates))
        links = [f for f in found if f]
        await self.sources.cache.set(key, links, LINK_TTL)
        return links

    # ------------------------------------------------------------------ merged chapters
    async def merged(self, source_id: str, manga_id: str, *, refresh: bool = False) -> dict[str, Any]:
        key = f"merged2:{source_id}:{manga_id}"
        if refresh:
            await self.sources.cache.delete(key)
        cached = await self.sources.cache.get(key)
        if cached is not None:
            return cached
        base = await self.sources.title(source_id, manga_id, refresh=refresh)
        anchor_name = base["manga"].get("source_name") or source_id
        chapters = [dict(c, source_id=source_id, source_name=anchor_name, alts=[]) for c in base["chapters"]]
        sources = [{"source_id": source_id, "source_name": anchor_name, "manga_id": manga_id,
                    "chapters_count": len(chapters), "latest": _latest(chapters), "added": 0}]
        result = {"manga": base["manga"], "chapters": chapters, "sources": sources}
        if not _numbers_ok(chapters) and chapters:
            await self.sources.cache.set(key, result, MERGED_TTL)
            return result

        links = await self.links(source_id, manga_id)

        async def load(link: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            try:
                d = await asyncio.wait_for(self.sources.title(link["source_id"], link["manga_id"]), self.timeout * 2)
                return link, d["chapters"]
            except Exception:  # noqa: BLE001
                return link, []

        others = [x for x in await asyncio.gather(*(load(link) for link in links)) if _numbers_ok(x[1])]
        # the fullest source first: its chapters fill the gaps, the rest become fallbacks
        others.sort(key=lambda x: len(x[1]), reverse=True)
        by_num: dict[float, dict[str, Any]] = {c["number"]: c for c in chapters if c.get("number") is not None}
        for link, chs in others:
            added = 0
            for c in chs:
                n = c.get("number")
                if n is None:
                    continue
                have = by_num.get(n)
                if have is None:
                    entry = dict(c, id=foreign_id(link["source_id"], c["id"]), source_id=link["source_id"],
                                 source_name=link["source_name"], alts=[])
                    by_num[n] = entry
                    chapters.append(entry)
                    added += 1
                elif len(have["alts"]) < MAX_ALTS:
                    have["alts"].append({"source_id": link["source_id"], "chapter_id": c["id"]})
            sources.append({"source_id": link["source_id"], "source_name": link["source_name"],
                            "manga_id": link["manga_id"], "chapters_count": len(chs), "latest": _latest(chs),
                            "added": added})
        numbered = sorted((c for c in chapters if c.get("number") is not None), key=lambda c: c["number"], reverse=True)
        result["chapters"] = numbered + [c for c in chapters if c.get("number") is None]
        await self.sources.cache.set(key, result, MERGED_TTL)
        return result


def _latest(chapters: list[dict[str, Any]]) -> float | None:
    nums = [c["number"] for c in chapters if c.get("number") is not None]
    return max(nums) if nums else None
