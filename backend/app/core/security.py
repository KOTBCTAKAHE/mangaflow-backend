"""Password hashing (Argon2id), JWT access tokens, opaque refresh tokens, HMAC signing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import get_settings

_hasher = PasswordHasher()
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


def create_access_token(user_id: uuid.UUID, *, session_id: uuid.UUID | None = None) -> tuple[str, int]:
    s = get_settings()
    ttl = s.access_token_ttl_minutes * 60
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if session_id:
        payload["sid"] = str(session_id)
    return jwt.encode(payload, s.secret_key, algorithm=ALGORITHM), ttl


def decode_access_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ signing
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_payload(payload: dict[str, Any], ttl_seconds: int, bucket_seconds: int = 86400) -> str:
    """Compact signed token ``<b64 json>.<b64 hmac>`` used for image-proxy URLs.

    Expiry is rounded up to ``bucket_seconds`` so the same image yields the same
    URL for a whole bucket — otherwise client/CDN caches would never hit.
    """
    expires = int(time.time()) + ttl_seconds
    expires += -expires % bucket_seconds
    body = dict(payload, e=expires)
    raw = _b64(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
    sig = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).digest()[:16]
    return f"{raw}.{_b64(sig)}"


def verify_signed(token: str) -> dict[str, Any] | None:
    try:
        raw, sig = token.split(".", 1)
        expected = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expected, _unb64(sig)):
            return None
        body = json.loads(_unb64(raw))
    except (ValueError, json.JSONDecodeError):
        return None
    if body.get("e", 0) < time.time():
        return None
    return body
