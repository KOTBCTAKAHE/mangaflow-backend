from __future__ import annotations

import json

import httpx
import pytest

from mangaflow_sources import (
    MangaType,
    PublicationStatus,
    SearchQuery,
    SourceAuthRequiredError,
    SourceBlockedError,
    SourceConfig,
    SourceUnavailableError,
)
from mangaflow_sources.adapters.desu import DesuAdapter
from mangaflow_sources.adapters.mangalib import MangaLibAdapter
from mangaflow_sources.adapters.remanga import RemangaAdapter
from mangaflow_sources.adapters.senkuro import SenkuroAdapter


def J(data, status=200):
    return httpx.Response(status, json=data)


# ---------------------------------------------------------------- MangaLib
async def test_mangalib_flow(router):
    router.add("GET", "/api/manga?", J({
        "data": [{"slug_url": "206--one-piece", "rus_name": "Ван Пис", "cover": {"thumbnail": "https://cover/x.jpg"},
                  "type": {"label": "Манга"}}],
        "meta": {"has_next_page": True},
    }))
    router.add("GET", "/api/manga/206--one-piece/chapters", J({"data": [
        {"number": "1", "volume": "1", "name": "Ромэнс Дон", "branches": [{"branch_id": None, "created_at": "2020-01-01T00:00:00Z", "teams": [{"name": "Team"}]}]},
        {"number": "2", "volume": "1", "name": None, "branches": [{"branch_id": 7, "created_at": "2020-01-08T00:00:00Z"}]},
    ]}))
    router.add("GET", "/api/manga/206--one-piece/chapter?", J({"data": {"pages": [{"url": "//manga/op/1.jpg"}, {"url": "/manga/op/2.jpg"}]}}))
    router.add("GET", "/api/manga/206--one-piece?", J({"data": {
        "rus_name": "Ван Пис", "eng_name": "One Piece", "summary": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Пираты"}]}]},
        "genres": [{"name": "Приключения"}], "authors": [{"name": "Ода"}], "status": {"label": "Онгоинг"},
        "type": {"label": "Манга"}, "cover": {"default": "https://cover/big.jpg"}, "ageRestriction": {"label": "16+"},
    }}))
    router.add("GET", "/api/constants", J({"data": {"imageServers": [
        {"id": "main", "url": "https://img2.imglib.info", "site_ids": [1]},
        {"id": "compress", "url": "https://img3.mixlib.me/", "site_ids": [1]},
    ]}}))
    a = MangaLibAdapter(transport=router.transport)
    page = await a.search(SearchQuery(query="ван", types=[MangaType.MANHWA], genres_exclude=["84"]))
    url = str(router.calls[-1].url)
    assert "q=%D0%B2%D0%B0%D0%BD" in url and "types%5B%5D=5" in url and "genres_exclude%5B%5D=84" in url
    assert router.calls[-1].headers["Site-Id"] == "1"
    assert page.items[0].id == "206--one-piece" and page.has_next

    details, chapters = await a.title("206--one-piece")
    assert details.description == "Пираты"
    assert details.status == PublicationStatus.ONGOING
    assert details.age_rating == "16+"
    assert [c.number for c in chapters] == [2, 1]
    assert chapters[0].id == "206--one-piece|2|1|7"
    assert chapters[1].title == "Глава 1: Ромэнс Дон" and chapters[1].scanlator == "Team"

    pages = await a.pages(chapters[0].id)
    assert "branch_id=7" in str(router.calls[-2].url)
    assert [p.url for p in pages] == ["https://img3.mixlib.me/manga/op/1.jpg", "https://img3.mixlib.me/manga/op/2.jpg"]


async def test_mangalib_token_audience_mismatch_falls_back(router):
    def handler(req: httpx.Request) -> httpx.Response:
        if "Authorization" in req.headers:
            return httpx.Response(422, json={"message": "audience mismatch"})
        return httpx.Response(200, json={"data": [], "meta": {"has_next_page": False}})

    router.add("GET", "/api/manga", handler)
    a = MangaLibAdapter(SourceConfig(token="foreign"), transport=router.transport)
    page = await a.popular()
    assert page.items == [] and len(router.calls) == 2


async def test_mangalib_empty_pages_is_auth_required(router):
    router.add("GET", "/chapter?", J({"data": {"pages": []}}))
    a = MangaLibAdapter(transport=router.transport)
    with pytest.raises(SourceAuthRequiredError):
        await a.pages("x|1|1")


# ---------------------------------------------------------------- Remanga
async def test_remanga_skips_paid_chapters(router):
    router.add("GET", "/titles/solo/", J({"content": {
        "main_name": "Соло", "secondary_name": "Solo / Solo Lvl", "description": "<p>Текст</p>",
        "genres": [{"name": "Экшен"}], "status": {"name": "Закончен"}, "type": {"name": "Манхва"},
        "cover": {"mid": "/media/solo.jpg"}, "branches": [{"id": 11}],
    }}))
    router.add("GET", "/titles/chapters/?", J({"content": [
        {"id": 3, "chapter": "3", "tome": 1, "is_paid": True, "is_bought": False},
        {"id": 2, "chapter": "2", "tome": 1, "name": "Ворота", "upload_date": "2024-01-02T00:00:00"},
        {"id": 1, "chapter": "1", "tome": 0, "is_published": False},
    ], "next": None}))
    router.add("GET", "/titles/chapters/2/", J({"content": {"pages": [[{"link": "https://img/1.jpg"}, {"link": "https://img/2.jpg"}]]}}))
    a = RemangaAdapter(transport=router.transport)
    details, chapters = await a.title("solo")
    assert details.title == "Соло" and details.alt_titles == ["Solo", "Solo Lvl"]
    assert details.description == "Текст"
    assert details.type == MangaType.MANHWA and details.status == PublicationStatus.COMPLETED
    assert details.cover_url == "https://remanga.org/media/solo.jpg"
    assert [c.id for c in chapters] == ["2"]
    assert chapters[0].title == "Глава 2: Ворота" and chapters[0].volume == "1"
    pages = await a.pages("2")
    assert len(pages) == 2


