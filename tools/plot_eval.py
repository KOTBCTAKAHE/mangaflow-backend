"""Plot-search quality on real Russian descriptions (Shikimori top-300) — run in CI."""
import asyncio
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, "backend")
from app.imgindex.text import OnnxTextEmbedder, passage, query  # noqa: E402

SHIKI = "https://shikimori.io"
QUERIES = [
    ("старшеклассница переродилась пауком в подземелье", "kumo desu ga"),
    ("самый слабый охотник получил систему и качается в одиночку", "ore dake level up"),
    ("парень с резиновым телом пират ищет сокровище", "one piece"),
    ("ниндзя в котором запечатан лис мечтает стать хокаге", "naruto"),
    ("тетрадь в которую пишешь имя и человек умирает", "death note"),
    ("люди живут за огромными стенами, а снаружи гиганты-людоеды", "shingeki no kyojin"),
    ("низкорослый парень мечтает стать звездой волейбола", "haikyuu"),
    ("парень с бензопилой охотится на демонов, чтобы выплатить долги", "chainsaw man"),
    ("шпион создаёт фальшивую семью с девочкой-телепаткой и убийцей", "spy"),
    ("после смерти переродился слизью в другом мире", "slime"),
    ("братья алхимики хотят вернуть тела после неудачной трансмутации", "hagane no renkinjutsushi"),
    ("школьник съел палец проклятого духа и стал магом", "jujutsu kaisen"),
    ("сестра превратилась в демона, а брат стал истребителем демонов", "kimetsu no yaiba"),
    ("лысый герой побеждает любого с одного удара и скучает", "one punch"),
    ("студент стал полугулем после встречи с девушкой-гулем", "tokyo ghoul"),
    ("мечник с клеймом мстит демонам в мрачном средневековье", "berserk"),
    ("мальчик без суперсилы в мире, где у всех причуды", "boku no hero"),
    ("дети в приюте узнают, что их растят на корм демонам", "yakusoku no neverland"),
    ("парень попал в другой мир и после смерти возвращается назад во времени", "re:zero"),
    ("мальчик хочет стать охотником и найти своего отца", "hunter"),
    ("футбольный проект по воспитанию эгоистичного нападающего", "blue lock"),
    ("богиня и бестолковая команда в фэнтези мире, комедия", "kono subarashii"),
    ("старик-волшебник и эльфийка после победы над королём демонов, эльфийка живёт тысячу лет", "sousou no frieren"),
    ("девушка, которая не может говорить с людьми, хочет завести сто друзей", "komi-san"),
    ("мальчик без магии в мире магов хочет стать императором чародеев", "black clover"),
    ("репетитор и пять сестёр-близняшек, одна из них станет невестой", "toubun no hanayome"),
    ("класс должен убить учителя-осьминога до конца учебного года", "ansatsu kyoushitsu"),
    ("всё человечество окаменело, а школьник-учёный возрождает цивилизацию с нуля", "dr. stone"),
    ("парень без магии решает всё мускулами в школе магов", "mashle"),
    ("безработный неудачник умирает и перерождается младенцем в мире магии", "mushoku tensei"),
    ("два гения из школьного совета хотят, чтобы другой первым признался в любви", "kaguya-sama"),
    ("сын викинга хочет отомстить наёмнику, убившему его отца", "vinland saga"),
    ("приключенцы готовят еду из монстров прямо в подземелье", "dungeon meshi"),
    ("сирота в древнем Китае мечтает стать великим генералом", "kingdom"),
    ("первокурсник вступает в клуб дайвинга, где все только пьют", "grand blue"),
    ("хулиган начинает играть в баскетбол ради девушки", "slam dunk"),
    ("a high school girl reborn as a spider in a dungeon", "kumo desu ga"),
    ("rubber boy wants to become king of the pirates", "one piece"),
    ("weakest hunter levels up alone", "ore dake level up"),
]


