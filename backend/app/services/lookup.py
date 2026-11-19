"""«Найти по скриншоту»: SauceNAO reverse image search → titles → our sources."""
from __future__ import annotations

import asyncio
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
MIN_SIMILARITY = 60.0
# translated/cleaned scans differ from the indexed raws: manga hits count from lower similarity
MIN_MANGA_SIMILARITY = 42.0
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
        idx = int(h.get("index_id") or 0)
        if sim < (MIN_MANGA_SIMILARITY if idx in MANGA_INDEXES else MIN_SIMILARITY):
            continue
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
            "md_id": d.get("md_id") or None,
        }
        key = title.lower()
        old = out.get(key)
        if old is None or (hit["is_manga"], sim) > (old["is_manga"], old["similarity"]):
            out[key] = hit
    return sorted(out.values(), key=lambda x: (x["is_manga"], x["similarity"]), reverse=True)


TRACE_MOE = "https://api.trace.moe/search"
ANILIST = "https://graphql.anilist.co"
MANGADEX = "https://api.mangadex.org"
TRACE_MIN = 0.86
_MD_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def parse_trace(j: dict[str, Any]) -> list[dict[str, Any]]:
    """trace.moe hits (anime frames) → one entry per anime, best similarity first."""
    out: dict[int, dict[str, Any]] = {}
    for r in j.get("result") or []:
        sim = float(r.get("similarity") or 0)
        a = r.get("anilist") if isinstance(r.get("anilist"), dict) else {}
        aid = a.get("id")
        if sim < TRACE_MIN or not aid or aid in out:
            continue
        t = a.get("title") or {}
        names = [x for x in [t.get("english"), t.get("romaji"), t.get("native"), *(a.get("synonyms") or [])] if x]
        if not names:
            continue
        ep = r.get("episode")
        out[aid] = {
            "title": names[0], "names": names, "anilist_id": aid, "is_adult": bool(a.get("isAdult")),
            "part": f"аниме, серия {ep}" if ep else "аниме-адаптация",
            "chapter_number": None, "similarity": round(sim * 100, 1), "thumbnail": r.get("image") or "",
            "engine": "trace.moe", "is_manga": False, "url": "",
        }
    return sorted(out.values(), key=lambda x: x["similarity"], reverse=True)


