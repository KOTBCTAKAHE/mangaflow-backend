from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.errors import ConflictError, UnauthorizedError
from ..core.logging import get_logger
from ..core.security import (
    create_access_token,
    hash_password,
    hash_token,
    needs_rehash,
    new_refresh_token,
    verify_password,
)
from ..models import RefreshSession, User
from ..repositories.users import SessionRepository, UserRepository

log = get_logger(__name__)

# Pre-computed hash so failed logins for unknown users cost the same time (no user enumeration by timing).
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session
        self.users = UserRepository(session)
        self.sessions = SessionRepository(session)

    async def register(self, *, email: str, username: str, password: str, display_name: str | None,
                       device_name: str | None, user_agent: str | None) -> tuple[User, dict]:
        if await self.users.email_taken(email):
            raise ConflictError("Email уже зарегистрирован", code="email_taken")
        if await self.users.username_taken(username):
            raise ConflictError("Имя пользователя занято", code="username_taken")
        user = await self.users.add(User(email=email.lower(), username=username, display_name=display_name,
                                         password_hash=hash_password(password), settings={}))
        tokens = await self._issue(user, device_name, user_agent, family_id=uuid.uuid4())
        log.info("user_registered", user_id=str(user.id))
        return user, tokens

    async def login(self, *, login: str, password: str, device_name: str | None,
                    user_agent: str | None) -> tuple[User, dict]:
        user = await self.users.by_login(login)
        if user is None:
            verify_password(password, _DUMMY_HASH)
            raise UnauthorizedError("Неверный логин или пароль", code="invalid_credentials")
        if not verify_password(password, user.password_hash) or not user.is_active:
            raise UnauthorizedError("Неверный логин или пароль", code="invalid_credentials")
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        return user, await self._issue(user, device_name, user_agent, family_id=uuid.uuid4())

    async def refresh(self, refresh_token: str, user_agent: str | None) -> tuple[User, dict]:
        rs = await self.sessions.by_hash(hash_token(refresh_token))
        now = datetime.now(UTC)
        if rs is None:
            raise UnauthorizedError("Сессия недействительна", code="invalid_refresh_token")
        if rs.revoked_at is not None:
            grace = timedelta(seconds=get_settings().refresh_reuse_grace_seconds)
            if now - _aware(rs.revoked_at) <= grace and await self.sessions.family_active(rs.family_id):
                # Just rotated and the session is alive (not logged out): the same app
                # refreshed twice at once (parallel requests, a background job). Not theft —
                # hand out another pair in the same family instead of logging the user out.
                user = await self.users.get(rs.user_id)
                if user is None or not user.is_active:
                    raise UnauthorizedError("Пользователь не найден", code="invalid_refresh_token")
                log.info("refresh_token_race", user_id=str(rs.user_id))
                return user, await self._issue(user, rs.device_name, user_agent, family_id=rs.family_id)
            # Reuse of a rotated token long after rotation → likely theft. Kill the whole family.
            await self.sessions.revoke_family(rs.family_id)
            log.warning("refresh_token_reuse", user_id=str(rs.user_id))
            raise UnauthorizedError("Сессия отозвана, войдите снова", code="refresh_token_reused")
        if _aware(rs.expires_at) <= now:
            raise UnauthorizedError("Сессия истекла, войдите снова", code="refresh_token_expired")
        user = await self.users.get(rs.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Пользователь не найден", code="invalid_refresh_token")
        rs.revoked_at = now
        return user, await self._issue(user, rs.device_name, user_agent, family_id=rs.family_id)

    async def logout(self, refresh_token: str) -> None:
        rs = await self.sessions.by_hash(hash_token(refresh_token))
        if rs is not None:
            await self.sessions.revoke_family(rs.family_id)

    async def logout_all(self, user_id: uuid.UUID) -> None:
        await self.sessions.revoke_all(user_id)

    async def _issue(self, user: User, device_name: str | None, user_agent: str | None,
                     *, family_id: uuid.UUID) -> dict:
        s = get_settings()
        token = new_refresh_token()
        now = datetime.now(UTC)
        rs = await self.sessions.add(RefreshSession(
            user_id=user.id, family_id=family_id, token_hash=hash_token(token),
            device_name=(device_name or "")[:128] or None, user_agent=(user_agent or "")[:512] or None,
            created_at=now, last_used_at=now, expires_at=now + timedelta(days=s.refresh_token_ttl_days),
        ))
        access, ttl = create_access_token(user.id, session_id=rs.family_id)
        return {"access_token": access, "refresh_token": token, "token_type": "bearer", "expires_in": ttl}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
