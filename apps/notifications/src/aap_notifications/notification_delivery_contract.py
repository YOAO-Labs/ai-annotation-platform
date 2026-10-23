"""Closed protocol for the isolated Notifications Redis publisher."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

NOTIFICATION_DELIVERY_PROTOCOL = "aap-notifications-delivery/v1"
NOTIFICATION_DELIVERY_POLICY_PROTOCOL = "aap-notifications-delivery-policy/v1"
NOTIFICATION_DELIVERY_POLICY = "aap-notifications-delivery/v1"
NOTIFICATION_TYPES = (
    "user.deactivation_requested",
    "user.deactivation_completed",
)


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class NotificationDeliveryUnavailable(RuntimeError):
    """The durable notification publisher owner did not return a valid reply."""


def _reject_protocol_alias(value: object) -> None:
    if isinstance(value, dict) and "protocol" in value:
        raise ValueError("Use only the declared wire field schema")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def _assert_finite(value: object) -> None:
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_finite(key)
            _assert_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_finite(item)


def _json_bytes(payload: dict | bytes | str | ClosedModel) -> bytes:
    if isinstance(payload, (bytes, str)):
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        parsed = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_protocol_alias(parsed)
        _assert_finite(parsed)
        return raw
    if isinstance(payload, ClosedModel):
        value = payload.model_dump(mode="json", by_alias=True)
    elif isinstance(payload, dict):
        value = payload
    else:
        raise ValueError("Expected a JSON object")
    _reject_protocol_alias(value)
    _assert_finite(value)
    return json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _decode(adapter: TypeAdapter, payload: dict | bytes | str):
    try:
        return adapter.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise NotificationDeliveryUnavailable() from None


def _decode_success(adapter: TypeAdapter, payload: dict | bytes | str):
    try:
        raw = _json_bytes(payload)
        envelope = json.loads(raw)
        if (
            not isinstance(envelope, dict)
            or type(envelope.get("ok")) is not bool
            or envelope["ok"] is not True
        ):
            raise ValueError("Invalid successful notification delivery reply")
        return adapter.validate_json(raw)
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise NotificationDeliveryUnavailable() from None


class NotificationDeliveryPolicyReply(ClosedModel):
    protocol: Literal["aap-notifications-delivery-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-notifications-delivery/v1"]


class NotificationClaimRequest(ClosedModel):
    protocol: Literal["aap-notifications-delivery/v1"] = Field(alias="schema")
    worker_id: uuid.UUID
    limit: int = Field(ge=1, le=100, strict=True)
    lease_seconds: int = Field(ge=1, le=300, strict=True)


class NotificationFinishRequest(ClosedModel):
    protocol: Literal["aap-notifications-delivery/v1"] = Field(alias="schema")
    notification_id: uuid.UUID
    worker_id: uuid.UUID
    lease_id: uuid.UUID
    outcome: Literal["delivered", "retry"]
    reason: Literal["published", "redis_error"]

    @model_validator(mode="after")
    def outcome_reason_pair(self) -> NotificationFinishRequest:
        if (self.outcome == "delivered") != (self.reason == "published"):
            raise ValueError("Notification delivery outcome and reason disagree")
        return self


class NotificationRequestedPayload(ClosedModel):
    email: str = Field(max_length=255, strict=True, repr=False)
    name: str = Field(max_length=100, strict=True, repr=False)
    role: Literal["super_admin", "project_admin", "employee", "viewer"]
    scheduled_at: datetime

    @field_validator("scheduled_at")
    @classmethod
    def aware_scheduled_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Notification timestamps must include a timezone")
        try:
            return value.astimezone(UTC)
        except (OverflowError, OSError, ValueError):
            raise ValueError(
                "Notification timestamp is outside the supported range"
            ) from None


class NotificationCompletedPayload(ClosedModel):
    email: str = Field(max_length=255, strict=True, repr=False)
    name: str = Field(max_length=100, strict=True, repr=False)
    auto: Literal[True]

    @field_validator("auto", mode="before")
    @classmethod
    def strict_auto(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Completed notification must have boolean auto=true")
        return value


class _NotificationLeaseBase(ClosedModel):
    id: uuid.UUID
    user_id: uuid.UUID
    target_type: Literal["user"]
    target_id: uuid.UUID
    created_at: datetime
    worker_id: uuid.UUID
    lease_id: uuid.UUID
    lease_expires_at: datetime

    @field_validator("created_at", "lease_expires_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Notification timestamps must include a timezone")
        try:
            return value.astimezone(UTC)
        except (OverflowError, OSError, ValueError):
            raise ValueError(
                "Notification timestamp is outside the supported range"
            ) from None


class NotificationRequestedLease(_NotificationLeaseBase):
    type: Literal["user.deactivation_requested"]
    payload: NotificationRequestedPayload = Field(repr=False)


class NotificationCompletedLease(_NotificationLeaseBase):
    type: Literal["user.deactivation_completed"]
    payload: NotificationCompletedPayload = Field(repr=False)


NotificationLease: TypeAlias = Annotated[
    NotificationRequestedLease | NotificationCompletedLease,
    Field(discriminator="type"),
]


class NotificationClaimData(ClosedModel):
    items: list[NotificationLease] = Field(max_length=100)


class NotificationClaimReply(ClosedModel):
    protocol: Literal["aap-notifications-delivery/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: NotificationClaimData

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Successful notification claim requires boolean true")
        return value


class NotificationFinishData(ClosedModel):
    accepted: bool

    @field_validator("accepted", mode="before")
    @classmethod
    def strict_accepted(cls, value):
        if type(value) is not bool:
            raise ValueError("Notification lease acceptance must be a boolean")
        return value


class NotificationFinishReply(ClosedModel):
    protocol: Literal["aap-notifications-delivery/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: NotificationFinishData

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Successful notification finish requires boolean true")
        return value


_POLICY = TypeAdapter(NotificationDeliveryPolicyReply)
_CLAIM_REQUEST = TypeAdapter(NotificationClaimRequest)
_CLAIM_REPLY = TypeAdapter(NotificationClaimReply)
_FINISH_REQUEST = TypeAdapter(NotificationFinishRequest)
_FINISH_REPLY = TypeAdapter(NotificationFinishReply)
_SCHEMA_ADAPTER = TypeAdapter(
    NotificationDeliveryPolicyReply
    | NotificationClaimRequest
    | NotificationClaimReply
    | NotificationFinishRequest
    | NotificationFinishReply
)


def notification_delivery_protocol_schema() -> dict:
    schema = _SCHEMA_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = NOTIFICATION_DELIVERY_PROTOCOL
    return schema


def decode_notification_delivery_policy(
    payload: dict | bytes | str,
) -> NotificationDeliveryPolicyReply:
    return _decode(_POLICY, payload)


def decode_notification_claim_request(
    payload: dict | bytes | str,
) -> NotificationClaimRequest:
    return _decode(_CLAIM_REQUEST, payload)


def decode_notification_finish_request(
    payload: dict | bytes | str,
) -> NotificationFinishRequest:
    return _decode(_FINISH_REQUEST, payload)


def decode_notification_claim_reply(
    payload: dict | bytes | str,
) -> NotificationClaimReply:
    return _decode_success(_CLAIM_REPLY, payload)


def decode_notification_finish_reply(
    payload: dict | bytes | str,
) -> NotificationFinishReply:
    return _decode_success(_FINISH_REPLY, payload)


def native_notification_command_json(command: ClosedModel) -> str:
    try:
        return _json_bytes(command).decode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise NotificationDeliveryUnavailable() from None