# ---------------------------------------------------------------- Desu
async def test_desu(router):
    router.add("GET", "/manga/api/?", J({"response": [{"id": 5, "russian": "Наруто", "image": {"original": "https://d/5.jpg"}, "kind": "manga"}],
                                          "pageNavParams": {"page": 1, "limit": 20, "count": 50}}))
    router.add("GET", "/manga/api/5/chapter/9", J({"response": {"pages": {"list": [{"img": "https://d/p1.jpg"}]}}}))
    router.add("GET", "/manga/api/5", J({"response": {
        "russian": "Наруто", "name": "Naruto", "genres": [{"russian": "Сёнэн"}], "status": "released",
        "chapters": {"list": [{"id": 9, "ch": 700, "vol": 72, "title": "Финал", "date": 1415000000}]}}}))
    a = DesuAdapter(transport=router.transport)
    page = await a.popular()
    assert page.items[0].title == "Наруто" and page.has_next
    details, chapters = await a.title("5")
    assert details.status == PublicationStatus.COMPLETED and details.genres == ["Сёнэн"]
    assert chapters[0].id == "5/chapter/9" and chapters[0].uploaded_at.year == 2014
    assert (await a.pages(chapters[0].id))[0].url == "https://d/p1.jpg"


# ---------------------------------------------------------------- Senkuro
async def test_senkuro_graphql(router):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        op = body["operationName"]
        if op == "search":
            return J({"data": {"search": {"edges": [
                {"node": {"__typename": "SearchManga", "slug": "chainsaw", "titles": [{"lang": "RU", "content": "Человек-бензопила"}],
                          "cover": {"main": {"url": "https://s/c.webp"}}}},
                {"node": {"__typename": "SearchUser", "slug": "u"}},
            ]}}})
        if op == "fetchManga":
            return J({"data": {"manga": {
                "slug": "chainsaw", "status": "ONGOING", "type": "MANGA", "rating": "QUESTIONABLE",
                "titles": [{"lang": "EN", "content": "Chainsaw Man"}, {"lang": "RU", "content": "Человек-бензопила"}],
                "localizations": [{"lang": "RU", "description": [{"__typename": "TiptapNodeNestedBlock", "content": [{"__typename": "TiptapNodeText", "text": "Дэндзи"}]}]}],
                "labels": [{"titles": [{"lang": "RU", "content": "Экшен"}]}], "mainStaff": [{"person": {"name": "Фудзимото"}}],
                "branches": [{"id": "b1", "primaryBranch": True, "chapters": 2, "primaryTeamActivities": [{"team": {"name": "TT"}}]}],
            }}})
        if op == "fetchMangaChapters":
            return J({"data": {"mangaChapters": {"edges": [
                {"node": {"slug": "c2", "number": "2", "volume": "1", "createdAt": "2024-05-01T00:00:00Z"}},
                {"node": {"slug": "c1", "number": "1", "volume": "1", "name": "Пёс"}},
            ], "pageInfo": {"hasNextPage": False}}}})
        if op == "fetchMangaChapter":
            return J({"data": {"mangaChapter": {"pages": [
                {"number": 2, "image": {"compress": {"url": "https://s/2.webp"}}},
                {"number": 1, "image": {"compress": {"url": "https://s/1.webp"}, "original": {"url": "https://s/1.png"}}},
            ]}}})
        return J({"errors": [{"message": "unknown"}]})

    router.add("POST", "api.senkuro.com/graphql", handler)
    a = SenkuroAdapter(transport=router.transport)
    res = await a.search(SearchQuery(query="бензопила"))
    assert [m.id for m in res.items] == ["chainsaw"]
    details, chapters = await a.title("chainsaw")
    assert details.title == "Человек-бензопила" and details.age_rating == "16+"
    assert details.description == "Дэндзи" and details.authors == ["Фудзимото"]
    assert [c.title for c in chapters] == ["Глава 2", "Глава 1: Пёс"]
    assert chapters[0].scanlator == "TT"
    pages = await a.pages("c2")
    assert [p.url for p in pages] == ["https://s/1.webp", "https://s/2.webp"]


# ---------------------------------------------------------------- HTTP layer
async def test_retries_then_succeeds(router):
    attempts = {"n": 0}

    def flaky(req):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(503)
        return J({"response": [], "pageNavParams": {}})

    router.add("GET", "/manga/api/", flaky)
    a = DesuAdapter(transport=router.transport)
    await a.popular()
    assert attempts["n"] == 2


async def test_error_mapping(router):
    router.add("GET", "/manga/api/", httpx.Response(403))
    a = DesuAdapter(transport=router.transport)
    with pytest.raises(SourceBlockedError):
        await a.popular()

    def boom(req):
        raise httpx.ConnectError("down", request=req)

    router.routes.clear()
    router.add("GET", "/manga/api/", boom)
    b = DesuAdapter(SourceConfig(max_retries=1), transport=router.transport)
    with pytest.raises(SourceUnavailableError):
        await b.popular()
