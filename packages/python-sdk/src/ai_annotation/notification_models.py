"""Typed contracts for notifications and notification preferences."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation.models import _AAPModel


class Notification(_AAPModel):
    id: UUID
    type: str
    target_type: str
    target_id: UUID
    payload: Any = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime


class NotificationPage(_AAPModel):
    items: list[Notification]
    total: int
    unread: int


class NotificationUnreadCount(_AAPModel):
    unread: int


class NotificationPreference(_AAPModel):
    type: str
    in_app: bool = True
    email: bool = False
    toast: bool = False


class NotificationPreferences(_AAPModel):
    items: list[NotificationPreference]


class NotificationPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    in_app: bool | None = None
    toast: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_channels(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for channel in ("in_app", "toast"):
                if channel in data and data[channel] is None:
                    raise ValueError(f"{channel} must be a boolean, not null")
        return data

    @model_validator(mode="after")
    def require_channel_patch(self) -> NotificationPreferenceUpdate:
        if not {"in_app", "toast"}.intersection(self.model_fields_set):
            raise ValueError("at least one of in_app or toast is required")
        return self


class NotificationActionResult(_AAPModel):
    ok: bool


class NotificationMarkAllReadResult(_AAPModel):
    updated: int


class NotificationClearReadResult(_AAPModel):
    deleted: int
