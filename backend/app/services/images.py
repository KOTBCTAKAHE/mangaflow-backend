"""Signed image proxy.

Source CDNs often require a Referer/Origin header that a mobile image widget
can't send, and some hosts block hot-linking. The backend therefore hands out
URLs like ``/api/v1/images/<signed-token>`` which:

* only resolve to URLs the server itself issued (HMAC; not an open proxy);
* are fetched with the adapter's ``image_headers``;
* are cached on disk and served with long-lived ``Cache-Control`` so a CDN or
  the bundled nginx ``proxy_cache`` can sit in front.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from mangaflow_sources import SourceRegistry
from mangaflow_sources.http import DEFAULT_UA

from ..core.config import Settings
from ..core.errors import AppError, NotFoundError
from ..core.logging import get_logger
from ..core.security import sign_payload, verify_signed

log = get_logger(__name__)

COVER_TTL = 365 * 24 * 3600
ALLOWED_WIDTHS = (160, 320, 480, 720, 1080)


@dataclass(slots=True)
class CachedImage:
    path: Path | None
    content_type: str
    data: bytes | None = None  # served from memory when the disk cache is not writable


def _writable_dir(preferred: str) -> Path:
    """The configured cache dir, or a temp dir when it is not writable (e.g. a volume
    created root-owned by an older image): images must keep working either way."""
    import tempfile

    for candidate in (Path(preferred), Path(tempfile.gettempdir()) / "mangaflow-images"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
            if candidate != Path(preferred):
                log.warning("image_cache_fallback", configured=preferred, using=str(candidate))
            return candidate
        except OSError as exc:
            log.warning("image_cache_not_writable", dir=str(candidate), error=str(exc))
    return Path(preferred)


class ImageSigner:
    def __init__(self, base_url: str, settings: Settings) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_ttl = settings.image_url_ttl_hours * 3600

    def url(self, source_id: str, raw_url: str, *, cover: bool = False) -> str:
        if not raw_url:
            return ""
        token = sign_payload({"s": source_id, "u": raw_url}, COVER_TTL if cover else self.page_ttl,
                             bucket_seconds=30 * 86400 if cover else 86400)
        return f"{self.base_url}/api/v1/images/{token}"


class ImageProxy:
    def __init__(self, registry: SourceRegistry, settings: Settings,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.registry = registry
        self.settings = settings
        self.dir = _writable_dir(settings.image_cache_dir)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=True, transport=transport,
            proxy=settings.sources_proxy, headers={"User-Agent": DEFAULT_UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_prune = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def decode(token: str) -> tuple[str, str]:
        body = verify_signed(token)
        if not body or "u" not in body or "s" not in body:
            raise NotFoundError("Ссылка на изображение недействительна или истекла", code="image_token_invalid")
        return body["s"], body["u"]

    async def get(self, token: str, width: int | None = None) -> CachedImage:
        source_id, url = self.decode(token)
        if width is not None:
            width = min(ALLOWED_WIDTHS, key=lambda w: abs(w - width))
        key = hashlib.sha256(f"{url}|{width}".encode()).hexdigest()
        path = self.dir / key[:2] / key
        meta = path.with_suffix(".ct")
        if path.exists() and meta.exists():
            return CachedImage(path, meta.read_text())
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if path.exists() and meta.exists():
                return CachedImage(path, meta.read_text())
            data, ctype = await self._fetch(source_id, url)
            if width:
                data, ctype = await asyncio.to_thread(_resize, data, width, ctype)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
                meta.write_text(ctype)
            except OSError as exc:  # full disk / read-only volume: still answer the request
                log.warning("image_cache_write_failed", error=str(exc))
                self._locks.pop(key, None)
                return CachedImage(None, ctype, data)
        self._locks.pop(key, None)
        await self._maybe_prune()
        return CachedImage(path, ctype)

    def cached(self, url: str) -> bytes | None:
        """The original (full-size) image if a reader already pulled it through the proxy."""
        key = hashlib.sha256(f"{url}|None".encode()).hexdigest()
        path = self.dir / key[:2] / key
        try:
            return path.read_bytes()
        except OSError:
            return None

    async def _fetch(self, source_id: str, url: str) -> tuple[bytes, str]:
        headers: dict[str, str] = {}
        if source_id in self.registry:
            headers.update(self.registry.get(source_id).image_headers(url))
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with self._client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 404:
                        raise NotFoundError("Изображение не найдено у источника", code="image_not_found")
                    if resp.status_code >= 400:
                        if resp.status_code >= 500 and attempt < 2:
                            await asyncio.sleep(0.4 * (attempt + 1))
                            continue
                        raise AppError(f"Источник вернул {resp.status_code}", code="image_upstream_error", status_code=502)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.image_max_bytes:
                            raise AppError("Изображение слишком большое", code="image_too_large", status_code=502)
                        chunks.append(chunk)
                data = b"".join(chunks)
                ctype = ctype if ctype.startswith("image/") else _sniff(data)
                if not ctype:
                    raise AppError("Источник вернул не изображение", code="image_invalid", status_code=502)
                return data, ctype
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        raise AppError(f"Не удалось загрузить изображение: {last_exc!r}", code="image_upstream_unavailable", status_code=504)

    async def _maybe_prune(self) -> None:
        if time.monotonic() - self._last_prune < 600:
            return
        self._last_prune = time.monotonic()
        await asyncio.to_thread(_prune, self.dir, self.settings.image_cache_max_mb * 1024 * 1024)


def _sniff(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return ""


def _resize(data: bytes, width: int, ctype: str) -> tuple[bytes, str]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width <= width:
                return data, ctype
            ratio = width / im.width
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            im = im.resize((width, max(1, int(im.height * ratio))), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, "WEBP", quality=82, method=4)
            return out.getvalue(), "image/webp"
    except Exception:  # noqa: BLE001 - serve original if Pillow can't decode
        return data, ctype


def _prune(root: Path, max_bytes: int) -> None:
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix not in (".ct", ".tmp")]
    total = sum(p.stat().st_size for p in files)
    if total <= max_bytes:
        return
    for p in sorted(files, key=lambda p: p.stat().st_atime):
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        p.with_suffix(".ct").unlink(missing_ok=True)
        if total <= max_bytes * 0.8:
            break