class ImageLookup:
    """Several engines, each optional: SauceNAO (manga pages, needs a free key),
    trace.moe (anime frames — TikTok edits are often from the anime) → AniList
    finds the manga it adapts; MangaDex adds Russian alt titles for our sources."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=30, transport=transport, headers={"User-Agent": "MangaFlow"})

    async def aclose(self) -> None:
        await self.client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.settings.saucenao_api_key)

    async def _saucenao(self, image: bytes, content_type: str) -> list[dict[str, Any]]:
        r = await self.client.post(SAUCENAO, params={
            "output_type": 2, "numres": 16, "db": 999, "api_key": self.settings.saucenao_api_key,
        }, files={"file": ("image", image, content_type)})
        if r.status_code == 429:
            raise AppError("лимит запросов исчерпан, попробуйте через минуту", code="rate_limited")
        if r.status_code in (401, 403):
            raise AppError(f"ключ не принят или сервер заблокирован ({r.status_code})", code="denied")
        if r.status_code >= 400:
            raise AppError(f"ответил {r.status_code}", code="failed")
        hits = parse_saucenao(r.json())
        for h in hits:
            h["engine"] = "SauceNAO"
            h.setdefault("names", [h["title"]])
        return hits

    async def _trace(self, image: bytes, content_type: str) -> list[dict[str, Any]]:
        r = await self.client.post(TRACE_MOE, params={"anilistInfo": "", "cutBorders": ""}, content=image,
                                   headers={"Content-Type": content_type})
        if r.status_code == 402 or r.status_code == 429:
            raise AppError("месячный лимит trace.moe исчерпан", code="rate_limited")
        if r.status_code >= 400:
            raise AppError(f"ответил {r.status_code}", code="failed")
        return parse_trace(r.json())

    async def _manga_of_anime(self, anilist_id: int, depth: int = 0) -> list[str]:
        """Titles of the manga an anime adapts (Cyrillic synonyms first). Sequels
        often link only to the previous season, so PREQUEL/PARENT are followed."""
        q = """query ($id: Int) { Media(id: $id) { relations { edges { relationType node {
            id type format title { romaji english native } synonyms } } } } }"""
        r = await self.client.post(ANILIST, json={"query": q, "variables": {"id": anilist_id}})
        if r.status_code >= 400:
            return []
        edges = (((r.json().get("data") or {}).get("Media") or {}).get("relations") or {}).get("edges") or []
        rank = {"SOURCE": 0, "ADAPTATION": 1, "PARENT": 2, "ALTERNATIVE": 3}
        manga = sorted((e for e in edges if (e.get("node") or {}).get("type") == "MANGA"
                        and (e.get("node") or {}).get("format") != "NOVEL" and e.get("relationType") in rank),
                       key=lambda e: rank[e["relationType"]])
        if manga:
            n = manga[0]["node"]
            t = n.get("title") or {}
            syn = n.get("synonyms") or []
            ru = [x for x in syn if re.search("[а-яА-ЯёЁ]", x)]
            return list(dict.fromkeys(x for x in [*ru, t.get("english"), t.get("romaji"), *syn, t.get("native")] if x))
        if depth < 2:
            for e in edges:
                n = e.get("node") or {}
                if n.get("type") == "ANIME" and e.get("relationType") in ("PREQUEL", "PARENT") and n.get("id"):
                    return await self._manga_of_anime(int(n["id"]), depth + 1)
        return []

    async def _mangadex(self, hit: dict[str, Any]) -> None:
        """MangaDex hit → its manga id + alt titles (Russian ones search our RU sources)."""
        md = hit.get("md_id")
        try:
            if not md:
                ch = next((m.group(0) for u in [hit.get("url") or ""] if "/chapter/" in u
                           for m in [_MD_UUID.search(u)] if m), None)
                if ch:
                    r = await self.client.get(f"{MANGADEX}/chapter/{ch}")
                    if r.status_code == 200:
                        rel = next((x for x in r.json()["data"]["relationships"] if x["type"] == "manga"), None)
                        md = rel and rel["id"]
            if not md:
                return
            r = await self.client.get(f"{MANGADEX}/manga/{md}")
            if r.status_code != 200:
                return
            a = r.json()["data"]["attributes"]
            alts = [v for d in a.get("altTitles") or [] for k, v in d.items() if k == "ru"]
            others = [v for d in [a.get("title") or {}, *(a.get("altTitles") or [])] for k, v in d.items() if k in ("en", "ja-ro")]
            hit["md_id"] = md
            hit["names"] = list(dict.fromkeys([*alts, hit["title"], *others]))
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return

    async def find_all(self, image: bytes, content_type: str = "image/jpeg") -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Returns (matches, engine statuses). Never raises for a single engine's failure."""
        engines: list[dict[str, str]] = []

        async def run(name: str, coro) -> list[dict[str, Any]]:
            try:
                hits = await coro
                engines.append({"name": name, "status": "ok" if hits else "nothing", "detail": f"{len(hits)} совпадений"})
                return hits
            except AppError as exc:
                engines.append({"name": name, "status": "error", "detail": exc.message})
            except (httpx.HTTPError, ValueError) as exc:
                engines.append({"name": name, "status": "error", "detail": f"недоступен ({type(exc).__name__})"})
            return []

        tasks = []
        if self.configured:
            tasks.append(run("SauceNAO", self._saucenao(image, content_type)))
        else:
            engines.append({"name": "SauceNAO", "status": "no_key",
                            "detail": "не настроен: SAUCENAO_API_KEY в .env сервера (бесплатно на saucenao.com)"})
        tasks.append(run("trace.moe", self._trace(image, content_type)))
        results = await asyncio.gather(*tasks)
        matches = [h for hits in results for h in hits]

        # enrich: anime → its manga's titles; MangaDex → Russian alt titles
        async def enrich(h: dict[str, Any]) -> None:
            if h.get("engine") == "trace.moe" and h.get("anilist_id"):
                names = await self._manga_of_anime(int(h["anilist_id"]))
                if names:
                    h["names"] = list(dict.fromkeys([*names, *h["names"]]))
            elif "mangadex" in (h.get("index") or "").lower() or "mangadex.org" in (h.get("url") or ""):
                await self._mangadex(h)

        await asyncio.gather(*(enrich(h) for h in matches[:4]))
        # manga-index hits first, then by similarity
        matches.sort(key=lambda h: (h.get("is_manga", False), h["similarity"]), reverse=True)
        return matches, engines

    async def find(self, image: bytes, content_type: str = "image/jpeg") -> list[dict[str, Any]]:
        return (await self.find_all(image, content_type))[0]
