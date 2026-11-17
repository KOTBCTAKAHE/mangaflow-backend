from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from ...core.config import get_settings
from ...core.ratelimit import RateLimit
from ...schemas.auth import AuthOut, LoginIn, RefreshIn, RegisterIn, TokenPair, UserOut
from ...schemas.common import Message
from ...services.auth import AuthService
from ..deps import CurrentUser, SessionDep

router = APIRouter(prefix="/auth", tags=["auth"],
                   dependencies=[Depends(RateLimit("auth", get_settings().rate_limit_auth))])


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, request: Request, session: SessionDep) -> AuthOut:
    user, tokens = await AuthService(session).register(
        email=body.email, username=body.username, password=body.password, display_name=body.display_name,
        device_name=body.device_name, user_agent=request.headers.get("user-agent"))
    return AuthOut(user=UserOut.model_validate(user), tokens=TokenPair(**tokens))


@router.post("/login", response_model=AuthOut)
async def login(body: LoginIn, request: Request, session: SessionDep) -> AuthOut:
    user, tokens = await AuthService(session).login(
        login=body.login, password=body.password, device_name=body.device_name,
        user_agent=request.headers.get("user-agent"))
    return AuthOut(user=UserOut.model_validate(user), tokens=TokenPair(**tokens))


@router.post("/refresh", response_model=AuthOut)
async def refresh(body: RefreshIn, request: Request, session: SessionDep) -> AuthOut:
    svc = AuthService(session)
    try:
        user, tokens = await svc.refresh(body.refresh_token, request.headers.get("user-agent"))
    except Exception:
        await session.commit()  # persist family revocation on token reuse
        raise
    return AuthOut(user=UserOut.model_validate(user), tokens=TokenPair(**tokens))


@router.post("/logout", response_model=Message)
async def logout(body: RefreshIn, session: SessionDep) -> Message:
    await AuthService(session).logout(body.refresh_token)
    return Message(message="ok")


@router.post("/logout-all", response_model=Message)
async def logout_all(user: CurrentUser, session: SessionDep) -> Message:
    await AuthService(session).logout_all(user.id)
    return Message(message="ok")
