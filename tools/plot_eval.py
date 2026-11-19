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

SHIKI = "https://shikimori.one"
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
    ("a high school girl reborn as a spider in a dungeon", "kumo desu ga"),
    ("rubber boy wants to become king of the pirates", "one piece"),
    ("weakest hunter levels up alone", "ore dake level up"),
]


def strip(t: str) -> str:
    return re.sub(r"\[/?[^\]]+\]", "", t or "")


async def fetch() -> list[dict]:
    out = []
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "MangaFlow plot-eval"}) as c:
        ids = []
        for page in range(1, 7):
            r = await c.get(f"{SHIKI}/api/mangas", params={"order": "popularity", "limit": 50, "page": page})
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
                            "english": " ".join(m.get("english") or []),
                            "genres": [g.get("russian") or g.get("name") for g in m.get("genres") or []],
                            "description": strip(m.get("description") or "")})
            await asyncio.sleep(0.7)
    return out


async def main() -> None:
    docs = await fetch()
    docs = [d for d in docs if d["description"]]
    print("titles with descriptions:", len(docs))
    e = OnnxTextEmbedder(Path(sys.argv[1]), Path(sys.argv[2]), 2)
    t = time.time()
    D = e.embed([passage(d["russian"] or d["name"], [d["name"], d["english"]], d["genres"], d["description"]) for d in docs])
    print(f"embedded in {time.time() - t:.1f}s ({(time.time() - t) / len(docs) * 1000:.0f} ms/title)")
    ranks = []
    for q, want in QUERIES:
        idx = [i for i, d in enumerate(docs) if want in (d["name"] + " " + d["english"]).lower()]
        if not idx:
            print(f"  -- not in set: {want}")
            continue
        s = D @ e.embed([query(q)])[0]
        order = list(np.argsort(-s))
        rank = min(order.index(i) for i in idx) + 1
        ranks.append(rank)
        top = docs[order[0]]
        print(f"{'OK  ' if rank == 1 else f'#{rank:<3}'} {q[:60]:60s} → {(top['russian'] or top['name'])[:30]:30s} "
              f"{s[order[0]]:.3f} (right one {max(s[i] for i in idx):.3f})")
    r = np.array(ranks)
    print(f"recall@1 {np.mean(r == 1):.0%}  @5 {np.mean(r <= 5):.0%}  @10 {np.mean(r <= 10):.0%}  (n={len(r)})")
    # similar titles, by eye
    for name in ("one piece", "naruto", "kumo desu ga", "berserk"):
        i = next((k for k, d in enumerate(docs) if name in d["name"].lower()), None)
        if i is None:
            continue
        s = D @ D[i]
        print("similar to", docs[i]["russian"] or docs[i]["name"], "→",
              ", ".join((docs[j]["russian"] or docs[j]["name"])[:25] for j in np.argsort(-s)[1:7]))


asyncio.run(main())
