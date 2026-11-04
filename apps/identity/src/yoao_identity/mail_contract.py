"""Closed protocol models for the durable credential-mail worker."""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .contract import ClosedModel, IdentityUnavailable, _reject_protocol_alias

MAIL_NATIVE_PROTOCOL = "aap-identity-recovery-mail-native/v1"
MAIL_POLICY_PROTOCOL = "aap-identity-recovery-mail-policy/v1"
MAIL_POLICY = "aap-identity-recovery-mail/v1"
MAIL_DELIVERY_POLICY_FIELDS = (
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
)
MAIL_OUTCOMES = ("delivered", "rejected", "retry", "unknown", "invalid_key")
MAIL_REASONS = (
    "sent",
    "not_configured",
    "message_invalid",
    "tls_failed",
    "auth_failed",
    "recipient_rejected",
    "sender_rejected",
    "data_rejected",
    "connect_failed",
    "transport_failed",
    "derivation_unavailable",
)

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
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


def _json_bytes(payload: dict | bytes | str) -> bytes:
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
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    _reject_protocol_alias(payload)
    _assert_finite(payload)
    return json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _decode(adapter: TypeAdapter, payload: dict | bytes | str):
    try:
        return adapter.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def _decode_success(adapter: TypeAdapter, payload: dict | bytes | str):
    try:
        raw = _json_bytes(payload)
        envelope = json.loads(raw)
        if (
            not isinstance(envelope, dict)
            or type(envelope.get("ok")) is not bool
            or envelope["ok"] is not True
        ):
            raise ValueError("Invalid successful mail envelope")
        return adapter.validate_json(raw)
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


class MailPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-recovery-mail-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-recovery-mail/v1"]


class MailDeliveryPolicy(ClosedModel):
    smtp_host: str = Field(strict=True)
    smtp_port: int = Field(ge=0, le=65535, strict=True)
    smtp_user: str = Field(strict=True, repr=False)
    smtp_password: str = Field(strict=True, repr=False)
    smtp_from: str = Field(strict=True)

    @field_validator("smtp_host", "smtp_user", "smtp_from")
    @classmethod
    def reject_header_controls(cls, value: str) -> str:
        if "\r" in value or "\n" in value or "\x00" in value:
            raise ValueError("Invalid mail policy text")
        return value


class MailClaimRequest(ClosedModel):
    protocol: Literal["aap-identity-recovery-mail-native/v1"] = Field(alias="schema")
    worker_id: uuid.UUID
    lease_seconds: int = Field(ge=1, le=300, strict=True)
    delivery_id: uuid.UUID | None


class MailRenewRequest(ClosedModel):
    protocol: Literal["aap-identity-recovery-mail-native/v1"] = Field(alias="schema")
    delivery_id: uuid.UUID
    worker_id: uuid.UUID
    lease_token: str = Field(min_length=1, max_length=256, strict=True, repr=False)


MailOutcome = Literal["delivered", "rejected", "retry", "unknown", "invalid_key"]
MailReason = Literal[
    "sent",
    "not_configured",
    "message_invalid",
    "tls_failed",
    "auth_failed",
    "recipient_rejected",
    "sender_rejected",
    "data_rejected",
    "connect_failed",
    "transport_failed",
    "derivation_unavailable",
]


class MailFinishRequest(ClosedModel):
    protocol: Literal["aap-identity-recovery-mail-native/v1"] = Field(alias="schema")
    delivery_id: uuid.UUID
    worker_id: uuid.UUID
    lease_token: str = Field(min_length=1, max_length=256, strict=True, repr=False)
    outcome: MailOutcome
    reason: MailReason
    retry_delay_seconds: int | None = Field(default=None, ge=1, le=300, strict=True)

    @model_validator(mode="after")
    def retry_delay_shape(self) -> MailFinishRequest:
        retryable = self.outcome in {"retry", "unknown", "invalid_key"}
        if retryable != (self.retry_delay_seconds is not None):
            raise ValueError("Retryable mail outcomes require a bounded retry delay")
        if self.outcome == "delivered" and self.reason != "sent":
            raise ValueError("Delivered mail must use the sent reason")
        if self.outcome == "invalid_key" and self.reason != "derivation_unavailable":
            raise ValueError("Invalid mail key must use the safe derivation reason")
        return self


