from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: UUID
    type: str
    target_type: str
    target_id: UUID
    payload: Any = {}
    read_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationList(BaseModel):
    items: list[NotificationOut]
    total: int
    unread: int


class UnreadCount(BaseModel):
    unread: int
