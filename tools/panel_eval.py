"""Run the panel detector on real pages (MangaDex) and print coarse layout sketches. Dev only."""
import asyncio
import io
import random
import sys

import httpx
import numpy as np
from PIL import Image

sys.path.insert(0, "tools")
from panels import ascii_map, detect  # noqa: E402

API = "https://api.mangadex.org"


async def main() -> None:
    rnd = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
    stats = []
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": "MangaFlow-dev"}) as c:
        for lang in ("ru", "en"):
            r = await c.get(f"{API}/manga", params={"order[followedCount]": "desc", "limit": 30, "originalLanguage[]": "ja",
                                                   "availableTranslatedLanguage[]": lang, "contentRating[]": "safe",
                                                   "hasAvailableChapters": "true"})
            for m in r.json()["data"][:15]:
                feed = (await c.get(f"{API}/manga/{m['id']}/feed", params={"translatedLanguage[]": lang, "limit": 30,
                                                                           "order[chapter]": "asc"})).json()["data"]
                feed = [ch for ch in feed if ch["attributes"].get("pages", 0) >= 12 and not ch["attributes"].get("externalUrl")]
                if not feed:
                    continue
                ch = rnd.choice(feed)
                h = (await c.get(f"{API}/at-home/server/{ch['id']}")).json()
                files = h["chapter"]["data"][4:7]
                for i, f in enumerate(files):
                    pr = await c.get(f"{h['baseUrl']}/data/{h['chapter']['hash']}/{f}")
                    if pr.status_code != 200:
                        continue
                    im = Image.open(io.BytesIO(pr.content)).convert("L")
                    w = 600
                    g = np.asarray(im.resize((w, round(im.height * w / im.width))))
                    rects = detect(g)
                    name = f"{lang}:{(m['attributes']['title'].get('en') or '?')[:30]} p{i}"
                    stats.append(len(rects))
                    print(f"=== {name}  {im.size}  panels={len(rects)}")
                    print(ascii_map(g, rects, 56))
                await asyncio.sleep(0.4)
    print("panel counts:", sorted(stats))


asyncio.run(main())
