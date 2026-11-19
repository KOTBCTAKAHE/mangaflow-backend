"""Live smoke test against the real sites: list -> cover -> title -> pages -> first page image.

    python source_adapters/tools/live_check.py [source_id ...]

Prints one block per source; with -v dumps raw snippets for failures. Not run in normal CI.
"""
from __future__ import annotations

import asyncio
import sys
import time
import traceback

import httpx

from mangaflow_sources.http import DEFAULT_UA
from mangaflow_sources.registry import BUILTIN_ADAPTERS

VERBOSE = "-v" in sys.argv
IDS = [a for a in sys.argv[1:] if not a.startswith("-")]


async def fetch_image(client: httpx.AsyncClient, adapter, url: str) -> str:
    if not url:
        return "EMPTY-URL"
    try:
        r = await client.get(url, headers=adapter.image_headers(url))
        ct = r.headers.get("content-type", "")
        head = r.content[:12]
        ok = r.status_code == 200 and (ct.startswith("image/") or head[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF", b"GIF"))
        return f"{'OK' if ok else 'BAD'} {r.status_code} {ct} {len(r.content)}B" + ("" if ok else f" body={r.text[:120]!r}")
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {e}"


async def check(cls, client: httpx.AsyncClient) -> None:
    a = cls()
    out = [f"=== {cls.info.id} ({a.base_url})"]
    t0 = time.monotonic()
    try:
        page = await a.popular(1)
        out.append(f"  popular: {len(page.items)} items, has_next={page.has_next}")
        if not page.items:
            latest = await a.latest(1)
            out.append(f"  latest: {len(latest.items)} items")
            page = latest
        if not page.items:
            raise RuntimeError("no items")
        m = page.items[0]
        out.append(f"  first: id={m.id!r} title={m.title!r}")
        out.append(f"  list cover: {m.cover_url[:140]} -> {await fetch_image(client, a, m.cover_url)}")
        d, chs = await a.title(m.id)
        out.append(f"  details: title={d.title!r} genres={d.genres[:4]} status={d.status.value} chapters={len(chs)}")
        out.append(f"  detail cover: {d.cover_url[:140]} -> {await fetch_image(client, a, d.cover_url)}")
        if chs:
            ch = chs[-1]
            out.append(f"  chapter: id={ch.id!r} title={ch.title!r}")
            pages = await a.pages(ch.id)
            out.append(f"  pages: {len(pages)}")
            if pages:
                out.append(f"  page0: {pages[0].url[:140]} -> {await fetch_image(client, a, pages[0].url)}")
    except Exception as e:  # noqa: BLE001
        out.append(f"  FAIL {type(e).__name__}: {str(e)[:400]}")
        if VERBOSE:
            out.append(traceback.format_exc()[-1500:])
    finally:
        await a.aclose()
    out.append(f"  ({time.monotonic() - t0:.1f}s)")
    print("\n".join(out), flush=True)


async def main() -> None:
    classes = [c for c in BUILTIN_ADAPTERS if not IDS or c.info.id in IDS]
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}) as client:
        for cls in classes:
            await check(cls, client)


if __name__ == "__main__":
    asyncio.run(main())
