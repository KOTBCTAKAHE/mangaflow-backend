from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import EmailStr, Field, field_validator

from .common import AwareDatetime, Schema


class RegisterIn(Schema):
    email: EmailStr
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.\-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    device_name: str | None = Field(default=None, max_length=128)

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: str) -> str:
        if v.isdigit() or v.isalpha():
            raise ValueError("пароль должен содержать буквы и цифры")
        return v


class LoginIn(Schema):
    login: str = Field(min_length=3, max_length=320, description="email или username")
    password: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=128)


class RefreshIn(Schema):
    refresh_token: str = Field(min_length=20, max_length=256)


class TokenPair(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(Schema):
    id: uuid.UUID
    email: str
    username: str
    display_name: str | None
    created_at: datetime


class AuthOut(Schema):
    user: UserOut
    tokens: TokenPair


class UserUpdateIn(Schema):
    display_name: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.\-]+$")


class PasswordChangeIn(Schema):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class SettingsIn(Schema):
    settings: dict[str, Any]
    updated_at: AwareDatetime


class SettingsOut(Schema):
    settings: dict[str, Any]
    updated_at: datetime


class SessionOut(Schema):
    id: uuid.UUID
    device_name: str | None
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime
    current: bool = False
