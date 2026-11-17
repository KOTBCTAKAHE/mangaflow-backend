from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict


def _aware(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=UTC)


AwareDatetime = Annotated[datetime, AfterValidator(_aware)]

T = TypeVar("T")


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page(Schema, Generic[T]):
    items: list[T]
    page: int = 1
    has_next: bool = False
    total: int | None = None


class SyncResponse(Schema, Generic[T]):
    items: list[T]
    server_time: datetime


class Message(Schema):
    message: str
