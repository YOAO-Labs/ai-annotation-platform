"""Closed read contract for the Recovery owner's registration setting."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, TypeAdapter, field_validator

from .contract import ClosedModel, IdentityUnavailable
from .recovery_contract import _json_bytes

REGISTRATION_STATUS_PROTOCOL = "aap-identity-registration-status/v1"
RegistrationReadOperation = Literal["read_status", "read_deployment_default"]


class RegistrationStatusRequest(ClosedModel):
    protocol: Literal["aap-identity-registration-status/v1"] = Field(alias="schema")
    operation: RegistrationReadOperation


class RegistrationStatusFallbacks(ClosedModel):
    allow_open_registration: bool
    frontend_base_url: str


class RegistrationStatusValue(ClosedModel):
    allow_open_registration: bool


class RegistrationStatusReply(ClosedModel):
    protocol: Literal["aap-identity-registration-status/v1"] = Field(alias="schema")
    operation: RegistrationReadOperation
    ok: Literal[True]
    data: RegistrationStatusValue

    @field_validator("ok", mode="before")
    @classmethod
    def strict_ok(cls, value):
        if type(value) is not bool:
            raise ValueError("Registration read success must be a boolean")
        return value


def decode_registration_status_request(
    payload: dict | bytes,
) -> RegistrationStatusRequest:
    try:
        return RegistrationStatusRequest.model_validate_json(_json_bytes(payload))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("Invalid registration read request") from None


def decode_registration_status_reply(
    payload: dict | bytes | ClosedModel, request: RegistrationStatusRequest
) -> RegistrationStatusReply:
    if not isinstance(request, RegistrationStatusRequest):
        raise IdentityUnavailable()
    try:
        reply = RegistrationStatusReply.model_validate_json(_json_bytes(payload))
        if reply.operation != request.operation:
            raise IdentityUnavailable()
        return reply
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityUnavailable() from None


def registration_status_schema() -> dict:
    schema = TypeAdapter(
        RegistrationStatusRequest
        | RegistrationStatusReply
        | RegistrationStatusFallbacks
    ).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": REGISTRATION_STATUS_PROTOCOL,
            "$comment": (
                "The Recovery owner supplies deployment defaults. read_status resolves "
                "the existing Settings policy in a bounded read transaction; "
                "read_deployment_default does not read overrides. Neither operation "
                "creates accounts, mail, audit events or receipts."
            ),
            "x-postgresql-functions": [
                "aap_identity_recovery_v1.read_registration_status(jsonb)"
            ],
        }
    )
    return schema
