"""Other LibGroup sites: the same api2.mangalib.me API, told apart by the ``Site-Id`` header.

Port of keiyoushi lib-multisrc/libgroup (YaoiLib = site 2, HentaiLib = site 4).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..errors import SourceAuthRequiredError, SourceNotFoundError, SourceParseError
from ..models import ChapterText, MangaListPage, MangaType
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


class RanobeLibAdapter(MangaLibAdapter):
    """Ранобэ: the same LibGroup API (site 3); a chapter's ``content`` is a ProseMirror document
    (older chapters: HTML) instead of pages."""

    site_id = 3
    info = replace(
        MangaLibAdapter.info,
        id="ranobelib",
        name="RanobeLib",
        base_url="https://ranobelib.me",
        content="text",
        capabilities=replace(MangaLibAdapter.info.capabilities, type_filter=False),
        notes="Ранобэ сети MangaLib. Главы — текст; 18+ — только с токеном (SOURCE_RANOBELIB_TOKEN).",
    )

    @staticmethod
    def _novel(page: MangaListPage) -> MangaListPage:
        for m in page.items:
            m.type = MangaType.NOVEL
        return page

    async def popular(self, page: int = 1) -> MangaListPage:
        return self._novel(await super().popular(page))

    async def latest(self, page: int = 1) -> MangaListPage:
        return self._novel(await super().latest(page))

    async def search(self, query) -> MangaListPage:  # noqa: ANN001
        return self._novel(await super().search(query))

    async def details(self, manga_id: str):  # noqa: ANN201
        d = await super().details(manga_id)
        d.type = MangaType.NOVEL
        return d

    async def pages(self, chapter_id: str):  # noqa: ANN201
        raise SourceParseError(self.info.id, "ранобэ: главы читаются как текст")

    async def text(self, chapter_id: str) -> ChapterText:
        parts = chapter_id.split("|")
        if len(parts) < 3:
            raise SourceParseError(self.info.id, "bad chapter id")
        slug, number, volume = parts[0], parts[1], parts[2]
        params = [("number", number), ("volume", volume)]
        if len(parts) > 3 and parts[3]:
            params.append(("branch_id", parts[3]))
        try:
            j = await self._get(f"{self.api}/manga/{quote(slug)}/chapter", params=params)
        except SourceNotFoundError as e:
            if self.token:
                raise
            raise SourceAuthRequiredError(self.info.id, "глава 18+ доступна только с токеном (SOURCE_RANOBELIB_TOKEN)",
                                          status=404) from e
        d = j.get("data") or {}
        attachments = d.get("attachments") or []
        title = " ".join(x for x in (f"Том {volume}" if volume else "", f"Глава {number}", str(d.get("name") or "")) if x)
        return ChapterText(title=title, blocks=lib_blocks(d.get("content"), attachments, self.info.base_url))


# ---------------------------------------------------------------- text chapters

def _attachment_url(ref: Any, attachments: list[dict[str, Any]], base: str) -> str:
    ref = str(ref or "")
    for a in attachments:
        if ref and ref in (str(a.get("name")), str(a.get("id")), str(a.get("filename"))):
            url = str(a.get("url") or "")
            return url if url.startswith("http") else base.rstrip("/") + "/" + url.lstrip("/")
    if ref.startswith("http"):
        return ref
    return ""


def _spans(nodes: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "hardBreak":
            out.append({"t": "\n"})
            continue
        if n.get("type") != "text":
            out.extend(_spans(n.get("content") or []))
            continue
        marks = {m.get("type") for m in n.get("marks") or [] if isinstance(m, dict)}
        span = {"t": str(n.get("text") or "")}
        if "italic" in marks:
            span["i"] = True
        if "bold" in marks:
            span["b"] = True
        out.append(span)
    return out


def _has_text(spans: list[dict[str, Any]]) -> bool:
    return any(s.get("t", "").strip() for s in spans)


def lib_blocks(content: Any, attachments: list[dict[str, Any]], base: str) -> list[dict[str, Any]]:
    """LibGroup chapter content (ProseMirror JSON or HTML) → blocks the app lays out itself."""
    if isinstance(content, str):
        return html_blocks(content, base)
    blocks: list[dict[str, Any]] = []

    def walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        t = n.get("type")
        kids = n.get("content") or []
        if t in ("paragraph",):
            sp = _spans(kids)
            blocks.append({"type": "p", "spans": sp} if _has_text(sp) else {"type": "gap"})
        elif t == "heading":
            sp = _spans(kids)
            if _has_text(sp):
                blocks.append({"type": "h", "spans": sp})
        elif t == "horizontalRule":
            blocks.append({"type": "hr"})
        elif t == "blockquote":
            for k in kids:
                sp = _spans(k.get("content") or []) if isinstance(k, dict) else []
                if _has_text(sp):
                    blocks.append({"type": "quote", "spans": sp})
        elif t == "image":
            attrs = n.get("attrs") or {}
            refs = [i.get("image") for i in attrs.get("images") or [] if isinstance(i, dict)] or [attrs.get("src")]
            for r in refs:
                url = _attachment_url(r, attachments, base)
                if url:
                    blocks.append({"type": "img", "url": url})
        else:  # doc, lists, unknown wrappers
            for k in kids:
                walk(k)

    walk(content or {})
    # a run of empty paragraphs is one pause, not a page of blank lines
    out: list[dict[str, Any]] = []
    for b in blocks:
        if b["type"] == "gap" and (not out or out[-1]["type"] in ("gap", "hr")):
            continue
        out.append(b)
    while out and out[-1]["type"] == "gap":
        out.pop()
    return out


def html_blocks(html: str, base: str) -> list[dict[str, Any]]:
    tree = HTMLParser(html or "")
    blocks: list[dict[str, Any]] = []
    root = tree.body or tree.root
    if root is None:
        return blocks
    for el in root.traverse(include_text=False):
        tag = el.tag
        inside_quote = False
        parent = el.parent
        while parent is not None:
            if parent.tag == "blockquote":
                inside_quote = True
                break
            parent = parent.parent
        if inside_quote and tag != "img":
            continue
        nested = any(n.tag in ("p", "div", "img") for n in list(el.traverse(include_text=False))[1:])
        if tag in ("p", "div") and not nested:
            text = el.text(separator="", strip=True)
            if text:
                blocks.append({"type": "p", "spans": [{"t": text}]})
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = el.text(strip=True)
            if text:
                blocks.append({"type": "h", "spans": [{"t": text}]})
        elif tag == "blockquote":
            text = el.text(separator=" ", strip=True)
            if text:
                blocks.append({"type": "quote", "spans": [{"t": text}]})
        elif tag == "hr":
            blocks.append({"type": "hr"})
        elif tag == "img":
            src = el.attributes.get("src") or el.attributes.get("data-src") or ""
            if src:
                blocks.append({"type": "img", "url": src if src.startswith("http") else base.rstrip("/") + "/" + src.lstrip("/")})
    return blocks

