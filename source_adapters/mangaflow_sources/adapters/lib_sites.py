"""Other LibGroup sites: the same api2.mangalib.me API, told apart by the ``Site-Id`` header.

Port of keiyoushi lib-multisrc/libgroup (YaoiLib = site 2, HentaiLib = site 4).
"""
from __future__ import annotations

from dataclasses import replace

from .mangalib import MangaLibAdapter


class YaoiLibAdapter(MangaLibAdapter):
    site_id = 2
    info = replace(
        MangaLibAdapter.info,
        id="yaoilib",
        name="YaoiLib (SlashLib)",
        base_url="https://v2.shlib.life",
        is_nsfw=True,
        notes="Сайт сети MangaLib (яой/BL). 18+ главы — только с токеном оператора (SOURCE_YAOILIB_TOKEN).",
    )
    fallback_servers = ("https://img3.mixlib.me", "https://img2.imglib.info")


class HentaiLibAdapter(MangaLibAdapter):
    site_id = 4
    info = replace(
        MangaLibAdapter.info,
        id="hentailib",
        name="HentaiLib",
        base_url="https://hentailib.me",
        is_nsfw=True,
        notes="18+. Сайт сети MangaLib; большинство глав отдаётся только с токеном оператора (SOURCE_HENTAILIB_TOKEN).",
    )
    fallback_servers = ("https://img2h.imgslib.link", "https://img3.mixlib.me")
