"""Download sample manga pages (MangaDex data-saver) for panel-detection tuning. Dev only."""
import asyncio
import random
from pathlib import Path

import httpx

API = "https://api.mangadex.org"
out = Path("pages")
out.mkdir(exist_ok=True)


async def main() -> None:
    rnd = random.Random(3)
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": "MangaFlow-dev"}) as c:
        got = 0
        for lang in ("ru", "en"):
            r = await c.get(f"{API}/manga", params={"order[followedCount]": "desc", "limit": 25, "originalLanguage[]": "ja",
                                                   "availableTranslatedLanguage[]": lang, "contentRating[]": "safe",
                                                   "hasAvailableChapters": "true"})
            for m in r.json()["data"]:
                if got >= 90:
                    break
                feed = (await c.get(f"{API}/manga/{m['id']}/feed", params={"translatedLanguage[]": lang, "limit": 30,
                                                                           "order[chapter]": "asc"})).json()["data"]
                feed = [ch for ch in feed if ch["attributes"].get("pages", 0) >= 12 and not ch["attributes"].get("externalUrl")]
                if not feed:
                    continue
                ch = rnd.choice(feed)
                h = (await c.get(f"{API}/at-home/server/{ch['id']}")).json()
                files = h["chapter"]["dataSaver"][3:7]
                for i, f in enumerate(files):
                    pr = await c.get(f"{h['baseUrl']}/data-saver/{h['chapter']['hash']}/{f}")
                    if pr.status_code == 200:
                        (out / f"{lang}_{m['id'][:8]}_{i}.jpg").write_bytes(pr.content)
                        got += 1
                print(lang, m["attributes"]["title"].get("en"), got, flush=True)
                await asyncio.sleep(0.5)


asyncio.run(main())
