"""Plot search, similar titles, «Для вас»."""
from __future__ import annotations

import tempfile
import uuid

import httpx

from mangaflow_sources import MangaDetails, PublicationStatus, SourceInfo, SourceRegistry, encode_ref

from app.core.cache import MemoryCache
from app.core.config import Settings
from app.imgindex.embedder import TinyEmbedder
from app.imgindex.main import create_index_app
from app.imgindex.text import TinyTextEmbedder, clean, passage
from app.services.plot import PlotClient
from app.services.recs import CoReading

from .conftest import H, FakeAdapter
from .test_imgindex import pages_transport

PLOTS = {
    "/spider": ("Паучок", "Девушка погибает и [b]переродилась[/b] пауком в подземелье, полном монстров."),
    "/slime": ("Слизь", "Офисный работник погиб и переродился слизью в другом мире монстров."),
    "/volley": ("Волейбол", "Невысокий школьник играет в волейбол за школьную команду."),
    "/chef": ("Повар", "Юный повар готовит еду и побеждает в кулинарных соревнованиях."),
}


class PlotAdapter(FakeAdapter):
    info = SourceInfo(id="plots", name="Plots", base_url="https://plots.example")
    titles = {k: (v[0], ["Фэнтези"]) for k, v in PLOTS.items()}

    async def details(self, manga_id: str) -> MangaDetails:
        title, desc = PLOTS[manga_id]
        return MangaDetails(id=manga_id, title=title, cover_url=f"https://img.plots/{manga_id[1:]}.jpg",
                            genres=["Фэнтези"], status=PublicationStatus.ONGOING, description=desc)


def test_text_helpers():
    assert clean("a [character=1]Б[/character] <i>в</i>\\n  г") == "a Б в \\n г"
    p = passage("Паучок", ["Kumo"], ["Фэнтези"], "Текст")
    assert p.startswith("passage: Паучок. Kumo. Жанры: Фэнтези. Текст")


async def test_plot_index_crawl_search_similar():
    settings = Settings(environment="test", redis_url="memory://", log_json=False, log_level="WARNING",
                        imgindex_dir=tempfile.mkdtemp(prefix="mf-plot-"), image_cache_dir=tempfile.mkdtemp(),
                        imgindex_plot_titles_per_minute=100000)
    reg = SourceRegistry.create(classes=[PlotAdapter])
    kw = dict(registry=reg, cache=MemoryCache(), embedder=TinyEmbedder(), text_embedder=TinyTextEmbedder(),
              image_transport=pages_transport(), start_worker=False)
    app = create_index_app(settings, **kw)
    async with app.router.lifespan_context(app):
        idx = app.state.index
        steps = 0
        while await idx._plot_step() and steps < 20:
            steps += 1
        assert await idx.embed_pending_plots() == 4
        assert idx.plots.count() == {"plots": 4, "plots_pending": 0}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://idx") as c:
            r = await c.post("/plot/search", json={"q": "девушка переродилась пауком в подземелье"})
            hits = r.json()["hits"]
            assert hits[0]["title"] == "Паучок" and hits[0]["sources"][0]["source_id"] == "plots"
            assert "[b]" not in hits[0]["description"] and r.json()["plots"] == 4
            r = await c.post("/plot/similar", json={"seeds": [{"source_id": "plots", "manga_id": encode_ref("/spider")}], "limit": 3})
            body = r.json()
            assert body["seeds"] == ["паучок"]
            assert [h["title"] for h in body["hits"]][0] == "Слизь"  # both reborn among monsters
            assert all(h["title"] != "Паучок" for h in body["hits"])
            # a seed the index hasn't seen yet is fetched and embedded on the fly
            idx.store.db.execute("DELETE FROM plots WHERE norm='повар'")
            r = await c.post("/plot/similar", json={"seeds": [{"source_id": "plots", "manga_id": encode_ref("/chef")}]})
            assert r.json()["seeds"] == ["повар"]
    app2 = create_index_app(settings, **kw)
    async with app2.router.lifespan_context(app2):
        assert app2.state.index.plots.count()["plots"] >= 3  # persisted


def test_coreading_similar_needs_two_readers_and_skips_me():
    co = CoReading()
    a, b, c, me = (uuid.uuid4() for _ in range(4))
    co.user_items = {a: {"x": 2, "y": 2}, b: {"x": 2, "y": 1, "z": 2}, c: {"x": 1, "z": 2}, me: {"x": 2, "w": 3}}
    co.item_users = {}
    for u, its in co.user_items.items():
        for n, w in its.items():
            co.item_users.setdefault(n, {})[u] = w
    got = co.similar({"x": 1.0}, me=me)
    assert {n for n, _, _ in got} == {"y", "z"}  # w: only "me" reads it with x
    assert all(s >= 2 for _, _, s in got)
    assert co.similar({"x": 1.0}, exclude={"y"}, me=me)[0][0] == "z"


