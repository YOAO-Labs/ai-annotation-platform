"""System configuration readback and versioned override requests."""

from __future__ import annotations

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

from ai_annotation.models import _AAPModel


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
    )


class SmtpStatus(_AAPModel):
    host: str | None
    port: int | None
    user: str | None
    from_address: str | None
    password_set: bool = False
    configured: bool


class SystemSettingMetadata(_AAPModel):
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


class SystemSettingsOut(_AAPModel):
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


class SystemSettingsUpdate(_RequestModel):
    allow_open_registration: StrictBool | None = None
    invitation_ttl_days: StrictInt | None = Field(default=None, ge=1, le=90)
    frontend_base_url: StrictStr | None = Field(default=None, max_length=255)
    smtp_host: StrictStr | None = Field(default=None, max_length=255)
    smtp_port: StrictInt | None = Field(default=None, ge=1, le=65535)
    smtp_user: StrictStr | None = Field(default=None, max_length=255)
    smtp_password: StrictStr | None = Field(default=None, max_length=255, repr=False)
    smtp_from: StrictStr | None = Field(default=None, max_length=255)
    max_invitations_per_day: StrictInt | None = Field(default=None, ge=1, le=1000)
    offline_threshold_minutes: StrictInt | None = Field(default=None, ge=2, le=60)
    dataset_import_max_files: StrictInt | None = Field(default=None, ge=1)
    dataset_import_max_total_bytes: StrictInt | None = Field(default=None, ge=1)
    task_create_sync_threshold: StrictInt | None = Field(default=None, ge=0)
    video_chunk_warmup_lookahead: StrictInt | None = Field(default=None, ge=0)
    expected_version: StrictStr | None = None


class SystemSettingsReset(_RequestModel):
    keys: list[StrictStr] = Field(min_length=1)
    expected_version: StrictStr | None = None

    @field_validator("keys")
    @classmethod
    def _unique_keys(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("keys must be unique")
        return values


class SmtpTestResult(_AAPModel):
    ok: bool
    to: str
    from_address: str = Field(alias="from")
    host: str
    port: int
