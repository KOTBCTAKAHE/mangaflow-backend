from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.errors import UnauthorizedError
from ..core.security import decode_access_token
from ..db.session import get_session
from ..models import User
from ..services.images import ImageSigner
from ..services.presenter import Presenter
from ..services.search import SearchService
from ..services.sources import SourceService
from ..services.works import WorkService

bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def optional_user(
    request: Request,
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User | None:
    if creds is None:
        return None
    try:
        payload = decode_access_token(creds.credentials)
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise UnauthorizedError("Токен недействителен или истёк", code="invalid_token") from None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Пользователь не найден", code="invalid_token")
    request.state.user_id = str(user.id)
    request.state.session_family = payload.get("sid")
    return user


async def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise UnauthorizedError("Требуется авторизация", code="auth_required")
    return user


OptionalUser = Annotated[User | None, Depends(optional_user)]
CurrentUser = Annotated[User, Depends(current_user)]


def source_service(request: Request) -> SourceService:
    return request.app.state.sources


def search_service(request: Request) -> SearchService:
    return request.app.state.search


def work_service(request: Request) -> WorkService:
    return request.app.state.works


def presenter(request: Request) -> Presenter:
    base = get_settings().public_base_url or str(request.base_url)
    return Presenter(ImageSigner(base, get_settings()))


SourcesDep = Annotated[SourceService, Depends(source_service)]
SearchDep = Annotated[SearchService, Depends(search_service)]
WorksDep = Annotated[WorkService, Depends(work_service)]
PresenterDep = Annotated[Presenter, Depends(presenter)]
