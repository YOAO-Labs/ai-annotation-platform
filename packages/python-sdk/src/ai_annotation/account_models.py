"""Typed self-service account requests with opaque preference preservation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SecretStr,
    StrictStr,
    field_validator,
    model_validator,
)

from ai_annotation.models import _AAPModel
from ai_annotation.auth_models import _validate_password_strength


class _AccountRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class ProfileUpdate(_AccountRequest):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class AvatarRefUpdate(_AccountRequest):
    avatar_ref: StrictStr | None = None

    @field_validator("avatar_ref")
    @classmethod
    def client_owned_avatar_ref(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"preset:[a-z0-9-]{1,40}", value):
            raise ValueError("avatar_ref must be a preset reference or null")
        return value


class PasswordChange(_AccountRequest):
    old_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, value: SecretStr) -> SecretStr:
        _validate_password_strength(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def password_must_change(self) -> "PasswordChange":
        if self.old_password.get_secret_value() == self.new_password.get_secret_value():
            raise ValueError("new_password must differ from old_password")
        return self


class DeactivationRequest(_AccountRequest):
    reason: str | None = Field(default=None, max_length=500)


class UserPreferencesPatch(RootModel[dict[str, Any]]):
    """Opaque partial PATCH: preserve nulls, unknown keys and revision tokens."""


class TaskEventIn(_AccountRequest):
    task_id: UUID
    project_id: UUID
    kind: Literal["annotate", "review"]
    started_at: datetime
    ended_at: datetime
    duration_ms: int = Field(ge=0, le=4 * 60 * 60 * 1000)
    annotation_count: int = Field(default=0, ge=0, le=1_000_000)
    was_rejected: bool = False
    client_id: UUID | None = None
    collector_version: str | None = Field(default=None, max_length=32)
    collection_coverage: Literal["qualified", "partial"] = "qualified"

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_closed_interval(self) -> "TaskEventIn":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must be >= started_at")
        elapsed_ms = round((self.ended_at - self.started_at).total_seconds() * 1000)
        if abs(elapsed_ms - self.duration_ms) > 1:
            raise ValueError("duration_ms must match started_at and ended_at")
        return self


class TaskEventBatchRequest(_AccountRequest):
    events: list[TaskEventIn] = Field(min_length=1, max_length=200)


class TaskEventDiscarded(_AAPModel):
    index: int = Field(ge=0)
    client_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=64)


class TaskEventBatchResult(_AAPModel):
    accepted: int
    queued_async: bool
    discarded: list[TaskEventDiscarded] = Field(default_factory=list)
