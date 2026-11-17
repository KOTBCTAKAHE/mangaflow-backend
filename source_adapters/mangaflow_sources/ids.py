"""Opaque, URL-safe public identifiers.

Adapters use raw site references (paths with ``/`` and ``?``, slugs, numeric
ids). The API exposes them base64url-encoded so they can live in URL path
segments and database keys unchanged.
"""
from __future__ import annotations

import base64
import binascii
import re

_VALID = re.compile(r"^[A-Za-z0-9_-]+$")


def encode_ref(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_ref(public: str) -> str:
    if not public or not _VALID.match(public):
        raise ValueError("invalid id")
    padded = public + "=" * (-len(public) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid id") from exc
    if not raw:
        raise ValueError("invalid id")
    return raw