class MailLease(ClosedModel):
    id: uuid.UUID
    actor_id: uuid.UUID
    purpose: Literal["password_reset", "email_verification"]
    email: str = Field(min_length=3, max_length=255, strict=True)
    frontend_base_url: str = Field(min_length=1, strict=True, repr=False)
    token_hash: str = Field(pattern=_HEX_64, strict=True, repr=False)
    token_ciphertext: str = Field(
        min_length=1, max_length=4096, strict=True, repr=False
    )
    encryption_id: str = Field(pattern=_HEX_64, strict=True, repr=False)
    expires_at: datetime
    attempt: int = Field(ge=1, lt=2**63, strict=True)
    worker_id: uuid.UUID
    lease_token: str = Field(min_length=1, max_length=256, strict=True, repr=False)
    lease_expires_at: datetime

    @field_validator("expires_at", "lease_expires_at")
    @classmethod
    def aware_datetime(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Mail timestamps must include a timezone")
        try:
            return value.astimezone(UTC)
        except (OverflowError, OSError, ValueError):
            raise ValueError("Mail timestamp is outside the supported range") from None


class MailClaimData(ClosedModel):
    delivery: MailLease | None


class MailClaimReply(ClosedModel):
    protocol: Literal["aap-identity-recovery-mail-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: MailClaimData

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Successful mail response requires boolean true")
        return value


class MailAcceptedData(ClosedModel):
    accepted: bool

    @field_validator("accepted", mode="before")
    @classmethod
    def strict_bool(cls, value):
        if type(value) is not bool:
            raise ValueError("Mail acceptance must be a boolean")
        return value


class MailAcceptedReply(ClosedModel):
    protocol: Literal["aap-identity-recovery-mail-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: MailAcceptedData

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Successful mail response requires boolean true")
        return value


_POLICY = TypeAdapter(MailPolicyReply)
_DELIVERY_POLICY = TypeAdapter(MailDeliveryPolicy)
_CLAIM_REQUEST = TypeAdapter(MailClaimRequest)
_RENEW_REQUEST = TypeAdapter(MailRenewRequest)
_FINISH_REQUEST = TypeAdapter(MailFinishRequest)
_CLAIM_REPLY = TypeAdapter(MailClaimReply)
_ACCEPTED_REPLY = TypeAdapter(MailAcceptedReply)


def decode_mail_policy(payload: dict | bytes | str) -> MailPolicyReply:
    return _decode(_POLICY, payload)


def decode_mail_delivery_policy(payload: dict | bytes | str) -> MailDeliveryPolicy:
    return _decode(_DELIVERY_POLICY, payload)


def decode_mail_claim_request(payload: dict | bytes | str) -> MailClaimRequest:
    return _decode(_CLAIM_REQUEST, payload)


def decode_mail_renew_request(payload: dict | bytes | str) -> MailRenewRequest:
    return _decode(_RENEW_REQUEST, payload)


def decode_mail_finish_request(payload: dict | bytes | str) -> MailFinishRequest:
    return _decode(_FINISH_REQUEST, payload)


def decode_mail_claim_reply(payload: dict | bytes | str) -> MailClaimReply:
    return _decode_success(_CLAIM_REPLY, payload)


def decode_mail_accepted_reply(payload: dict | bytes | str) -> MailAcceptedReply:
    return _decode_success(_ACCEPTED_REPLY, payload)


def native_command_json(command: ClosedModel) -> str:
    try:
        payload = command.model_dump(mode="json", by_alias=True)
        _reject_protocol_alias(payload)
        _assert_finite(payload)
        return json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise IdentityUnavailable() from None
