"""Concrete Grouple mirrors. Adding another mirror = one class with ``info``."""
from __future__ import annotations

from ..models import SourceInfo
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
