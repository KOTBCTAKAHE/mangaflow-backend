"""Concrete Grouple mirrors. Adding another mirror = one class with ``info``."""
from __future__ import annotations

from ..errors import SourceAuthRequiredError, SourceNotFoundError
from ..models import PageImage, SourceInfo
from .grouple import GROUPLE_CAPS, GROUPLE_GENRES, GroupleAdapter


class ReadMangaAdapter(GroupleAdapter):
    info = SourceInfo(
        id="readmanga",
        name="ReadManga",
        base_url="https://3.readmanga.ru",
        capabilities=GROUPLE_CAPS,
        genres=GROUPLE_GENRES,
        notes="Домены периодически меняются — задайте зеркало через SOURCE_READMANGA_BASE_URL.",
    )


class MintMangaAdapter(GroupleAdapter):
    info = SourceInfo(
        id="mintmanga",
        name="MintManga",
        base_url="https://2.mintmanga.one",
        is_nsfw=True,
        capabilities=GROUPLE_CAPS,
        genres=GROUPLE_GENRES,
        notes="Взрослая манга (16+/18+). Без cookie сессии часть глав отдаётся заглушкой.",
    )


class SelfMangaAdapter(GroupleAdapter):
    info = SourceInfo(
        id="selfmanga",
        name="SelfManga",
        base_url="https://1.selfmanga.live",
        capabilities=GROUPLE_CAPS,
        genres=GROUPLE_GENRES,
        notes="Русская авторская манга.",
    )


class SeiMangaAdapter(GroupleAdapter):
    info = SourceInfo(
        id="seimanga",
        name="SeiManga",
        base_url="https://1.seimanga.me",
        capabilities=GROUPLE_CAPS,
        genres=GROUPLE_GENRES,
        notes="Сеть Grouple (как ReadManga). Зеркало: SOURCE_SEIMANGA_BASE_URL.",
    )


class UsagiAdapter(GroupleAdapter):
    info = SourceInfo(
        id="usagi",
        name="Usagi",
        base_url="https://web.usagi.one",
        capabilities=GROUPLE_CAPS,
        genres=GROUPLE_GENRES,
        notes="Сеть Grouple. Зеркало: SOURCE_USAGI_BASE_URL.",
    )


class AllHentaiAdapter(GroupleAdapter):
    info = SourceInfo(
        id="allhentai",
        name="AllHentai",
        base_url="https://20.allhen.online",
        is_nsfw=True,
        capabilities=GROUPLE_CAPS,
        genres=GROUPLE_GENRES,
        notes="18+, сеть Grouple. Главы только для авторизованных — cookie оператора в SOURCE_ALLHENTAI_COOKIE.",
    )

    async def pages(self, chapter_id: str) -> list[PageImage]:
        try:
            return await super().pages(chapter_id)
        except SourceNotFoundError as e:
            # the site answers 404 + "blockedForAnonymous" to visitors without an account
            raise SourceAuthRequiredError(
                self.info.id, "главы AllHentai доступны только с аккаунтом: задайте SOURCE_ALLHENTAI_COOKIE", status=e.status,
            ) from e