def _plot_mock(hits_by_seed: dict[str, list[dict]]):
    def handler(req: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(req.content or b"{}")
        if req.url.path == "/plot/search":
            return httpx.Response(200, json={"plots": 1234, "hits": hits_by_seed["search"]})
        if req.url.path == "/plot/similar":
            key = "profile" if len(body["seeds"]) > 1 else body["seeds"][0]["manga_id"]
            hits = [h for h in hits_by_seed.get(key, []) if h["norm"] not in set(body.get("exclude") or [])]
            return httpx.Response(200, json={"hits": hits, "seeds": ["x"]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _hit(title: str, sim: float, sid: str = "fake", mid: str | None = None) -> dict:
    from app.services.search import normalize_title
    return {"norm": normalize_title(title), "title": title, "similarity": sim, "description": "о чём-то",
            "genres": ["Экшен"], "sources": [{"source_id": sid, "manga_id": mid or f"/{title}", "title": title,
                                                "cover_url": "https://img.fake/c.jpg"}]}


async def test_plot_search_endpoint(client, app):
    app.state.settings.imgindex_url = "http://imgindex"
    app.state.plots = PlotClient(app.state.settings, transport=_plot_mock({"search": [
        _hit("Берсерк", 88.1), _hit("Нет такого источника", 87.0, sid="ghost")]}))
    r = await client.get("/api/v1/search/plot", params={"q": "чёрный мечник мстит демонам"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["title"] for i in body["items"]] == ["Берсерк"]  # unknown source dropped
    it = body["items"][0]
    assert it["similarity"] == 88.1 and it["snippet"] == "о чём-то" and "/api/v1/images/" in it["cover_url"]
    assert it["sources"][0]["source_name"] and body["indexed"] == 1234
    app.state.settings.imgindex_url = ""
    r = await client.get("/api/v1/search/plot", params={"q": "чёрный мечник"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "plot_unavailable"


async def test_for_you_and_similar(client, auth, app):
    app.state.settings.imgindex_url = "http://imgindex"
    app.state.plots = PlotClient(app.state.settings, transport=_plot_mock({
        "profile": [_hit("Альфа", 90), _hit("Бета", 88), _hit("Гамма", 86), _hit("Дельта", 84), _hit("Шум", 60)],
        "/one-piece": [_hit("Альфа", 91), _hit("Эпсилон", 89), _hit("Зета", 88), _hit("Эта", 87)],
        "/berserk": [_hit("Тета", 80)],
    }))
    seeds = [{"source_id": "fake", "manga_id": "/one-piece", "title": "Ван-Пис", "weight": 2},
             {"source_id": "fake", "manga_id": "/berserk", "title": "Берсерк", "weight": 1}]
    r = await client.post("/api/v1/discover/for-you", json={"seeds": seeds, "known": ["Бета"]})
    assert r.status_code == 200, r.text
    sec = {s["key"]: s for s in r.json()["sections"]}
    assert [i["title"] for i in sec["for_you"]["items"]] == ["Альфа", "Гамма", "Дельта"]  # Бета known, Шум too far
    because = sec["because:fake:/one-piece"]
    assert because["title"] == "Потому что вы читали «Ван-Пис»"
    assert [i["title"] for i in because["items"]] == ["Эпсилон", "Зета", "Эта"]  # Альфа already shown
    assert "because:fake:/berserk" not in sec  # one title is not a row
    assert all(i["reason"] for s in sec.values() for i in s["items"])

    app.state.plots = PlotClient(app.state.settings, transport=_plot_mock({
        encode_ref("/one-piece"): [_hit("Альфа", 91), _hit("Эпсилон", 89)]}))
    r = await client.get(f"/api/v1/manga/fake/{encode_ref('/one-piece')}/similar", params={"title": "Ван-Пис"})
    assert r.status_code == 200, r.text
    assert [i["title"] for i in r.json()["items"]][:2] == ["Альфа", "Эпсилон"]
    app.state.settings.imgindex_url = ""


def test_lexical_stems_and_fusion():
    import sqlite3
    import threading

    from app.imgindex.lexical import Lexical, Tokenizer, fuse

    tok = Tokenizer()
    assert tok("Гиганты-людоеды за стенами, и это всё") == ["гигант", "людоед", "стен"]
    assert tok("Pirates reborn") == ["pirat", "reborn"]
    db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    lex = Lexical(db, threading.RLock())
    lex.add(1, "Токийский гуль", "Студент становится полугулем после встречи с гулем")
    lex.add(2, "Атака титанов", "Люди живут за стенами, снаружи титаны")
    lex.add(2, "Атака титанов", "Человечество за огромными стенами; снаружи титаны-людоеды")  # replaced, not duplicated
    assert len(lex) == 2
    assert [d for d, _ in lex.search("парень стал гулем")] == [1]
    assert lex.search("стена людоеды")[0][0] == 2
    assert lex.search("") == [] and lex.search("и это всё") == []
    assert [d for d, _ in fuse([1, 2, 3], [3, 4])][:2] == [3, 1]
