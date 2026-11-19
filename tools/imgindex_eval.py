"""Calibrate our image search on real pages (MangaDex, data-saver) — run in CI.

Index pages of a few chapters, then query with edited screenshots (positives) and
with pages that are NOT indexed (negatives), print similarity distributions.
"""
import asyncio
import io
import random
import sys
import tempfile
import time
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, "backend")
from app.imgindex.embedder import OnnxEmbedder, index_views, open_image, query_views  # noqa: E402
from app.imgindex.store import Store  # noqa: E402

API = "https://api.mangadex.org"
rnd = random.Random(7)


async def chapter_pages(c: httpx.AsyncClient, manga_id: str, n_ch: int) -> list[list[bytes]]:
    feed = (await c.get(f"{API}/manga/{manga_id}/feed", params={"translatedLanguage[]": "en", "limit": 40,
                                                                "order[chapter]": "asc"})).json()["data"]
    feed = [ch for ch in feed if ch["attributes"].get("pages", 0) >= 8 and not ch["attributes"].get("externalUrl")]
    out = []
    for ch in feed[:: max(1, len(feed) // n_ch)][:n_ch]:
        h = (await c.get(f"{API}/at-home/server/{ch['id']}")).json()
        base, hsh = h["baseUrl"], h["chapter"]["hash"]
        pages = []
        for f in h["chapter"]["dataSaver"][:14]:
            r = await c.get(f"{base}/data-saver/{hsh}/{f}")
            if r.status_code == 200:
                pages.append(r.content)
        out.append(pages)
        await asyncio.sleep(0.5)
    return out


def jpeg(im: Image.Image, q: int = 70) -> Image.Image:
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=q)
    return Image.open(io.BytesIO(b.getvalue())).convert("RGB")


def edits(im: Image.Image) -> dict[str, Image.Image]:
    w, h = im.size
    out = {"exact": jpeg(im, 85)}
    # a panel: 45–65% crop somewhere on the page
    cw, ch = int(w * rnd.uniform(0.45, 0.65)), int(h * rnd.uniform(0.3, 0.5))
    x, y = rnd.randint(0, w - cw), rnd.randint(0, h - ch)
    out["crop_panel"] = jpeg(im.crop((x, y, x + cw, y + ch)))
    # phone screenshot: status/nav bars, page scaled into the middle
    phone = Image.new("RGB", (1080, 2340), (18, 18, 18))
    s = 1080 / w
    phone.paste(im.resize((1080, int(h * s))), (0, max(0, (2340 - int(h * s)) // 2)))
    d = ImageDraw.Draw(phone)
    d.rectangle((0, 0, 1080, 90), fill=(30, 30, 30))
    d.rectangle((0, 2200, 1080, 2340), fill=(30, 30, 30))
    out["phone_screenshot"] = jpeg(phone.resize((540, 1170)))
    # TikTok: blurred copy as background, page smaller in the middle, caption over it
    bg = im.resize((720, 1280)).filter(ImageFilter.GaussianBlur(24))
    s = 620 / w
    fg = im.resize((620, int(h * s)))
    bg.paste(fg, (50, max(0, (1280 - fg.height) // 2)))
    d = ImageDraw.Draw(bg)
    d.rectangle((60, 1050, 660, 1130), fill=(255, 255, 255))
    d.text((80, 1070), "this manga is peak, name in comments", fill=(0, 0, 0))
    out["tiktok"] = jpeg(bg, 60)
    # caption / translation over the art, colour shift
    t = im.convert("RGB").copy()
    ImageDraw.Draw(t).rectangle((int(w * .1), int(h * .7), int(w * .9), int(h * .82)), fill=(250, 250, 250))
    out["caption_overlay"] = jpeg(t.point(lambda v: min(255, int(v * 1.1 + 8))))
    return out


async def main() -> None:
    root = Path(tempfile.mkdtemp())
    emb = OnnxEmbedder(Path(sys.argv[1]), 2)
    store = Store(root, emb.dim)
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": "MangaFlow-eval"}) as c:
        top = (await c.get(f"{API}/manga", params={"order[followedCount]": "desc", "limit": 8,
                                                     "availableTranslatedLanguage[]": "en",
                                                     "contentRating[]": "safe"})).json()["data"]
        indexed, held_out = [], []
        for m in top:
            name = (m["attributes"]["title"].get("en") or next(iter(m["attributes"]["title"].values())))
            try:
                chs = await chapter_pages(c, m["id"], 3)
            except Exception as e:  # noqa: BLE001
                print("skip", name, e)
                continue
            if len(chs) < 3:
                continue
            for ci, pages in enumerate(chs[:2]):
                for pi, data in enumerate(pages):
                    indexed.append((name, ci, pi, data))
            held_out += [(name, data) for data in chs[2]]
            print("got", name, [len(p) for p in chs])
    t = time.time()
    for i, (name, ci, pi, data) in enumerate(indexed):
        im = open_image(data)
        store.add_page("md", name, f"{name}#{ci}", pi, emb.embed(index_views(im)))
    print(f"indexed {len(indexed)} pages, {len(store)} vectors in {time.time() - t:.1f}s")
    pid = {(r[1], r[2], r[3]): r[0] for r in store.db.execute("SELECT id, manga_id, chapter_id, page_index FROM pages")}

    stats: dict[str, list[tuple[float, bool, bool]]] = {}
    for name, ci, pi, data in rnd.sample(indexed, min(60, len(indexed))):
        for kind, q in edits(open_image(data)).items():
            hits = store.search(emb.embed(query_views(q)), 40)
            want = pid[(name, f"{name}#{ci}", pi)]
            best_sim = hits[0][1] if hits else 0
            rank1 = bool(hits) and hits[0][0] == want
            meta = store.page_meta([hits[0][0]]) if hits else {}
            title_ok = bool(hits) and meta[hits[0][0]]["manga_id"] == name
            stats.setdefault(kind, []).append((best_sim, rank1, title_ok))
    for kind, v in stats.items():
        sims = np.array([x[0] for x in v])
        print(f"{kind:17s} page@1 {np.mean([x[1] for x in v]):.0%}  title@1 {np.mean([x[2] for x in v]):.0%}  "
              f"sim p10 {np.percentile(sims, 10):.3f} p50 {np.median(sims):.3f}")
    neg = []
    for name, data in held_out:
        im = open_image(data)
        for kind, q in [("page", im), *list(edits(im).items())[1:4]]:
            hits = store.search(emb.embed(query_views(q)), 40)
            neg.append(hits[0][1] if hits else 0)
    neg = np.array(neg)
    print(f"NEGATIVES (not indexed, {len(neg)}): p50 {np.median(neg):.3f} p90 {np.percentile(neg, 90):.3f} "
          f"p99 {np.percentile(neg, 99):.3f} max {neg.max():.3f}")
    # same-title-but-other-chapter is the hard negative; report separately is the same set here


asyncio.run(main())
