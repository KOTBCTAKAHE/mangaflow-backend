"""Vectors (usearch HNSW, int8) + what they point to (SQLite)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from usearch.index import Index

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, manga_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
  page_index INTEGER NOT NULL, UNIQUE(source_id, chapter_id, page_index));
CREATE TABLE IF NOT EXISTS vec (id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS vec_page ON vec(page_id);
CREATE TABLE IF NOT EXISTS chapters (
  source_id TEXT NOT NULL, chapter_id TEXT NOT NULL, manga_id TEXT NOT NULL,
  chapter_title TEXT NOT NULL DEFAULT '', chapter_number REAL,
  status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 0,
  added_at REAL NOT NULL, done_at REAL, error TEXT,
  PRIMARY KEY (source_id, chapter_id));
CREATE INDEX IF NOT EXISTS chapters_queue ON chapters(status, priority DESC, added_at);
CREATE TABLE IF NOT EXISTS titles (
  source_id TEXT NOT NULL, manga_id TEXT NOT NULL, title TEXT NOT NULL, norm TEXT NOT NULL,
  cover_url TEXT NOT NULL DEFAULT '', planned_at REAL, PRIMARY KEY (source_id, manga_id));
CREATE INDEX IF NOT EXISTS titles_norm ON titles(norm);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, root: Path, dim: int) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.dim = dim
        self.db = sqlite3.connect(root / "meta.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.path = root / f"vectors-{dim}.usearch"
        self.index = Index(ndim=dim, metric="cos", dtype="i8", connectivity=16)
        if self.path.exists():
            self.index.load(str(self.path))
        self._recover()
        self._dirty = 0
        self._saved_at = time.monotonic()

    # ------------------------------------------------------------ consistency
    def _recover(self) -> None:
        """Vectors added after the last save were lost on a crash: re-queue their chapters."""
        saved = int(self.kv("saved_max_vec") or 0)
        lost = self.db.execute(
            "SELECT DISTINCT p.source_id, p.chapter_id FROM vec v JOIN pages p ON p.id = v.page_id WHERE v.id > ?",
            (saved,)).fetchall()
        if not lost:
            return
        with self.lock:
            self.db.execute("BEGIN")
            for sid, cid in lost:
                self.db.execute("DELETE FROM vec WHERE page_id IN (SELECT id FROM pages WHERE source_id=? AND chapter_id=?)",
                                (sid, cid))
                self.db.execute("DELETE FROM pages WHERE source_id=? AND chapter_id=?", (sid, cid))
                self.db.execute("UPDATE chapters SET status='queued', done_at=NULL WHERE source_id=? AND chapter_id=?",
                                (sid, cid))
            self.db.execute("COMMIT")
            for key in [k for k in self._keys_above(saved)]:
                self.index.remove(key)

    def _keys_above(self, saved: int) -> list[int]:
        return [int(k) for k in self.index.keys if int(k) > saved] if len(self.index) else []

    def kv(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self.db.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value))
            return value
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def save(self, force: bool = False) -> None:
        with self.lock:
            if not self._dirty or (not force and self._dirty < 2000 and time.monotonic() - self._saved_at < 300):
                return
            tmp = self.path.with_suffix(".tmp")
            self.index.save(str(tmp))
            tmp.replace(self.path)
            top = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM vec").fetchone()[0]
            self.kv("saved_max_vec", str(top))
            self._dirty = 0
            self._saved_at = time.monotonic()

    # ------------------------------------------------------------ queue
    def enqueue(self, source_id: str, manga_id: str, chapter_id: str, *, priority: int = 0,
                chapter_title: str = "", chapter_number: float | None = None) -> bool:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO chapters(source_id, chapter_id, manga_id, chapter_title, chapter_number, priority, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_id, chapter_id) DO UPDATE SET "
                "priority=MAX(chapters.priority, excluded.priority), "
                "chapter_title=CASE WHEN excluded.chapter_title != '' THEN excluded.chapter_title ELSE chapters.chapter_title END, "
                "chapter_number=COALESCE(excluded.chapter_number, chapters.chapter_number), "
                "status=CASE WHEN chapters.status='failed' AND excluded.priority > 0 THEN 'queued' ELSE chapters.status END",
                (source_id, chapter_id, manga_id, chapter_title, chapter_number, priority, time.time()))
            return cur.rowcount > 0

    def next_chapter(self, min_priority: int = 0) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT source_id, chapter_id, manga_id, chapter_title, chapter_number, priority FROM chapters "
            "WHERE status='queued' AND priority >= ? ORDER BY priority DESC, added_at LIMIT 1", (min_priority,)).fetchone()
        if not row:
            return None
        keys = ("source_id", "chapter_id", "manga_id", "chapter_title", "chapter_number", "priority")
        return dict(zip(keys, row, strict=True))

    def finish_chapter(self, source_id: str, chapter_id: str, error: str | None = None) -> None:
        self.db.execute("UPDATE chapters SET status=?, done_at=?, error=? WHERE source_id=? AND chapter_id=?",
                        ("failed" if error else "done", time.time(), error, source_id, chapter_id))

    def queued(self, min_priority: int = 0) -> int:
        return self.db.execute("SELECT COUNT(*) FROM chapters WHERE status='queued' AND priority >= ?",
                               (min_priority,)).fetchone()[0]

    # ------------------------------------------------------------ titles
    def upsert_title(self, source_id: str, manga_id: str, title: str, norm: str, cover_url: str = "") -> None:
        self.db.execute(
            "INSERT INTO titles(source_id, manga_id, title, norm, cover_url) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id, manga_id) DO UPDATE SET title=excluded.title, norm=excluded.norm, "
            "cover_url=CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE titles.cover_url END",
            (source_id, manga_id, title, norm, cover_url))

    def title(self, source_id: str, manga_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT title, cover_url, planned_at FROM titles WHERE source_id=? AND manga_id=?",
                              (source_id, manga_id)).fetchone()
        return {"title": row[0], "cover_url": row[1], "planned_at": row[2]} if row else None

    def planned_elsewhere(self, source_id: str, norm: str) -> bool:
        return self.db.execute("SELECT 1 FROM titles WHERE norm=? AND source_id != ? AND planned_at IS NOT NULL LIMIT 1",
                               (norm, source_id)).fetchone() is not None

    def mark_planned(self, source_id: str, manga_id: str) -> None:
        self.db.execute("UPDATE titles SET planned_at=? WHERE source_id=? AND manga_id=?", (time.time(), source_id, manga_id))

    # ------------------------------------------------------------ vectors
    def has_page(self, source_id: str, chapter_id: str, page_index: int) -> bool:
        return self.db.execute("SELECT 1 FROM pages WHERE source_id=? AND chapter_id=? AND page_index=?",
                               (source_id, chapter_id, page_index)).fetchone() is not None

    def add_page(self, source_id: str, manga_id: str, chapter_id: str, page_index: int, vectors: np.ndarray) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                cur = self.db.execute("INSERT OR IGNORE INTO pages(source_id, manga_id, chapter_id, page_index) VALUES (?, ?, ?, ?)",
                                      (source_id, manga_id, chapter_id, page_index))
                if not cur.rowcount:
                    self.db.execute("ROLLBACK")
                    return
                page_id = cur.lastrowid
                ids = []
                for _ in range(len(vectors)):
                    ids.append(self.db.execute("INSERT INTO vec(page_id) VALUES (?)", (page_id,)).lastrowid)
                self.index.add(np.array(ids, dtype=np.uint64), vectors.astype(np.float32))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            self._dirty += len(vectors)

    def __len__(self) -> int:
        return len(self.index)

    def search(self, vectors: np.ndarray, k: int = 40) -> list[tuple[int, float]]:
        """Best similarity per page over all query views → [(page_id, sim)]."""
        if not len(self.index):
            return []
        with self.lock:
            res = self.index.search(vectors.astype(np.float32), min(k, len(self.index)))
        keys = np.atleast_2d(res.keys)
        dist = np.atleast_2d(res.distances)
        vec_ids = sorted({int(x) for x in keys.ravel()})
        if not vec_ids:
            return []
        q = ",".join("?" * len(vec_ids))
        page_of = dict(self.db.execute(f"SELECT id, page_id FROM vec WHERE id IN ({q})", vec_ids).fetchall())
        best: dict[int, float] = {}
        for krow, drow in zip(keys, dist, strict=True):
            for kk, dd in zip(krow, drow, strict=True):
                pid = page_of.get(int(kk))
                if pid is None:
                    continue
                sim = 1.0 - float(dd)
                if sim > best.get(pid, -1.0):
                    best[pid] = sim
        return sorted(best.items(), key=lambda x: x[1], reverse=True)

    def page_meta(self, page_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not page_ids:
            return {}
        q = ",".join("?" * len(page_ids))
        rows = self.db.execute(
            f"SELECT p.id, p.source_id, p.manga_id, p.chapter_id, p.page_index, c.chapter_title, c.chapter_number, "
            f"t.title, t.cover_url FROM pages p "
            f"LEFT JOIN chapters c ON c.source_id = p.source_id AND c.chapter_id = p.chapter_id "
            f"LEFT JOIN titles t ON t.source_id = p.source_id AND t.manga_id = p.manga_id WHERE p.id IN ({q})",
            page_ids).fetchall()
        keys = ("source_id", "manga_id", "chapter_id", "page_index", "chapter_title", "chapter_number", "title", "cover_url")
        return {r[0]: dict(zip(keys, r[1:], strict=True)) for r in rows}

    def stats(self) -> dict[str, Any]:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {
            "vectors": len(self.index),
            "pages": one("SELECT COUNT(*) FROM pages"),
            "chapters_done": one("SELECT COUNT(*) FROM chapters WHERE status='done'"),
            "chapters_queued": one("SELECT COUNT(*) FROM chapters WHERE status='queued'"),
            "chapters_failed": one("SELECT COUNT(*) FROM chapters WHERE status='failed'"),
            "titles": one("SELECT COUNT(DISTINCT manga_id) FROM pages"),
        }

    def close(self) -> None:
        self.save(force=True)
        self.db.close()
