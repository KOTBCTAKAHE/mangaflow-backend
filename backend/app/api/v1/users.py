from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse

from ...core.errors import AppError, ConflictError, NotFoundError
from ...core.security import hash_password, verify_password
from ...repositories.users import SessionRepository, UserRepository
from ...schemas.auth import PasswordChangeIn, SessionOut, SettingsIn, SettingsOut, UserOut, UserUpdateIn
from ...schemas.common import Message
from ...schemas.reading import StatsOut
from ...services.backup import BackupIn, export_backup, import_backup
from ...services.stats import compute_stats
from ...services.sync import aware
from ..deps import CurrentUser, SessionDep

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> Any:
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(body: UserUpdateIn, user: CurrentUser, session: SessionDep) -> Any:
    if body.username and body.username != user.username:
        if await UserRepository(session).username_taken(body.username, exclude=user.id):
            raise ConflictError("Имя пользователя занято", code="username_taken")
        user.username = body.username
    if body.display_name is not None:
        user.display_name = body.display_name or None
    return user


@router.post("/me/password", response_model=Message)
async def change_password(body: PasswordChangeIn, user: CurrentUser, session: SessionDep) -> Message:
    if not verify_password(body.current_password, user.password_hash):
        raise AppError("Текущий пароль неверен", code="invalid_password", status_code=400)
    user.password_hash = hash_password(body.new_password)
    await SessionRepository(session).revoke_all(user.id)
    return Message(message="Пароль изменён, остальные сессии завершены")


@router.get("/me/settings", response_model=SettingsOut)
async def get_settings_(user: CurrentUser) -> SettingsOut:
    return SettingsOut(settings=user.settings or {}, updated_at=aware(user.settings_updated_at))


@router.put("/me/settings", response_model=SettingsOut)
async def put_settings(body: SettingsIn, user: CurrentUser) -> SettingsOut:
    if body.updated_at > aware(user.settings_updated_at):
        user.settings = body.settings
        user.settings_updated_at = body.updated_at
    return SettingsOut(settings=user.settings or {}, updated_at=aware(user.settings_updated_at))


@router.get("/me/sessions", response_model=list[SessionOut])
async def sessions(user: CurrentUser, session: SessionDep, request: Request) -> list[SessionOut]:
    current = getattr(request.state, "session_family", None)
    return [SessionOut(id=s.id, device_name=s.device_name, user_agent=s.user_agent, created_at=s.created_at,
                       last_used_at=s.last_used_at, current=str(s.family_id) == current)
            for s in await SessionRepository(session).active_for_user(user.id)]


@router.delete("/me/sessions/{session_id}", response_model=Message)
async def revoke_session(session_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Message:
    from ...models import RefreshSession

    rs = await session.get(RefreshSession, session_id)
    if rs is None or rs.user_id != user.id:
        raise NotFoundError("Сессия не найдена")
    await SessionRepository(session).revoke_family(rs.family_id)
    return Message(message="ok")


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(user: CurrentUser, session: SessionDep, password: str = Query(..., min_length=1)) -> None:
    if not verify_password(password, user.password_hash):
        raise AppError("Неверный пароль", code="invalid_password", status_code=400)
    await UserRepository(session).delete(user)


@router.get("/me/stats", response_model=StatsOut)
async def stats(user: CurrentUser, session: SessionDep, days: int = Query(365, ge=7, le=1095)) -> Any:
    return await compute_stats(session, user.id, days=days)


@router.get("/me/export")
async def export(user: CurrentUser, session: SessionDep) -> JSONResponse:
    data = await export_backup(session, user)
    name = f"mangaflow-backup-{datetime.now(UTC):%Y%m%d-%H%M}.json"
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/me/import")
async def import_(body: BackupIn, user: CurrentUser, session: SessionDep) -> dict[str, int]:
    if body.format != "mangaflow-backup":
        raise AppError("Неизвестный формат резервной копии", code="invalid_backup", status_code=422)
    return await import_backup(session, user, body)
