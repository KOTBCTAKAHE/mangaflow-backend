"""Why don't covers show? Checks the whole image path, inside the api container:

    docker compose exec api python /srv/source_adapters/tools/image_check.py astramanga tomilolib

1. the API's own catalog response (which cover URL the app receives),
2. that URL fetched back from the API (signed image proxy, ?w=320 like the app),
3. the image proxy called in-process (shows the real exception).
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
from urllib.parse import urlsplit

import httpx

IDS = [a for a in sys.argv[1:] if not a.startswith("-")] or ["astramanga", "tomilolib", "mangalib"]
API = os.environ.get("CHECK_API", "http://localhost:8000")


async def via_api(client: httpx.AsyncClient, sid: str) -> None:
    try:
        r = await client.get(f"{API}/api/v1/manga/{sid}", params={"page": 1})
        print(f"  catalog: HTTP {r.status_code}")
        items = r.json().get("items") or []
        if not items:
            print("  catalog: no items", r.text[:300])
            return
        cover = items[0]["cover_url"]
        print(f"  cover_url sent to the app: {cover[:110]}…")
        parts = urlsplit(cover)
        local = f"{API}{parts.path}?w=320"
        img = await client.get(local)
        print(f"  proxy (local):  HTTP {img.status_code} {img.headers.get('content-type')} {len(img.content)}B"
              + ("" if img.status_code == 200 else f" body={img.text[:300]!r}"))
        if parts.scheme and parts.netloc:
            try:
                ext = await client.get(cover + "&w=320" if "?" in cover else cover + "?w=320")
                print(f"  public URL:     HTTP {ext.status_code} {ext.headers.get('content-type')} {len(ext.content)}B"
                      f" cache={ext.headers.get('x-cache-status')}")
            except Exception as e:  # noqa: BLE001
                print(f"  public URL:     FAIL {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"  api check FAIL {type(e).__name__}: {e}")


async def in_process(sid: str) -> None:
    try:
        from app.core.config import get_settings
        from app.services.images import ImageProxy, ImageSigner
        from mangaflow_sources.registry import SourceRegistry
    except Exception as e:  # noqa: BLE001
        print(f"  (in-process check unavailable: {e})")
        return
    settings = get_settings()
    reg = SourceRegistry.create(classes=None)
    proxy = ImageProxy(reg, settings)
    try:
        a = reg.get(sid)
        page = await a.popular(1)
        raw = page.items[0].cover_url
        token = ImageSigner("http://x", settings).url(sid, raw, cover=True).rsplit("/", 1)[1]
        print(f"  cache dir: {settings.image_cache_dir} writable={os.access(settings.image_cache_dir, os.W_OK)}")
        for w in (None, 320):
            img = await proxy.get(token, w)
            print(f"  in-process w={w}: OK {img.content_type} {img.path.stat().st_size}B")
    except Exception as e:  # noqa: BLE001
        print(f"  in-process FAIL {type(e).__name__}: {e}")
        print("   ", traceback.format_exc().strip().splitlines()[-3:])
    finally:
        await proxy.aclose()


async def main() -> None:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for sid in IDS:
            print(f"=== {sid}")
            if "--no-api" not in sys.argv:
                await via_api(client, sid)
            await in_process(sid)


asyncio.run(main())
