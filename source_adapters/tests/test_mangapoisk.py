from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mangaflow_sources import PublicationStatus, SearchQuery, SourceAuthRequiredError
from mangaflow_sources.adapters.mangapoisk import MangaPoiskAdapter, parse_poisk_date

B = "https://mangapoisk.me"


def H(text, status=200):
    return httpx.Response(status, text=text, headers={"content-type": "text/html; charset=utf-8"})


CATALOG = """<html><body>
<div class="manga-card"><a href="/manga/solo-leveling" title="Поднятие уровня в одиночку / Solo Leveling"><img data-src="/uploads/c1.jpg" src="/blank.gif"></a></div>
<div class="manga-card"><a href="https://mangapoisk.me/manga/berserk" title="Берсерк"><img src="https://cdn.mangapoisk.me/c2.jpg"></a></div>
<div class="manga-card"><a href="/manga/x"></a></div>
<ul><li><a href="?page=1">1</a></li><li><a href="?page=2">Вперёд »</a></li></ul>
</body></html>"""

SEARCH = """<html><body>
<article class="card"><a class="card-about" href="/manga/one-piece"><img src="/c/op.jpg"></a>
  <div class="post-description"><p class="card-title">Ван Пис / One Piece</p></div></article>
<ul class="pagination"><li><a aria-label="Вперёд" aria-disabled="true">»</a></li></ul>
</body></html>"""

TITLE = """<html><body>
<div class="card"><p>реклама</p></div>
<div class="card"><header>x</header>
  <h1 class="text-base"><span>Берсерк / Berserk</span></h1>
  <img class="w-full" src="/uploads/berserk.jpg">
  <span>Жанр: <a>Боевик</a>, <a>Ужасы</a></span>
  <span>Статус: Выпускается</span>
  <div class="manga-description">Чёрный мечник.</div>
</div></body></html>"""

CH1 = """<ul>
<li class="chapter-item"><a href="/manga/berserk/chapter/1-375">Том 41 Глава 375 Свет</a><span class="chapter-title">Глава 375</span><span class="chapter-date">3 дня назад</span></li>
<li class="chapter-item"><a href="/manga/berserk/chapter/41-374">Глава 374</a><span class="chapter-title">Глава 374</span><span class="chapter-date">12 марта 2024</span></li>
</ul><ul class="pagination"><li class="page-item">1</li><li class="page-item">2</li><li class="page-item">»</li></ul>"""
CH2 = """<li class="chapter-item"><a href="/manga/berserk/chapter/1-1">Глава 1</a><span class="chapter-title">Глава 1</span></li>"""


async def test_catalog_and_filters(router):
    router.add("GET", "/manga?", H(CATALOG))
    a = MangaPoiskAdapter(transport=router.transport)
    page = await a.search(SearchQuery(genres_include=["3", "5"], genres_exclude=["260"], statuses=[PublicationStatus.COMPLETED]))
    assert [(m.id, m.title) for m in page.items] == [("/manga/solo-leveling", "Поднятие уровня в одиночку"), ("/manga/berserk", "Берсерк")]
    assert page.items[0].cover_url == f"{B}/uploads/c1.jpg"
    assert page.has_next
    q = parse_qs(urlparse(str(router.calls[-1].url)).query)
    assert q["genres"] == ["[3,5]"] and q["genres-exclude"] == ["[260]"] and q["translated"] == ["[1]"]
    assert q["sortBy"] == ["popular"]


async def test_text_search(router):
    router.add("GET", "/search?", H(SEARCH))
    a = MangaPoiskAdapter(transport=router.transport)
    page = await a.search(SearchQuery(query="ван пис"))
    assert [m.title for m in page.items] == ["Ван Пис"] and not page.has_next
    assert (await a.search(SearchQuery(query="ва"))).items == []  # site needs ≥3 chars


async def test_title_chapters_pages(router):
    router.add("GET", "/manga/berserk/chaptersList?page=2", H(CH2))
    router.add("GET", "/manga/berserk/chaptersList", H(CH1))
    router.add("GET", "/manga/berserk/chapter/1-375", H('<img class="page-image" data-src="/p/1.jpg"><img class="page-image" src="https://img.me/2.webp">'))
    router.add("GET", "/manga/berserk/chapter/9-9", H('<div class="text-error-500-400-token">Удалено</div>'))
    router.add("GET", "/manga/berserk", H(TITLE))
    a = MangaPoiskAdapter(transport=router.transport)
    d, chs = await a.title("/manga/berserk")
    assert d.title == "Берсерк" and d.alt_titles == ["Berserk"] and d.genres == ["Боевик", "Ужасы"]
    assert d.status == PublicationStatus.ONGOING and d.cover_url == f"{B}/uploads/berserk.jpg"
    assert d.description == "Чёрный мечник."
    assert [c.number for c in chs] == [375, 374, 1]
    assert chs[0].title == "Том 41 Глава 375 Свет" and chs[1].uploaded_at == datetime(2024, 3, 12, tzinfo=UTC)
    pages = await a.pages("/manga/berserk/chapter/1-375")
    assert [p.url for p in pages] == [f"{B}/p/1.jpg", "https://img.me/2.webp"]
    with pytest.raises(SourceAuthRequiredError):
        await a.pages("/manga/berserk/chapter/9-9")


def test_dates():
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    assert parse_poisk_date("5 минут назад", now=now) == datetime(2026, 11, 18, 11, 55, tzinfo=UTC)
    assert parse_poisk_date("2 часа назад", now=now).hour == 10
    assert parse_poisk_date("1 января 2025", now=now) == datetime(2025, 1, 1, tzinfo=UTC)
    assert parse_poisk_date("непонятно", now=now) is None
