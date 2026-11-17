from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RefreshSession, User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.s.get(User, user_id)

    async def by_login(self, login: str) -> User | None:
        login = login.strip().lower()
        stmt = select(User).where(or_(func.lower(User.email) == login, func.lower(User.username) == login))
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def email_taken(self, email: str) -> bool:
        stmt = select(User.id).where(func.lower(User.email) == email.lower())
        return (await self.s.execute(stmt)).first() is not None

    async def username_taken(self, username: str, exclude: uuid.UUID | None = None) -> bool:
        stmt = select(User.id).where(func.lower(User.username) == username.lower())
        if exclude:
            stmt = stmt.where(User.id != exclude)
        return (await self.s.execute(stmt)).first() is not None

    async def add(self, user: User) -> User:
        self.s.add(user)
        await self.s.flush()
        return user

    async def delete(self, user: User) -> None:
        await self.s.delete(user)


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(self, rs: RefreshSession) -> RefreshSession:
        self.s.add(rs)
        await self.s.flush()
        return rs

    async def by_hash(self, token_hash: str) -> RefreshSession | None:
        stmt = select(RefreshSession).where(RefreshSession.token_hash == token_hash)
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        await self.s.execute(
            update(RefreshSession)
            .where(RefreshSession.family_id == family_id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_all(self, user_id: uuid.UUID) -> None:
        await self.s.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def active_for_user(self, user_id: uuid.UUID) -> list[RefreshSession]:
        now = datetime.now(UTC)
        stmt = (
            select(RefreshSession)
            .where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None),
                   RefreshSession.expires_at > now)
            .order_by(RefreshSession.last_used_at.desc())
        )
        return list((await self.s.execute(stmt)).scalars())
