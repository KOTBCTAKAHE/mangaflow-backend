"""Checks a source's account token and chapter access from inside the server.

    docker compose exec api python -m app.source_check hentailib [slug]

Prints whether the token is set and accepted (and whose it is), how many chapters a title
has with and without it, and clears the cached chapter lists of that source.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

from mangaflow_sources import SourceRegistry
from mangaflow_sources.registry import discover_adapter_classes

from .core.config import get_settings
from .main import source_configs


async def main(source_id: str, slug: str | None) -> int:
    settings = get_settings()
    classes = [c for c in discover_adapter_classes() if c.info.id == source_id]
    if not classes:
        print(f"Нет источника «{source_id}»")
        return 2
    cfg = source_configs(settings)[source_id]
    with_token = SourceRegistry.create({source_id: cfg}, classes=classes).get(source_id)
    without = SourceRegistry.create({source_id: replace(cfg, token=None)}, classes=classes).get(source_id)
    env = f"SOURCE_{source_id.upper()}_TOKEN"
    code = 0
    try:
        raw = cfg.token or ""
        token = getattr(with_token, "token", raw) or ""
        print(f"{env}: {'задан, ' + str(len(token)) + ' символов' if token else 'НЕ задан'}"
              + (" (очищен от «Bearer »/кавычек/JSON)" if token and token != raw.strip() else ""))
        if hasattr(with_token, "check_auth"):
            a = await with_token.check_auth()
            if a.get("ok"):
                print(f"Токен принят сайтом: пользователь «{a.get('user')}» (id {a.get('id')})")
            else:
                code = 1
                print(f"Токен НЕ принят: {a.get('reason') or 'сайт не вернул пользователя'}")
                print(f"  → войдите на {with_token.info.base_url}, F12 → Application → Local Storage → "
                      f"{with_token.info.base_url} → ключ «auth» → token.access_token")
        if not slug:
            page = await without.popular(1)
            slug = page.items[0].id if page.items else None
        if slug:
            n_with = len(await with_token.chapters(slug)) if token else None
            n_without = len(await without.chapters(slug))
            print(f"Главы «{slug}»: с токеном — {n_with if n_with is not None else '—'}, без токена — {n_without}")
            if token and not n_with:
                code = 1
        if settings.redis_url.startswith("redis"):
            from redis.asyncio import Redis

            r = Redis.from_url(settings.redis_url)
            n = 0
            async for k in r.scan_iter(match=f"mf:title:{source_id}:*", count=500):
                n += await r.delete(k)
            await r.aclose()
            print(f"Кэш тайтлов источника очищен: {n} записей")
    finally:
        for a in (with_token, without):
            await a.aclose()
    return code


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
