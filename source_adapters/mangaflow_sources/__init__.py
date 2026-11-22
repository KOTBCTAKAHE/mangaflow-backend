"""MangaFlow Source Adapter Layer."""
from .base import SourceAdapter, SourceConfig
from .errors import (
    SourceAuthRequiredError,
    SourceBlockedError,
    SourceError,
    SourceNotFoundError,
    SourceParseError,
    SourceRateLimitedError,
    SourceUnavailableError,
    UnknownSourceError,
)
from .ids import decode_ref, encode_ref
from .models import (
    Chapter,
    ChapterText,
    Genre,
    MangaDetails,
    MangaListPage,
    MangaSummary,
    MangaType,
    PageImage,
    PublicationStatus,
    SearchQuery,
    SortOrder,
    SourceCapabilities,
    SourceInfo,
)
from .registry import BUILTIN_ADAPTERS, SourceRegistry

__all__ = [
    "BUILTIN_ADAPTERS", "Chapter", "ChapterText", "Genre", "MangaDetails", "MangaListPage", "MangaSummary", "MangaType",
    "PageImage", "PublicationStatus", "SearchQuery", "SortOrder", "SourceAdapter", "SourceAuthRequiredError",
    "SourceBlockedError", "SourceCapabilities", "SourceConfig", "SourceError", "SourceInfo",
    "SourceNotFoundError", "SourceParseError", "SourceRateLimitedError", "SourceRegistry",
    "SourceUnavailableError", "UnknownSourceError", "decode_ref", "encode_ref",
]
