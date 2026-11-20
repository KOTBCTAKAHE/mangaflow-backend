from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mangaflow_sources import PublicationStatus, SearchQuery, SourceAuthRequiredError
from mangaflow_sources.adapters.astramanga import AstraMangaAdapter
from mangaflow_sources.adapters.grouple_sites import AllHentaiAdapter, SeiMangaAdapter, UsagiAdapter
from mangaflow_sources.adapters.lib_sites import HentaiLibAdapter, YaoiLibAdapter
from mangaflow_sources.adapters.mangadex import MangaDexRuAdapter
from mangaflow_sources.adapters.mangapill import MangaPillAdapter
from mangaflow_sources.adapters.tomilolib import TomiloLibAdapter, resolve_image_url
from mangaflow_sources.models import MangaType, SortOrder
from mangaflow_sources.registry import BUILTIN_ADAPTERS


def H(text, status=200):
    return httpx.Response(status, text=text, headers={"content-type": "text/html; charset=utf-8"})


def J(obj, status=200):
    return httpx.Response(status, json=obj)


def q(router):
    return parse_qs(urlparse(str(router.calls[-1].url)).query)


def test_registry_ids_unique_and_nsfw_flagged():
    ids = [c.info.id for c in BUILTIN_ADAPTERS]
    assert len(ids) == len(set(ids)) and len(ids) >= 20
    for c in (YaoiLibAdapter, HentaiLibAdapter, AllHentaiAdapter):
        assert c.info.is_nsfw
    assert MangaPillAdapter.info.lang == "en" and MangaDexRuAdapter.info.lang == "ru"
    assert SeiMangaAdapter.info.base_url != UsagiAdapter.info.base_url


# --------------------------------------------------------------- LibGroup siblings
async def test_lib_siblings_send_their_site_id(router):
    router.add("GET", "/manga", J({"data": [{"slug_url": "1--x", "rus_name": "Икс", "cover": {"md": "https://c/x.jpg"}}],
                                   "meta": {"has_next_page": False}}))
    a = YaoiLibAdapter(transport=router.transport)
    page = await a.popular()
    assert page.items[0].title == "Икс"
    req = router.calls[-1]
    assert req.headers["Site-Id"] == "2"
    assert parse_qs(urlparse(str(req.url)).query)["site_id[]"] == ["2"]
    assert req.headers["Origin"] == "https://v2.shlib.life"
    h = HentaiLibAdapter(transport=router.transport)
    await h.popular()
    assert router.calls[-1].headers["Site-Id"] == "4"


async def test_lib_sibling_fallback_image_server(router):
    router.add("GET", "/constants", httpx.Response(500))
    router.add("GET", "/chapter", J({"data": {"pages": [{"url": "//manga/x/1.jpg"}]}}))
    a = HentaiLibAdapter(transport=router.transport, config=None)
    a.http.max_retries = 0
    pages = await a.pages("x|1|1")
    assert pages[0].url == "https://img2h.imgslib.link/manga/x/1.jpg"


# --------------------------------------------------------------- MangaDex RU
async def test_mangadex_ru_requests_russian(router):
    router.add("GET", "/manga", J({"data": [], "total": 0, "limit": 24, "offset": 0}))
    a = MangaDexRuAdapter(transport=router.transport)
    await a.popular()
    assert q(router)["availableTranslatedLanguage[]"] == ["ru"]


