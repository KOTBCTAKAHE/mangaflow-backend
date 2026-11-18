from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx

from mangaflow_sources import MangaType, PublicationStatus, SearchQuery, SourceConfig
from mangaflow_sources.adapters.mangadex import MangaDexAdapter

MID = "a1c7c817-4e59-43b7-9365-09675a149a6f"


def J(data, status=200):
    return httpx.Response(status, json=data)


def q(req: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(req.url)).query)


LIST = {
    "data": [{
        "id": MID,
        "attributes": {"title": {"ja-ro": "One Piece"}, "altTitles": [{"en": "One Piece EN"}], "originalLanguage": "ja"},
        "relationships": [{"type": "cover_art", "attributes": {"fileName": "cover.jpg"}}],
    }, {
        "id": "b2",
        "attributes": {"title": {"en": "Solo Leveling"}, "originalLanguage": "ko"},
        "relationships": [],
    }],
    "total": 95,
}
TAGS = {"data": [
    {"id": "tag-action", "attributes": {"name": {"en": "Action"}, "group": "genre"}},
    {"id": "tag-romance", "attributes": {"name": {"en": "Romance"}, "group": "genre"}},
]}


async def test_list_and_filters(router):
    router.add("GET", "/manga/tag", J(TAGS))
    router.add("GET", "/manga?", J(LIST))
    a = MangaDexAdapter(transport=router.transport)
    page = await a.search(SearchQuery(page=2, genres_include=["Action"], genres_exclude=["romance"],
                                      types=[MangaType.MANHUA], statuses=[PublicationStatus.ONGOING]))
    assert [m.title for m in page.items] == ["One Piece EN", "Solo Leveling"]
    assert page.items[0].cover_url == f"https://uploads.mangadex.org/covers/{MID}/cover.jpg.512.jpg"
    assert page.items[0].type == MangaType.MANGA and page.items[1].type == MangaType.MANHWA
    assert page.has_next  # offset 30 + 30 < 95
    params = q(router.calls[-1])
    assert params["offset"] == ["30"]
    assert params["includedTags[]"] == ["tag-action"] and params["excludedTags[]"] == ["tag-romance"]
    assert params["originalLanguage[]"] == ["zh", "zh-hk"] and params["status[]"] == ["ongoing"]
    assert params["availableTranslatedLanguage[]"] == ["en"]
    assert params["contentRating[]"] == ["safe", "suggestive"]
    # tags are fetched once
    await a.search(SearchQuery(genres_include=["Action"]))
    assert sum(1 for c in router.calls if "/manga/tag" in str(c.url)) == 1


async def test_text_search_and_last_page(router):
    router.add("GET", "/manga?", J({**LIST, "total": 2}))
    a = MangaDexAdapter(transport=router.transport)
    page = await a.search(SearchQuery(query="one piece"))
    assert not page.has_next
    assert q(router.calls[-1])["title"] == ["one piece"]


async def test_details_chapters_pages(router):
    router.add("GET", f"/manga/{MID}?", J({"data": {
        "id": MID,
        "attributes": {
            "title": {"en": "One Piece"}, "altTitles": [{"ja": "ワンピース"}, {"ru": "Ван Пис"}],
            "description": {"en": "Pirates!", "ru": "Пираты!"}, "status": "ongoing", "originalLanguage": "ja",
            "contentRating": "safe",
            "tags": [{"attributes": {"name": {"en": "Action"}, "group": "genre"}},
                     {"attributes": {"name": {"en": "Long Strip"}, "group": "format"}}],
        },
        "relationships": [{"type": "author", "attributes": {"name": "Oda Eiichiro"}},
                          {"type": "cover_art", "attributes": {"fileName": "c.png"}}],
    }}))
    feed = {"data": [
        {"id": "c3", "attributes": {"chapter": "1100", "volume": None, "title": "Dawn", "pages": 17,
                                    "publishAt": "2026-11-01T00:00:00+00:00"},
         "relationships": [{"type": "scanlation_group", "attributes": {"name": "TCB"}}]},
        {"id": "c3dup", "attributes": {"chapter": "1100", "volume": None, "pages": 18}, "relationships": []},
        {"id": "ext", "attributes": {"chapter": "1099", "externalUrl": "https://mangaplus", "pages": 0}, "relationships": []},
        {"id": "c1", "attributes": {"chapter": None, "title": "Romance Dawn (oneshot)", "pages": 40}, "relationships": []},
    ], "total": 4}
    router.add("GET", f"/manga/{MID}/feed", J(feed))
    router.add("GET", "/at-home/server/c3", J({"baseUrl": "https://node.mangadex.network/tok", "chapter": {
        "hash": "abc", "data": ["1.png", "2.png"], "dataSaver": ["1.jpg", "2.jpg"]}}))
    a = MangaDexAdapter(transport=router.transport)
    d, chs = await a.title(MID)
    assert d.title == "One Piece" and d.description == "Pirates!"
    assert "Ван Пис" in d.alt_titles and d.genres == ["Action"] and d.authors == ["Oda Eiichiro"]
    assert d.status == PublicationStatus.ONGOING and d.web_url.endswith(f"/title/{MID}")
    assert [c.id for c in chs] == ["c3", "c1"]
    assert chs[0].title == "Chapter 1100: Dawn" and chs[0].scanlator == "TCB" and chs[0].number == 1100
    assert chs[1].title == "Romance Dawn (oneshot)"
    feed_q = q(next(c for c in router.calls if "/feed" in str(c.url)))
    assert feed_q["translatedLanguage[]"] == ["en"]
    pages = await a.pages("c3")
    assert [p.url for p in pages] == ["https://uploads.mangadex.org/data/abc/1.png", "https://uploads.mangadex.org/data/abc/2.png"]


async def test_language_is_configurable(router):
    router.add("GET", f"/manga/{MID}/feed", J({"data": [], "total": 0}))
    a = MangaDexAdapter(SourceConfig(extra={"lang": "ru"}), transport=router.transport)
    assert await a.chapters(MID) == []
    assert q(router.calls[-1])["translatedLanguage[]"] == ["ru"]
