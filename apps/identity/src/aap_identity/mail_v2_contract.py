"""Closed protocol for the versioned Identity credential and invitation mail worker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .contract import ClosedModel
from .mail_contract import _decode, _decode_success, native_command_json

MAIL_V2_NATIVE_PROTOCOL = "aap-identity-mail-native/v2"
MAIL_V2_POLICY_PROTOCOL = "aap-identity-mail-policy/v2"
MAIL_V2_POLICY = "aap-identity-mail/v2"

MailV2Outcome = Literal["delivered", "rejected", "retry", "unknown", "invalid_key"]
MailV2Reason = Literal[
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


class MailV2PolicyReply(ClosedModel):
    protocol: Literal["aap-identity-mail-policy/v2"] = Field(alias="schema")
    policy: Literal["aap-identity-mail/v2"]


class MailV2ClaimRequest(ClosedModel):
    protocol: Literal["aap-identity-mail-native/v2"] = Field(alias="schema")
    worker_id: uuid.UUID
    lease_seconds: int = Field(ge=1, le=300, strict=True)
    delivery_id: uuid.UUID | None


class MailV2RenewRequest(ClosedModel):
    protocol: Literal["aap-identity-mail-native/v2"] = Field(alias="schema")
    delivery_id: uuid.UUID
    worker_id: uuid.UUID
    lease_token: str = Field(min_length=1, max_length=256, strict=True, repr=False)


class MailV2FinishRequest(ClosedModel):
    protocol: Literal["aap-identity-mail-native/v2"] = Field(alias="schema")
    delivery_id: uuid.UUID
    worker_id: uuid.UUID
    lease_token: str = Field(min_length=1, max_length=256, strict=True, repr=False)
    outcome: MailV2Outcome
    reason: MailV2Reason
    retry_delay_seconds: int | None = Field(default=None, ge=1, le=300, strict=True)

    @model_validator(mode="after")
    def retry_delay_shape(self) -> MailV2FinishRequest:
        retryable = self.outcome in {"retry", "unknown", "invalid_key"}
        if retryable != (self.retry_delay_seconds is not None):
            raise ValueError("Retryable mail outcomes require a bounded retry delay")
        if self.outcome == "delivered" and self.reason != "sent":
            raise ValueError("Delivered mail must use the sent reason")
        if self.outcome == "invalid_key" and self.reason != "derivation_unavailable":
            raise ValueError("Invalid mail key must use the safe derivation reason")
        if self.outcome != "delivered" and self.reason == "sent":
            raise ValueError("Only delivered mail may use the sent reason")
        if self.outcome != "invalid_key" and self.reason == "derivation_unavailable":
            raise ValueError("Only invalid keys may use the derivation reason")
        return self


class _MailV2LeaseBase(ClosedModel):
    id: uuid.UUID
    email: str = Field(min_length=3, max_length=255, strict=True, repr=False)
    frontend_base_url: str = Field(min_length=1, strict=True, repr=False)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True, repr=False)
    token_ciphertext: str = Field(
        min_length=1, max_length=4096, strict=True, repr=False
    )
    encryption_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True, repr=False)
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


class RecoveryMailV2Lease(_MailV2LeaseBase):
    purpose: Literal["password_reset", "email_verification"]
    actor_id: uuid.UUID


class InvitationMailV2Lease(_MailV2LeaseBase):
    purpose: Literal["invitation"]
    issuer_id: uuid.UUID
    invitation_id: uuid.UUID
    project_name: str | None = Field(max_length=255, strict=True, repr=False)
    project_member_role: Literal["annotator", "reviewer", "viewer"] | None

    @field_validator("project_name")
    @classmethod
    def safe_project_name(cls, value: str | None) -> str | None:
        if value is not None and any(character in value for character in "\r\n\x00"):
            raise ValueError("Invalid invitation project name")
        return value


MailV2Lease: TypeAlias = Annotated[
    RecoveryMailV2Lease | InvitationMailV2Lease,
    Field(discriminator="purpose"),
]


class MailV2ClaimData(ClosedModel):
    delivery: MailV2Lease | None


class MailV2ClaimReply(ClosedModel):
    protocol: Literal["aap-identity-mail-native/v2"] = Field(alias="schema")
    ok: Literal[True]
    data: MailV2ClaimData

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Successful mail response requires boolean true")
        return value


class MailV2AcceptedData(ClosedModel):
    accepted: bool

    @field_validator("accepted", mode="before")
    @classmethod
    def strict_accepted(cls, value):
        if type(value) is not bool:
            raise ValueError("Mail lease acceptance must be a boolean")
        return value


class MailV2AcceptedReply(ClosedModel):
    protocol: Literal["aap-identity-mail-native/v2"] = Field(alias="schema")
    ok: Literal[True]
    data: MailV2AcceptedData

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Successful mail response requires boolean true")
        return value


_POLICY = TypeAdapter(MailV2PolicyReply)
_CLAIM_REQUEST = TypeAdapter(MailV2ClaimRequest)
_RENEW_REQUEST = TypeAdapter(MailV2RenewRequest)
_FINISH_REQUEST = TypeAdapter(MailV2FinishRequest)
_CLAIM_REPLY = TypeAdapter(MailV2ClaimReply)
_ACCEPTED_REPLY = TypeAdapter(MailV2AcceptedReply)


def decode_mail_v2_policy(payload: dict | bytes | str) -> MailV2PolicyReply:
    return _decode(_POLICY, payload)


def decode_mail_v2_claim_request(payload: dict | bytes | str) -> MailV2ClaimRequest:
    return _decode(_CLAIM_REQUEST, payload)


def decode_mail_v2_renew_request(payload: dict | bytes | str) -> MailV2RenewRequest:
    return _decode(_RENEW_REQUEST, payload)


def decode_mail_v2_finish_request(payload: dict | bytes | str) -> MailV2FinishRequest:
    return _decode(_FINISH_REQUEST, payload)


def decode_mail_v2_claim_reply(payload: dict | bytes | str) -> MailV2ClaimReply:
    return _decode_success(_CLAIM_REPLY, payload)


def decode_mail_v2_accepted_reply(payload: dict | bytes | str) -> MailV2AcceptedReply:
    return _decode_success(_ACCEPTED_REPLY, payload)


def native_mail_v2_command_json(command: ClosedModel) -> str:
    return native_command_json(command)