# --------------------------------------------------------------- AstraManga
async def test_astramanga_catalog_title_pages(router):
    api = "https://api.astramanga.org/api/v1"
    router.add("GET", f"{api}/search", J({"data": {"titles": [
        {"id": 7, "slug": "tower", "name": "Башня", "cover_image": "covers/t.webp", "type": "manhwa"}],
        "total_pages": 3, "current_page": 1}}))
    router.add("GET", f"{api}/titles/tower", J({"data": {
        "id": 7, "slug": "tower", "name": "Башня", "secondary_name": "Tower", "alternative_names": ["塔"],
        "cover_versions": {"high": "covers/h.webp"}, "description": " Вверх. ", "type": "manhwa", "status": "ongoing",
        "genres": [{"name": "Фэнтези"}], "tags": [{"name": "Башня"}, {"name": "Фэнтези"}], "publishers": [{"name": "Naver"}]}}))
    router.add("GET", f"{api}/titles/7/branches", J({"data": {"branches": [
        {"id": 1, "count_chapters": 2, "name": "Команда А"}, {"id": 2, "count_chapters": 1, "name": "Команда Б"}]}}))
    router.add("GET", f"{api}/branches/1/chapters", J({"data": {"items": [
        {"id": 11, "number": 2, "volume_number": 1, "name": "Глава 2", "published_at": "2026-01-02T00:00:00Z"},
        {"id": 10, "number": 1, "name": "Начало"}]}}))
    router.add("GET", f"{api}/branches/2/chapters", J({"data": {"items": [{"id": 20, "number": 1.5}]}}))
    router.add("GET", f"{api}/chapters/11/pages", J({"data": {"pages": [
        {"image_url": "https://cdn/b.jpg", "page_number": 9}, {"image_url": "https://cdn/a.jpg", "page_number": 1}]}}))
    a = AstraMangaAdapter(transport=router.transport)
    page = await a.search(SearchQuery(query=" башня ", sort=SortOrder.UPDATED, types=[MangaType.MANHWA]))
    assert page.items[0].id == "tower" and page.items[0].cover_url == "https://astramanga.org/media/covers/t.webp"
    assert page.has_next and page.items[0].type == MangaType.MANHWA
    p = q(router)
    assert p["query"] == ["башня"] and p["sort"] == ["-updated_at"] and p["type"] == ["manhwa"]
    d, chs = await a.title("tower")
    assert d.alt_titles == ["Tower", "塔"] and d.genres == ["Фэнтези", "Башня"] and d.authors == ["Naver"]
    assert d.cover_url.endswith("/media/covers/h.webp") and d.status == PublicationStatus.ONGOING
    assert [(c.id, c.title, c.scanlator) for c in chs] == [
        ("11", "Том 1 Глава 2", "Команда А"), ("20", "Глава 1.5", "Команда Б"), ("10", "Глава 1: Начало", "Команда А")]
    assert [p.url for p in await a.pages("11")] == ["https://cdn/b.jpg", "https://cdn/a.jpg"]


# --------------------------------------------------------------- TomiloLib
def test_tomilo_image_urls():
    assert resolve_image_url("/uploads/covers/a.jpg", "https://tomilo-lib.ru") == "https://tomilolib.s3.regru.cloud/covers/a.jpg"
    assert resolve_image_url("https://tomilo-lib.ru/uploads/x.png", "https://tomilo-lib.ru") == "https://tomilolib.s3.regru.cloud/x.png"
    assert resolve_image_url("pages/1.webp", "https://tomilo-lib.ru") == "https://tomilolib.s3.regru.cloud/pages/1.webp"
    assert resolve_image_url(None, "x") == ""


async def test_tomilo_list_hides_adult_and_paginates_chapters(router):
    router.add("GET", "/api/titles?", J({"data": {"titles": [
        {"_id": "a1", "name": " Солнце ", "coverImage": "/uploads/c.jpg"},
        {"_id": "a2", "name": "18+", "isAdult": True}], "pagination": {"page": 1, "pages": 2}}}))
    router.add("GET", "/api/titles/a1", J({"data": {"_id": "a1", "slug": "sun", "name": "Солнце", "altNames": ["Sun", ""],
                                                     "genres": ["Драма"], "status": "pause", "author": "Ким", "isAdult": False}}))

    def chapters(req):
        page = parse_qs(urlparse(str(req.url)).query)["page"][0]
        if page == "1":
            return J({"data": {"chapters": [
                {"_id": "c1", "chapterNumber": 1, "releaseDate": "2025-05-01T00:00:00Z"},
                {"_id": "c0", "chapterNumber": 0.5, "isPublished": False}], "pagination": {"page": 1, "pages": 2}}})
        return J({"data": {"chapters": [{"_id": "c2", "chapterNumber": 2, "isPaid": True, "unlockPrice": 10,
                                         "freeAt": "2099-01-01T00:00:00Z"}], "pagination": {"page": 2, "pages": 2}}})

    router.add("GET", "/api/chapters?", chapters)
    router.add("GET", "/api/chapters/c2", J({"data": {"pages": [], "isPaid": True}}))
    router.add("GET", "/api/chapters/c1", J({"data": {"pages": ["/uploads/p/1.jpg"]}}))
    a = TomiloLibAdapter(transport=router.transport)
    page = await a.popular()
    assert [m.title for m in page.items] == ["Солнце"] and page.has_next
    assert q(router)["sortBy"] == ["views"]
    d, chs = await a.title("a1")
    assert d.web_url == "https://tomilo-lib.ru/titles/sun" and d.alt_titles == ["Sun"] and d.status == PublicationStatus.HIATUS
    assert [(c.id, c.scanlator) for c in chs] == [("c2", "🔒 Платно"), ("c1", None)]
    assert (await a.pages("c1"))[0].url == "https://tomilolib.s3.regru.cloud/p/1.jpg"
    with pytest.raises(SourceAuthRequiredError):
        await a.pages("c2")


