"""Parsing helpers shared by adapters."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .models import MangaType, PublicationStatus

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean(text: str | None) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def parse_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        m = re.search(r"\d+(?:[.,]\d+)?", str(value))
        return float(m.group(0).replace(",", ".")) if m else None


def parse_ru_date(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse ``сегодня``/``вчера``/``DD.MM.YY(YY)``/ISO strings."""
    if not text:
        return None
    now = now or datetime.now(UTC)
    t = text.strip().lower()
    if "сегодня" in t:
        return now
    if "вчера" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", t)
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, int(m.group(2)), int(m.group(1)), tzinfo=UTC)
        except ValueError:
            return None
    return parse_iso(text)


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def from_unix(ts: object) -> datetime | None:
    try:
        value = float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value > 1e12:  # milliseconds
        value /= 1000
    return datetime.fromtimestamp(value, tz=UTC)


def status_from_ru(text: str | None) -> PublicationStatus:
    if not text:
        return PublicationStatus.UNKNOWN
    t = text.lower()
    if any(k in t for k in ("продолжается", "выходит", "онгоинг", "ongoing")):
        return PublicationStatus.ONGOING
    if any(k in t for k in ("заверш", "лицензир", "выпущен", "released", "finished")):
        return PublicationStatus.COMPLETED
    if any(k in t for k in ("заморож", "приостанов", "frozen")):
        return PublicationStatus.HIATUS
    if any(k in t for k in ("прекращ", "брош", "заброш", "dropped")):
        return PublicationStatus.CANCELLED
    if "анонс" in t or "announce" in t:
        return PublicationStatus.ANNOUNCED
    return PublicationStatus.UNKNOWN


def type_from_text(text: str | None) -> MangaType | None:
    if not text:
        return None
    t = text.lower()
    if "манхв" in t or "manhwa" in t:
        return MangaType.MANHWA
    if "маньхуа" in t or "manhua" in t:
        return MangaType.MANHUA
    if "комикс" in t or "comic" in t:
        return MangaType.COMIC
    if "манга" in t or "manga" in t:
        return MangaType.MANGA
    return MangaType.OTHER


_CHAPTER_RE = re.compile(r"^\s*(\d+)\s*[-—]\s*(\d+(?:\.\d+)?)\s*(?:[:—]\s*(.*))?$")


def split_volume_chapter(text: str) -> tuple[str | None, float | None, str]:
    """Grouple format ``"1 - 5: Начало"`` -> (volume, number, subtitle)."""
    m = _CHAPTER_RE.match(text)
    if not m:
        return None, parse_float(text) if re.search(r"\d", text) else None, text
    return m.group(1), float(m.group(2)), (m.group(3) or "").strip()


def chapter_title(number: float | None, name: str | None = None, prefix: str = "Глава") -> str:
    if number is None:
        return name or prefix
    num = str(int(number)) if float(number).is_integer() else str(number)
    return f"{prefix} {num}" + (f": {name}" if name else "")
