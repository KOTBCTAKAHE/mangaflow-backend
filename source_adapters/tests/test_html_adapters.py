from __future__ import annotations

import httpx
import pytest

from mangaflow_sources import PublicationStatus, SearchQuery, SourceParseError, SortOrder
from mangaflow_sources.adapters.acomics import AcomicsAdapter
from mangaflow_sources.adapters.grouple_sites import ReadMangaAdapter
from mangaflow_sources.adapters.mangabuff import MangaBuffAdapter
from mangaflow_sources.models import MangaType

from .conftest import fixture


async def test_grouple_list(router):
    router.add("GET", "/list?sortType=RATING&offset=0", httpx.Response(200, text=fixture("grouple_list.html")))
    a = ReadMangaAdapter(transport=router.transport)
    page = await a.popular(1)
    assert [m.id for m in page.items] == ["/vanpanchmen", "/berserk"]
    assert page.items[0].title == "Ванпанчмен"
    assert page.items[1].title == "Берсерк"
    assert page.items[1].cover_url.startswith("https://staticrm")
    assert page.has_next is False


async def test_grouple_search_and_genre_urls(router):
    router.add("GET", "/search/advancedResults", httpx.Response(200, text=fixture("grouple_list.html")))
    router.add("GET", "/list/genre/comedy", httpx.Response(200, text=fixture("grouple_list.html")))
    a = ReadMangaAdapter(transport=router.transport)
    await a.search(SearchQuery(query="ван пан", page=2))
    assert "q=%D0%B2%D0%B0%D0%BD%20%D0%BF%D0%B0%D0%BD" in str(router.calls[-1].url)
    assert "offset=50" in str(router.calls[-1].url)
    await a.search(SearchQuery(genres_include=["comedy"], sort=SortOrder.UPDATED))
    assert "sortType=UPDATED" in str(router.calls[-1].url)


async def test_grouple_title_and_chapters(router):
    router.add("GET", "/vanpanchmen", httpx.Response(200, text=fixture("grouple_title.html")))
    a = ReadMangaAdapter(transport=router.transport)
    details, chapters = await a.title("/vanpanchmen")
    assert details.title == "Ванпанчмен"
    assert details.alt_titles == ["One Punch-Man"]
    assert details.authors == ["ONE", "Мурата Юсукэ"]
    assert details.genres == ["боевик", "комедия"]
    assert details.status == PublicationStatus.ONGOING
    assert details.description.startswith("Сайтама")
    assert len(chapters) == 3
    first = chapters[0]
    assert first.title == "Глава 200: Финал арки"
    assert first.number == 200 and first.volume == "30"
    assert first.id == "/vanpanchmen/vol30/200?mtr=true"
    assert first.scanlator == "Translator"
    assert first.uploaded_at and first.uploaded_at.year == 2025
    assert chapters[1].title == "Глава 1" and chapters[1].volume == "1"
    assert chapters[2].title == "Экстра"


async def test_grouple_pages(router):
    router.add("GET", "/vol30/200", httpx.Response(200, text=fixture("grouple_reader.html")))
    a = ReadMangaAdapter(transport=router.transport)
    pages = await a.pages("/vanpanchmen/vol30/200?mtr=true")
    assert [p.url for p in pages] == [
        "https://one-way.work/auto/12/34/01.jpg",
        "https://t1.rmr.rocks/auto/12/34/02.jpg",
    ]
    assert pages[0].headers["Referer"].startswith("https://")


async def test_grouple_pages_missing_script_raises(router):
    router.add("GET", "/x", httpx.Response(200, text="<html>login required</html>"))
    a = ReadMangaAdapter(transport=router.transport)
    with pytest.raises(SourceParseError):
        await a.pages("/x")


async def test_grouple_mirror_override(router):
    from mangaflow_sources import SourceConfig

    router.add("GET", "mirror.example/list", httpx.Response(200, text=fixture("grouple_list.html")))
    a = ReadMangaAdapter(SourceConfig(base_url="https://mirror.example/"), transport=router.transport)
    await a.popular()
    assert router.calls[-1].url.host == "mirror.example"


async def test_mangabuff(router):
    router.add("GET", "/manga?sort=rating", httpx.Response(200, text=fixture("mangabuff_list.html")))
    router.add("GET", "/manga/solo-leveling/1/201", httpx.Response(200, text=fixture("mangabuff_reader.html")))
    router.add("GET", "/manga/solo-leveling", httpx.Response(200, text=fixture("mangabuff_title.html")))
    a = MangaBuffAdapter(transport=router.transport)
    page = await a.popular()
    assert [m.id for m in page.items] == ["/manga/solo-leveling", "/manga/tower"]
    assert page.items[0].cover_url == "https://mangabuff.ru/img/manga/posters/solo.jpg"

    details, chapters = await a.title("/manga/solo-leveling")
    assert details.title == "Поднятие уровня в одиночку"
    assert details.alt_titles == ["Solo Leveling", "Na Honjaman Level Up"]
    assert details.status == PublicationStatus.COMPLETED
    assert details.type == MangaType.MANHWA
    assert details.genres == ["Экшен", "Фэнтези"]
    assert [c.number for c in chapters] == [201, 200]
    assert chapters[0].title == "Глава 201: Эпилог"

    pages = await a.pages(chapters[0].id)
    assert [p.url for p in pages] == ["https://c3.mangabuff.ru/chapters/1.jpg", "https://c3.mangabuff.ru/chapters/2.jpg"]


async def test_acomics(router):
    router.add("GET", "/~snow/about", httpx.Response(200, text=fixture("acomics_about.html")))
    router.add("GET", "/~snow/2", httpx.Response(200, text='<img class="issue" src="/upload/!c/snow/2.png">'))
    a = AcomicsAdapter(transport=router.transport)
    details, chapters = await a.title("snow")
    assert details.title == "Снежная королева"
    assert details.authors == ["Анна"]
    assert [c.id for c in chapters] == ["/~snow/3", "/~snow/2", "/~snow/1"]
    pages = await a.pages("/~snow/2")
    assert pages[0].url == "https://acomics.ru/upload/!c/snow/2.png"
