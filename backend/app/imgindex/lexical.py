"""Word matching for plot search: BM25 over stemmed descriptions (Russian + English).

Dense vectors understand paraphrases («гиганты-людоеды» ≈ «титаны»), but miss rare exact
words a reader remembers («гуль», «причуды», «приют», «палец»). Ranks of both are fused
with reciprocal rank fusion.
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict

import snowballstemmer

_WORD = re.compile(r"[a-zа-я0-9]+")
_CYR = re.compile(r"[а-я]")
STOP = frozenset("""
это этот эта эти этого этой этим который которая которое которые которого которой которых
его её ее их ему ей им они она оно он мы вы ты меня тебя себя свой своя свои своего своей
что чтобы как когда где если или либо так также тоже уже ещё еще лишь только даже очень
был была было были будет будут быть есть нет без для при про под над между после перед
через из-за всех весь вся всё все всего всей тот того той тем том там тут здесь чем чего
кто кого кому однако хотя потом теперь затем сначала снова может могут можно нужно надо
the and for with that this from his her their they them was were are has have had but not
who whom which what when where into onto about after before over under than then also just
""".split())


class Tokenizer:
    def __init__(self) -> None:
        self.ru = snowballstemmer.stemmer("russian")
        self.en = snowballstemmer.stemmer("english")
        self._lock = threading.Lock()  # snowball stemmers keep state

    def __call__(self, text: str) -> list[str]:
        words = [w for w in _WORD.findall((text or "").lower().replace("ё", "е")) if len(w) >= 3 and w not in STOP]
        with self._lock:
            return [self.ru.stemWord(w) if _CYR.search(w) else self.en.stemWord(w) for w in words]


class Lexical:
    """BM25 via SQLite FTS5 over pre-stemmed text (FTS5 itself has no Russian stemmer).
    Lives in the index's SQLite file: no RAM cost, updated with each description."""

    def __init__(self, db, lock: threading.RLock) -> None:
        self.db = db
        self.lock = lock
        self.tok = Tokenizer()
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS plots_fts USING fts5(title, body, "
                        "tokenize='unicode61 remove_diacritics 0')")

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM plots_fts").fetchone()[0]

    def add(self, doc: int, title: str, text: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM plots_fts WHERE rowid=?", (doc,))
            self.db.execute("INSERT INTO plots_fts(rowid, title, body) VALUES (?, ?, ?)",
                            (doc, " ".join(self.tok(title)), " ".join(self.tok(text))))

    def search(self, query: str, k: int = 100) -> list[tuple[int, float]]:
        terms = list(dict.fromkeys(self.tok(query)))[:32]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        with self.lock:
            rows = self.db.execute(
                "SELECT rowid, bm25(plots_fts, 2.0, 1.0) AS s FROM plots_fts WHERE plots_fts MATCH ? ORDER BY s LIMIT ?",
                (match, k)).fetchall()
        return [(int(r), -float(sc)) for r, sc in rows]  # FTS5 bm25: lower is better


def fuse(*rankings: list[int], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal rank fusion: robust to the two scores living on different scales."""
    score: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for r, doc in enumerate(ranking):
            score[doc] += 1.0 / (k + r + 1)
    return sorted(score.items(), key=lambda x: x[1], reverse=True)