# --------------------------------------------------------------- MangaPill (EN)
PILL_HOME = """<div><h4>Trending Mangas</h4><div class="grid">
<div><a href="/manga/2/one-piece"><img data-src="https://cdn/op.jpg"></a><a href="/manga/2/one-piece"><div class="line-clamp-2">One Piece</div></a></div>
<div class="ad">x</div></div></div>
<div><h4>Other</h4><div class="grid"><div><a href="/manga/9/x"><div class="line-clamp-2">X</div></a></div></div></div>"""
PILL_TITLE = """<div class="container"><div><div><img data-src="https://cdn/op-big.jpg"></div>
<div><h1>One Piece</h1><div>ワンピース</div><p class="text-sm">Pirates.</p>
<div><label class="text-secondary">Type</label><div>manga</div><label class="text-secondary">Status</label><div>publishing</div></div>
<a href="/search?genre=Action">Action</a></div></div>
<div id="chapters"><div><a href="/chapters/2-11100000/one-piece-chapter-1100">Chapter 1100</a><a href="/chapters/2-10010000/one-piece-chapter-1001.5">Chapter 1001.5</a></div></div></div>"""


async def test_mangapill(router):
    router.add("GET", "/chapters/2-11100000", H('<picture><img data-src="https://cdn/p1.jpg"></picture><picture><img data-src="https://cdn/p2.jpg"></picture>'))
    router.add("GET", "/manga/2/one-piece", H(PILL_TITLE))
    router.add("GET", "/search", H('<div class="grid"><div><a href="/manga/2/one-piece"><div class="line-clamp-2">One Piece</div></a></div></div><a class="btn btn-sm">Next</a>'))
    router.add("GET", "mangapill.com/", H(PILL_HOME))
    a = MangaPillAdapter(transport=router.transport)
    pop = await a.popular()
    assert [(m.id, m.title, m.cover_url) for m in pop.items] == [("/manga/2/one-piece", "One Piece", "https://cdn/op.jpg")]
    found = await a.search(SearchQuery(query="one piece"))
    assert found.has_next and found.items[0].title == "One Piece"
    d, chs = await a.title("/manga/2/one-piece")
    assert d.status == PublicationStatus.ONGOING and d.genres == ["Action"] and d.cover_url == "https://cdn/op-big.jpg"
    assert d.type == MangaType.MANGA and d.description == "Pirates."
    assert [c.number for c in chs] == [1100, 1001.5]
    pages = await a.pages(chs[0].id)
    assert [p.url for p in pages] == ["https://cdn/p1.jpg", "https://cdn/p2.jpg"]
    assert pages[0].headers["Referer"] == "https://mangapill.com/"


async def test_auth_walls_are_reported_as_auth_required(router):
    router.add("GET", "/vol1/1", H("<script>useContext('blockedForAnonymous')</script>", status=404))
    with pytest.raises(SourceAuthRequiredError, match="COOKIE"):
        await AllHentaiAdapter(transport=router.transport).pages("/x/vol1/1?mtr=true")
    router.add("GET", "/chapter", J({"data": {"toast": {"message": "Not Found"}}}, status=404))
    with pytest.raises(SourceAuthRequiredError, match="SOURCE_HENTAILIB_TOKEN"):
        await HentaiLibAdapter(transport=router.transport).pages("x|1|1")


def test_lib_token_is_cleaned():
    from mangaflow_sources.adapters.mangalib import clean_token

    assert clean_token("  Bearer abc.def ") == "abc.def"
    assert clean_token('"abc"') == "abc"
    assert clean_token('{"token": {"access_token": "xyz", "refresh_token": "r"}, "auth": {"id": 1}}') == "xyz"
    assert clean_token("") is None and clean_token(None) is None
