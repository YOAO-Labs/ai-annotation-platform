from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)
from app.core.password import validate_password_strength


class ProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class AvatarRefUpdate(BaseModel):
    """头像引用写入契约（``PATCH /auth/me/avatar``）。

    ``avatar_ref`` 只接受 ``preset:<slug>`` 或 null（= 恢复首字母）；``upload:`` 由上传
    端点独占写入，客户端提交会被拒。省略字段等价于清空。
    """

    model_config = ConfigDict(extra="forbid")

    avatar_ref: StrictStr | None = None


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        errors = validate_password_strength(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


class SmtpStatus(BaseModel):
    host: str | None
    port: int | None
    user: str | None
    from_address: str | None
    password_set: bool = False
    configured: bool


class SystemSettingMetadata(BaseModel):
    """Non-secret provenance and validation metadata for one system setting."""

    source: Literal["deployment", "override"]
    deployment_default: Any
    updated_at: datetime | None = None
    updated_by: str | None = None
    value_type: Literal["bool", "int", "str", "json"]
    unit: str | None = None
    effect: str
    min_value: int | None = None
    max_value: int | None = None
    in_range: bool


class SystemSettingsOut(BaseModel):
    environment: str
    invitation_ttl_days: int
    frontend_base_url: str
    smtp: SmtpStatus
    allow_open_registration: bool
    max_invitations_per_day: int
    offline_threshold_minutes: int
    dataset_import_max_files: int
    dataset_import_max_total_bytes: int
    task_create_sync_threshold: int
    video_chunk_warmup_lookahead: int
    version: str
    metadata: dict[str, SystemSettingMetadata]


class SystemSettingsUpdate(BaseModel):
    """v0.8.1 · admin UI PATCH 入参，全部 Optional：未提供字段不变更。
    smtp_password 传空串视为清除；未提供则不动现有值。
    """

    model_config = ConfigDict(extra="forbid")

    allow_open_registration: StrictBool | None = None
    invitation_ttl_days: StrictInt | None = Field(default=None, ge=1, le=90)
    frontend_base_url: StrictStr | None = Field(default=None, max_length=255)
    smtp_host: StrictStr | None = Field(default=None, max_length=255)
    smtp_port: StrictInt | None = Field(default=None, ge=1, le=65535)
    smtp_user: StrictStr | None = Field(default=None, max_length=255)
    smtp_password: StrictStr | None = Field(default=None, max_length=255)
    smtp_from: StrictStr | None = Field(default=None, max_length=255)
    max_invitations_per_day: StrictInt | None = Field(default=None, ge=1, le=1000)
    offline_threshold_minutes: StrictInt | None = Field(default=None, ge=2, le=60)
    dataset_import_max_files: StrictInt | None = Field(default=None, ge=1)
    dataset_import_max_total_bytes: StrictInt | None = Field(default=None, ge=1)
    task_create_sync_threshold: StrictInt | None = Field(default=None, ge=0)
    video_chunk_warmup_lookahead: StrictInt | None = Field(default=None, ge=0)
    expected_version: StrictStr | None = None


class SystemSettingsReset(BaseModel):
    """Explicitly remove DB overrides; PATCH null never means reset."""

    model_config = ConfigDict(extra="forbid")

    keys: list[StrictStr] = Field(min_length=1)
    expected_version: StrictStr | None = None

    @field_validator("keys")
    @classmethod
    def _unique_keys(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("keys 不得重复")
        return values
