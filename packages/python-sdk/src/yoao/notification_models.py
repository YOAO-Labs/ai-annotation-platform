"""Typed contracts for notifications, preferences, and external notification channels."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from yoao.models import _SDKModel


class Notification(_SDKModel):
    id: UUID
    type: str
    target_type: str
    target_id: UUID
    payload: Any = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime


class NotificationPage(_SDKModel):
    items: list[Notification]
    total: int
    unread: int


class NotificationUnreadCount(_SDKModel):
    unread: int


class NotificationPreference(_SDKModel):
    type: str
    in_app: bool = True
    email: bool = False
    toast: bool = False
    external_channels: dict[str, bool] = Field(default_factory=dict)


class NotificationPreferences(_SDKModel):
    items: list[NotificationPreference]


class NotificationPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    in_app: bool | None = None
    toast: bool | None = None
    external_channels: dict[str, bool] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_channels(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for channel in ("in_app", "toast"):
                if channel in data and data[channel] is None:
                    raise ValueError(f"{channel} must be a boolean, not null")
            if "external_channels" in data:
                ext = data["external_channels"]
                if ext is None or not isinstance(ext, dict):
                    raise ValueError(
                        "external_channels must be a channel->boolean map, not null"
                    )
                for channel_id, enabled in ext.items():
                    if not isinstance(channel_id, str) or type(enabled) is not bool:
                        raise ValueError("external_channels values must be booleans")
        return data

    @model_validator(mode="after")
    def require_channel_patch(self) -> NotificationPreferenceUpdate:
        has_builtin = bool({"in_app", "toast"}.intersection(self.model_fields_set))
        has_external = bool(self.external_channels)
        if not has_builtin and not has_external:
            raise ValueError(
                "at least one of in_app, toast, or external_channels is required"
            )
        return self


class NotificationActionResult(_SDKModel):
    ok: bool


class NotificationMarkAllReadResult(_SDKModel):
    updated: int


class NotificationClearReadResult(_SDKModel):
    deleted: int


class NotificationChannelAdmin(_SDKModel):
    id: str
    channel_id: str
    display_name: str
    description: str | None = None
    homepage: str | None = None
    source_tier: Literal["official", "third_party"]
    mode: Literal["direct", "managed"]
    status: Literal["prepared", "active", "disabled", "retired"]
    revision: int
    endpoint_url: str
    auth_token_sha256: str
    protocol_version: str
    capabilities: list[str]
    address_schema: dict[str, Any]
    manifest: dict[str, Any] | None = None
    rate_limit_per_minute: int
    consecutive_auth_errors: int
    created_at: str
    updated_at: str
    disabled_at: str | None = None
    retired_at: str | None = None


class NotificationChannelAdminList(_SDKModel):
    items: list[NotificationChannelAdmin]


class NotificationChannelRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_url: StrictStr = Field(min_length=1, max_length=2048)
    auth_token: StrictStr = Field(min_length=1, max_length=4096)
    source_tier: Literal["official", "third_party"] = "third_party"
    mode: Literal["direct", "managed"] = "direct"
    status: Literal["prepared", "active"] = "active"
    rate_limit_per_minute: StrictInt = Field(default=60, ge=1, le=600)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    manifest: dict[str, Any] | None = None


class NotificationChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    source_tier: Literal["official", "third_party"] | None = None
    endpoint_url: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    auth_token: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    rate_limit_per_minute: StrictInt | None = Field(default=None, ge=1, le=600)


class NotificationChannelProbeResult(_SDKModel):
    healthy: bool
    health_detail: dict[str, Any] | None = None
    setup_ok: bool
    protocol_version: str | None = None
    channel_id: str | None = None
    display_name: str | None = None
    capabilities: list[str] | None = None
    error: str | None = None


class UserNotificationChannel(_SDKModel):
    channel_id: str
    display_name: str
    description: str | None = None
    homepage: str | None = None
    source_tier: Literal["official", "third_party"]
    status: Literal["active", "disabled"]
    capabilities: list[str]
    address_schema: dict[str, Any]
    bound: bool
    address: dict[str, Any] | None = None
    locale: str = "zh-CN"
    verified_at: str | None = None
    updated_at: str | None = None


class UserNotificationChannelList(_SDKModel):
    items: list[UserNotificationChannel]


class UserChannelBindingUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: dict[str, Any]
    locale: StrictStr = "zh-CN"


class UserNotificationChannelBinding(_SDKModel):
    channel_id: str
    address: dict[str, Any]
    locale: str
    verified_at: str | None = None
    updated_at: str


class VerifyChannelAddressResult(_SDKModel):
    ok: bool = True
    verified_at: str | None = None
    provider_message_id: str | None = None


class DeleteChannelBindingResult(_SDKModel):
    ok: bool = True
    deleted: bool
