"""Stdlib-only HTTP server producing deterministic PNG pages/covers for E2E."""
from __future__ import annotations

import hashlib
import struct
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def png(width: int, height: int, seed: str) -> bytes:
    h = hashlib.sha256(seed.encode()).digest()
    base = (h[0], h[1], h[2])
    rows = []
    for y in range(height):
        band = (y // 40) % 2
        r, g, b = (c // 2 + (60 if band else 0) for c in base)
        rows.append(b"\x00" + bytes((r, g, b)) * width)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/cover/"):
            body = png(300, 428, self.path)
        elif self.path.startswith("/page/"):
            body = png(720, 1040, self.path)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
