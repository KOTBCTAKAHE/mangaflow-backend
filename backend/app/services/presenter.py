"""Rewrite raw source image URLs into signed proxy URLs for API responses."""
from __future__ import annotations

from typing import Any

from .images import ImageSigner


class Presenter:
    def __init__(self, signer: ImageSigner) -> None:
        self.signer = signer

    def card(self, card: dict[str, Any]) -> dict[str, Any]:
        return {**card, "cover_url": self.signer.url(card["source_id"], card["cover_url"], cover=True)}

    def cards(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.card(c) for c in items]

    def listing(self, data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "items": self.cards(data["items"])}

    def title(self, data: dict[str, Any]) -> dict[str, Any]:
        m = data["manga"]
        return {"manga": {**m, "cover_url": self.signer.url(m["source_id"], m["cover_url"], cover=True)},
                "chapters": data["chapters"]}

    def text(self, source_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Text chapter: pictures inside go through the image proxy like pages do."""
        blocks = [
            {**b, "url": self.signer.url(source_id, b["url"])} if b.get("type") == "img" and b.get("url") else b
            for b in data["blocks"]
        ]
        return {**data, "blocks": blocks}

    def pages(self, source_id: str, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"index": p["index"], "url": self.signer.url(source_id, p["url"])} for p in pages]

    def search(self, data: dict[str, Any]) -> dict[str, Any]:
        items = []
        for g in data["items"]:
            first = g["sources"][0]["source_id"]
            items.append({
                **g,
                "cover_url": self.signer.url(first, g["cover_url"], cover=True),
                "sources": [{**s, "cover_url": self.signer.url(s["source_id"], s["cover_url"], cover=True)}
                            for s in g["sources"]],
            })
        return {**data, "items": items}
