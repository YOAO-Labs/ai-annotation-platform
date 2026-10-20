"""Closed portable contract for the two Identity operational creators."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import Field, ValidationError, field_validator, model_validator

from .actor_contract import _json_bytes
from .contract import ClosedModel, IdentityUnavailable

PROVISIONING_PROTOCOL = "aap-identity-provisioning/v1"
ProvisioningMode: TypeAlias = Literal["bootstrap_admin", "import_admin"]


class _ProvisioningModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def declared_schema_field_only(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class ProvisioningLookupRequest(_ProvisioningModel):
    protocol: Literal["aap-identity-provisioning/v1"] = Field(alias="schema")
    email: str = Field(strict=True)


class ProvisioningCreateRequest(_ProvisioningModel):
    protocol: Literal["aap-identity-provisioning/v1"] = Field(alias="schema")
    mode: ProvisioningMode
    account_id: uuid.UUID
    email: str = Field(strict=True)
    name: str = Field(strict=True)
    password_hash: str = Field(strict=True, repr=False)
    now: datetime | None
    request_id: str | None = Field(strict=True)

    @field_validator("now", mode="before")
    @classmethod
    def parse_timestamp_text(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid provisioning timestamp") from None
        return value

    @field_validator("now")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Provisioning timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def mode_specific_fields(self) -> ProvisioningCreateRequest:
        if self.mode == "bootstrap_admin":
            if self.now is None:
                raise ValueError("Bootstrap provisioning requires a timestamp")
        elif (
            self.email != "admin"
            or self.name != "超级管理员"
            or self.now is not None
            or self.request_id is not None
        ):
            raise ValueError("Import provisioning fields do not match the legacy seed")
        return self


class ProvisioningReply(_ProvisioningModel):
    protocol: Literal["aap-identity-provisioning/v1"] = Field(alias="schema")
    email: str = Field(strict=True)
    account_id: uuid.UUID | None


def decode_provisioning_reply(
    payload: dict | bytes,
    request: ProvisioningLookupRequest | ProvisioningCreateRequest,
) -> uuid.UUID | None:
    if not isinstance(request, (ProvisioningLookupRequest, ProvisioningCreateRequest)):
        raise IdentityUnavailable()
    try:
        reply = ProvisioningReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IdentityUnavailable() from None

    if reply.email != request.email:
        raise IdentityUnavailable()
    if isinstance(request, ProvisioningCreateRequest) and (
        reply.account_id is None or reply.account_id != request.account_id
    ):
        raise IdentityUnavailable()
    return reply.account_id
