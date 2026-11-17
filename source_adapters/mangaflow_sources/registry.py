"""Adapter registry.

Built-in adapters are listed in :data:`BUILTIN_ADAPTERS`. Third-party packages
can contribute adapters without touching this code via the
``mangaflow.sources`` entry-point group::

    [project.entry-points."mangaflow.sources"]
    mysite = "my_pkg.mysite:MySiteAdapter"
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Iterable

import httpx

from .adapters.acomics import AcomicsAdapter
from .adapters.desu import DesuAdapter
from .adapters.grouple_sites import MintMangaAdapter, ReadMangaAdapter, SelfMangaAdapter
from .adapters.mangabuff import MangaBuffAdapter
from .adapters.mangalib import MangaLibAdapter
from .adapters.remanga import RemangaAdapter
from .adapters.senkuro import SenkuroAdapter
from .base import SourceAdapter, SourceConfig
from .errors import UnknownSourceError
from .models import SourceInfo

log = logging.getLogger(__name__)

BUILTIN_ADAPTERS: tuple[type[SourceAdapter], ...] = (
    MangaLibAdapter,
    RemangaAdapter,
    SenkuroAdapter,
    ReadMangaAdapter,
    MintMangaAdapter,
    SelfMangaAdapter,
    DesuAdapter,
    MangaBuffAdapter,
    AcomicsAdapter,
)


def discover_adapter_classes() -> list[type[SourceAdapter]]:
    classes = list(BUILTIN_ADAPTERS)
    known = {c.info.id for c in classes}
    for ep in entry_points(group="mangaflow.sources"):
        try:
            cls = ep.load()
        except Exception:  # noqa: BLE001 - a broken plugin must not take the app down
            log.exception("failed to load source plugin %s", ep.name)
            continue
        if isinstance(cls, type) and issubclass(cls, SourceAdapter) and cls.info.id not in known:
            classes.append(cls)
            known.add(cls.info.id)
    return classes


class SourceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    @classmethod
    def create(
        cls,
        configs: dict[str, SourceConfig] | None = None,
        *,
        disabled: Iterable[str] = (),
        classes: Iterable[type[SourceAdapter]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "SourceRegistry":
        reg = cls()
        configs = configs or {}
        disabled_set = set(disabled)
        for adapter_cls in classes if classes is not None else discover_adapter_classes():
            sid = adapter_cls.info.id
            if sid in disabled_set:
                continue
            reg.register(adapter_cls(configs.get(sid), transport=transport))
        return reg

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.info.id] = adapter

    def get(self, source_id: str) -> SourceAdapter:
        try:
            return self._adapters[source_id]
        except KeyError:
            raise UnknownSourceError(source_id) from None

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._adapters

    def all(self) -> list[SourceAdapter]:
        return list(self._adapters.values())

    def infos(self) -> list[SourceInfo]:
        return [a.info for a in self._adapters.values()]

    async def aclose(self) -> None:
        for a in self._adapters.values():
            await a.aclose()
