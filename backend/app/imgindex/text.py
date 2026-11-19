"""Plot search: title descriptions → multilingual-e5-small vectors (MIT, Russian + English).

One vector per title (by normalized name, across sources); the sources that have the
title are looked up in ``titles`` when results are shown.
"""
from __future__ import annotations

import re
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from usearch.index import Index

from .lexical import Lexical, fuse

MAX_TOKENS = 256
_TAGS = re.compile(r"\[/?[a-z_]+(?:=[^\]]*)?\]|<[^>]+>", re.I)
_WS = re.compile(r"\s+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plots (
  id INTEGER PRIMARY KEY, norm TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  source_id TEXT NOT NULL, manga_id TEXT NOT NULL, alt TEXT NOT NULL DEFAULT '',
  genres TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  vec BLOB, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS plots_src ON plots(source_id, manga_id);
"""


class TextEmbedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


def clean(text: str) -> str:
    return _WS.sub(" ", _TAGS.sub(" ", text or "")).strip()


def passage(title: str, alt: list[str], genres: list[str], description: str) -> str:
    parts = [title]
    if alt:
        parts.append(" / ".join(alt[:4]))
    if genres:
        parts.append("Жанры: " + ", ".join(genres[:12]))
    if description:
        parts.append(clean(description))
    return "passage: " + ". ".join(p for p in parts if p)


def query(text: str) -> str:
    return "query: " + clean(text)


class OnnxTextEmbedder:
    dim = 384  # replaced by the model's real output size in __init__ (small 384, base 768)

    def __init__(self, model_path: Path, tokenizer_path: Path, threads: int = 2) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), so, providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(str(tokenizer_path))
        self.tok.enable_truncation(MAX_TOKENS)
        self.tok.enable_padding(pad_id=self.tok.token_to_id("<pad>"), pad_token="<pad>")
        self._lock = threading.Lock()
        self.dim = int(self.embed(["passage: MangaFlow"]).shape[1])

    def embed(self, texts: list[str]) -> np.ndarray:
        out = []
        for i in range(0, len(texts), 16):
            enc = self.tok.encode_batch(texts[i:i + 16])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            with self._lock:
                h = self.session.run(None, {"input_ids": ids, "attention_mask": mask})[0]
            m = mask[..., None].astype(np.float32)
            out.append((h * m).sum(1) / np.maximum(m.sum(1), 1e-9))
        v = np.concatenate(out).astype(np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-8)


class TinyTextEmbedder:
    """Model-free stand-in for tests: hashed bag of word stems."""

    dim = 256

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"\w{3,}", t.lower().replace("ё", "е")):
                if w in ("query", "passage"):
                    continue
                out[i, zlib.crc32(w[:5].encode()) % self.dim] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-8)


class PlotIndex:
    """Vectors in usearch (key = plots.id), text and a float16 copy of the vector in SQLite."""

    def __init__(self, root: Path, db, lock: threading.RLock, dim: int) -> None:
        self.db = db
        self.lock = lock
        self.dim = dim
        self.db.executescript(SCHEMA)
        self.lexical = Lexical(db, lock)
        self.path = root / f"plots-{dim}.usearch"
        self.index = Index(ndim=dim, metric="cos", dtype="f16", connectivity=16)
        if self.path.exists():
            self.index.load(str(self.path))
        self._reconcile()
        self._dirty = 0

    def _lexical_text(self, alt: str, genres: str, description: str) -> str:
        return " ".join([alt.replace("\n", " "), genres.replace("\n", " "), description])

    def _reconcile(self) -> None:
        """Rebuild the ANN / word indexes from SQLite when they disagree (crash, model change)."""
        if len(self.lexical) != self.db.execute("SELECT COUNT(*) FROM plots").fetchone()[0]:
            with self.lock:
                self.db.execute("DELETE FROM plots_fts")
                for i, t, a, g, d in self.db.execute("SELECT id, title, alt, genres, description FROM plots").fetchall():
                    self.lexical.add(i, t, self._lexical_text(a, g, d))
        n = self.db.execute("SELECT COUNT(*) FROM plots WHERE vec IS NOT NULL").fetchone()[0]
        if n == len(self.index):
            return
        self.index = Index(ndim=self.dim, metric="cos", dtype="f16", connectivity=16)
        rows = self.db.execute("SELECT id, vec FROM plots WHERE vec IS NOT NULL").fetchall()
        good = [(i, np.frombuffer(v, dtype=np.float16)) for i, v in rows if v and len(v) == self.dim * 2]
        if good:
            self.index.add(np.array([i for i, _ in good], dtype=np.uint64),
                           np.stack([v for _, v in good]).astype(np.float32))
        bad = [i for i, v in rows if not v or len(v) != self.dim * 2]
        for i in bad:
            self.db.execute("UPDATE plots SET vec=NULL WHERE id=?", (i,))
        self._dirty = len(good) or 1

    def save(self) -> None:
        with self.lock:
            if not self._dirty:
                return
            tmp = self.path.with_suffix(".tmp")
            self.index.save(str(tmp))
            tmp.replace(self.path)
            self._dirty = 0

    def has_description(self, norm: str) -> bool:
        row = self.db.execute("SELECT description FROM plots WHERE norm=?", (norm,)).fetchone()
        return bool(row and row[0])

    def known(self, norm: str) -> bool:
        return self.db.execute("SELECT 1 FROM plots WHERE norm=?", (norm,)).fetchone() is not None

    def upsert(self, norm: str, source_id: str, manga_id: str, title: str, alt: list[str], genres: list[str],
               description: str) -> None:
        description = clean(description)
        with self.lock:
            row = self.db.execute("SELECT id, description FROM plots WHERE norm=?", (norm,)).fetchone()
            if row and len(row[1]) >= len(description):
                return  # keep the richer description we already have
            self.db.execute(
                "INSERT INTO plots(norm, title, source_id, manga_id, alt, genres, description, vec, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?) ON CONFLICT(norm) DO UPDATE SET title=excluded.title, "
                "source_id=excluded.source_id, manga_id=excluded.manga_id, alt=excluded.alt, genres=excluded.genres, "
                "description=excluded.description, vec=NULL, updated_at=excluded.updated_at",
                (norm, title, source_id, manga_id, "\n".join(alt[:6]), "\n".join(genres[:16]), description, time.time()))
            if row and self.index.contains(row[0]):
                self.index.remove(row[0])
            pid = self.db.execute("SELECT id FROM plots WHERE norm=?", (norm,)).fetchone()[0]
            self.lexical.add(pid, title, self._lexical_text("\n".join(alt[:6]), "\n".join(genres[:16]), description))

    def pending(self, limit: int = 32) -> list[tuple[int, str]]:
        rows = self.db.execute("SELECT id, title, alt, genres, description FROM plots "
                               "WHERE vec IS NULL AND (description != '' OR genres != '') LIMIT ?", (limit,)).fetchall()
        return [(i, passage(t, [x for x in a.split("\n") if x], [x for x in g.split("\n") if x], d))
                for i, t, a, g, d in rows]

    def set_vectors(self, ids: list[int], vecs: np.ndarray) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            for i, v in zip(ids, vecs, strict=True):
                self.db.execute("UPDATE plots SET vec=? WHERE id=?", (v.astype(np.float16).tobytes(), i))
            self.db.execute("COMMIT")
            keys = np.array(ids, dtype=np.uint64)
            for k in ids:
                if self.index.contains(k):
                    self.index.remove(k)
            self.index.add(keys, vecs.astype(np.float32))
            self._dirty += len(ids)

    def vector(self, norm: str) -> np.ndarray | None:
        row = self.db.execute("SELECT vec FROM plots WHERE norm=?", (norm,)).fetchone()
        if not row or not row[0] or len(row[0]) != self.dim * 2:
            return None
        return np.frombuffer(row[0], dtype=np.float16).astype(np.float32)

    def search(self, vec: np.ndarray, k: int = 40, exclude: set[str] | None = None,
               text: str | None = None) -> list[dict[str, Any]]:
        """Dense neighbours; with ``text`` also BM25 word matches, fused by rank (RRF)."""
        if not len(self.index):
            return []
        depth = max(100, k * 3) + len(exclude or ())
        with self.lock:
            res = self.index.search(vec.astype(np.float32), min(depth, len(self.index)))
        dense = [int(x) for x in np.atleast_1d(res.keys)]
        order = dense
        if text:
            words = [d for d, _ in self.lexical.search(text, depth)]
            order = [d for d, _ in fuse(dense, words)]
        ids = order[: depth]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = {r[0]: r for r in self.db.execute(
            f"SELECT id, norm, title, source_id, manga_id, genres, description, vec FROM plots WHERE id IN ({q})", ids)}
        out = []
        for i in ids:
            r = rows.get(i)
            if r is None or (exclude and r[1] in exclude) or not r[7]:
                continue
            sim = float(np.frombuffer(r[7], dtype=np.float16).astype(np.float32) @ vec)
            out.append({"norm": r[1], "title": r[2], "source_id": r[3], "manga_id": r[4],
                        "genres": [g for g in r[5].split("\n") if g], "description": r[6][:400],
                        "similarity": round(sim * 100, 1)})
            if len(out) >= k:
                break
        if not text:
            out.sort(key=lambda x: x["similarity"], reverse=True)
        return out

    def count(self) -> dict[str, int]:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {"plots": one("SELECT COUNT(*) FROM plots WHERE vec IS NOT NULL"),
                "plots_pending": one("SELECT COUNT(*) FROM plots WHERE vec IS NULL AND description != ''")}
