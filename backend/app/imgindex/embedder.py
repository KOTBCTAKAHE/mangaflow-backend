"""Image → 512-d SSCD descriptor (Meta AI's copy-detection model, MIT).

SSCD is trained to match an image with its edited copies — crops, rescaling,
overlaid text, colour changes, screenshots — which is exactly a TikTok frame or
a phone screenshot of a page we have indexed.
"""
from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image, ImageOps

SIZE = 320
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# webtoon strips are cut into overlapping windows of this height/width ratio
TILE_RATIO = 1.4
TALL = 1.9  # h/w above this → strip: tiles instead of one squashed vector
WIDE = 1.25  # w/h above this → two-page spread: halves as well


class Embedder(Protocol):
    dim: int

    def embed(self, images: list[Image.Image]) -> np.ndarray: ...


def open_image(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (1600, 1600))  # JPEG: decode at reduced size, much faster for huge strips
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    return im


def tiles(im: Image.Image) -> list[Image.Image]:
    w, h = im.size
    t = int(w * TILE_RATIO)
    if t <= 0 or h <= t:
        return [im]
    out, y = [], 0
    while True:
        out.append(im.crop((0, y, w, min(h, y + t))))
        if y + t >= h:
            break
        y = min(y + t // 2, h - t)
    return out


def index_views(im: Image.Image) -> list[Image.Image]:
    """What gets stored for one page."""
    w, h = im.size
    if h / max(w, 1) > TALL:
        return tiles(im)
    views = [im]
    if w / max(h, 1) > WIDE:
        views += [im.crop((0, 0, w // 2, h)), im.crop((w // 2, 0, w, h))]
    return views


def trim_borders(im: Image.Image, tol: float = 10.0) -> Image.Image:
    """Cut flat bars (letterboxing, app chrome, blurred TikTok background is not flat — the
    user can crop that in the app)."""
    g = np.asarray(im.convert("L").resize((max(1, im.width // 4), max(1, im.height // 4))), dtype=np.float32)
    rows, cols = g.std(axis=1) > tol, g.std(axis=0) > tol
    if not rows.any() or not cols.any():
        return im
    y0, y1 = np.argmax(rows), len(rows) - np.argmax(rows[::-1])
    x0, x1 = np.argmax(cols), len(cols) - np.argmax(cols[::-1])
    box = (x0 * 4, y0 * 4, min(im.width, x1 * 4), min(im.height, y1 * 4))
    if (box[2] - box[0]) * (box[3] - box[1]) < 0.15 * im.width * im.height:
        return im
    return im.crop(box)


def query_views(im: Image.Image) -> list[Image.Image]:
    """A screenshot → several guesses of where the page is in it."""
    views = [im]
    t = trim_borders(im)
    if t.size != im.size:
        views.append(t)
    base = t
    w, h = base.size
    if h / max(w, 1) > TALL:
        views += tiles(base)
    elif w / max(h, 1) > WIDE:
        views += [base.crop((0, 0, w // 2, h)), base.crop((w // 2, 0, w, h))]
    # the page often sits in the middle with UI above and below
    if h > w:
        views.append(base.crop((0, int(h * 0.12), w, int(h * 0.88))))
    return views


def preprocess(images: list[Image.Image]) -> np.ndarray:
    arr = np.stack([np.asarray(im.resize((SIZE, SIZE), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
                    for im in images])
    arr = (arr - _MEAN) / _STD
    return arr.transpose(0, 3, 1, 2).astype(np.float32)


def l2(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-8)


class OnnxEmbedder:
    dim = 512

    def __init__(self, model_path: Path, threads: int = 2) -> None:
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), so, providers=["CPUExecutionProvider"])
        self._lock = threading.Lock()

    def embed(self, images: list[Image.Image]) -> np.ndarray:
        out = []
        for i in range(0, len(images), 8):
            batch = preprocess(images[i:i + 8])
            with self._lock:
                out.append(self.session.run(None, {"image": batch})[0])
        return l2(np.concatenate(out).astype(np.float32))


class TinyEmbedder:
    """Model-free fallback for tests: a 16×16 grey thumbnail, enough to find the same image."""

    dim = 256

    def embed(self, images: list[Image.Image]) -> np.ndarray:
        v = np.stack([np.asarray(im.convert("L").resize((16, 16)), dtype=np.float32).ravel() for im in images])
        v -= v.mean(axis=1, keepdims=True)
        return l2(v)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