def strip(t: str) -> str:
    return re.sub(r"\[/?[^\]]+\]", "", t or "")


async def fetch() -> list[dict]:
    out = []
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "MangaFlow plot-eval"}) as c:
        for dom in ("https://shikimori.one", "https://shikimori.me", "https://shikimori.io"):
            try:
                r = await c.get(f"{dom}/api/mangas", params={"limit": 1})
                print("domain", dom, r.status_code, r.headers.get("location"), r.headers.get("content-type"))
            except httpx.HTTPError as e:
                print("domain", dom, "error", type(e).__name__)
        ids = []
        for page in range(1, 13):
            r = await c.get(f"{SHIKI}/api/mangas", params={"order": "popularity", "limit": 50, "page": page})
            if r.status_code != 200:
                print("list failed", r.status_code, r.text[:200])
                break
            ids += [m["id"] for m in r.json()]
            await asyncio.sleep(0.8)
        for i in ids:
            for _ in range(3):
                r = await c.get(f"{SHIKI}/api/mangas/{i}")
                if r.status_code == 429:
                    await asyncio.sleep(3)
                    continue
                break
            if r.status_code == 200:
                m = r.json()
                out.append({"name": m.get("name") or "", "russian": m.get("russian") or "",
                            "english": " ".join(x for x in (m.get("english") or []) if x),
                            "genres": [g.get("russian") or g.get("name") for g in m.get("genres") or []],
                            "description": strip(m.get("description") or "")})
            await asyncio.sleep(0.7)
    return out


async def main() -> None:
    import sqlite3
    import tempfile
    import threading

    from app.imgindex.text import PlotIndex
    from app.services.search import normalize_title

    docs = [d for d in await fetch() if d["description"]]
    print("titles with descriptions:", len(docs))
    models = [a.split(",") for a in sys.argv[1:]]  # name,onnx,tokenizer
    for name, onnx, tok in models:
        e = OnnxTextEmbedder(Path(onnx), Path(tok), 2)
        root = Path(tempfile.mkdtemp())
        db = sqlite3.connect(root / "m.sqlite3", check_same_thread=False, isolation_level=None)
        idx = PlotIndex(root, db, threading.RLock(), e.dim)
        by_norm = {}
        for i, d in enumerate(docs):
            title = d["russian"] or d["name"]
            norm = normalize_title(title)
            by_norm[norm] = d
            idx.upsert(norm, "shiki", str(i), title, [d["name"], d["english"]], d["genres"], d["description"])
        t = time.time()
        while pend := idx.pending(64):
            idx.set_vectors([i for i, _ in pend], e.embed([x for _, x in pend]))
        print(f"== {name}: embedded {len(docs)} in {time.time() - t:.1f}s ({(time.time() - t) / len(docs) * 1000:.0f} ms/title)")
        for mode in ("dense", "hybrid"):
            ranks, misses = [], []
            qt = 0.0
            for q, want in QUERIES:
                if not any(want in (d["name"] + " " + d["english"]).lower() for d in docs):
                    continue
                t0 = time.time()
                v = e.embed([query(q)])[0]
                hits = idx.search(v, 50, None, q if mode == "hybrid" else None)
                qt += time.time() - t0
                rank = next((r + 1 for r, h in enumerate(hits)
                             if want in (by_norm[h["norm"]]["name"] + " " + by_norm[h["norm"]]["english"]).lower()), 99)
                ranks.append(rank)
                if rank > 1:
                    misses.append(f"#{rank} {q[:40]} → {hits[0]['title'][:28]}")
            r = np.array(ranks)
            print(f"  {mode:6s} recall@1 {np.mean(r == 1):.0%}  @3 {np.mean(r <= 3):.0%}  @5 {np.mean(r <= 5):.0%}  "
                  f"@10 {np.mean(r <= 10):.0%}  (n={len(r)}, {qt / len(r) * 1000:.0f} ms/query)")
            for m in misses:
                print("      ", m)


asyncio.run(main())
