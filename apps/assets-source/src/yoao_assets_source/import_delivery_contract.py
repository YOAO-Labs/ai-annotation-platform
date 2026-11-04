"""Frozen wire values for the Assets connector-import delivery relay."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

DatasetImportDeliveryKind = Literal["thumbnail", "video_metadata", "notification"]
DatasetImportDeliveryFailureCode = Literal["invalid_request", "lease_lost"]


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetImportDelivery(_Value):
    delivery_id: UUID
    job_id: UUID
    kind: DatasetImportDeliveryKind
    target_id: UUID
    lease_token: UUID
    lease_expires_at: datetime

    @field_validator("lease_expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease_expires_at must be timezone-aware")
        return value


class DatasetImportDeliveryCompletion(_Value):
    delivery_id: UUID
    lease_token: UUID
    delivered: Literal[True]


class DatasetImportDeliveryFailure(Exception):
    def __init__(self, code: DatasetImportDeliveryFailureCode):
        super().__init__(code)
        self.code = code


class _Failure(_Value):
    code: DatasetImportDeliveryFailureCode


class _ClaimReply(_Value):
    schema_version: Literal["aap-dataset-import-delivery/v1"] = Field(alias="schema")
    ok: StrictBool
    data: DatasetImportDelivery | None = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self) -> _ClaimReply:
        if self.ok:
            if "data" not in self.model_fields_set or "error" in self.model_fields_set:
                raise ValueError("successful claim must contain only data")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("failed claim must contain only error")
        return self


class _CompleteReply(_Value):
    schema_version: Literal["aap-dataset-import-delivery/v1"] = Field(alias="schema")
    ok: StrictBool
    data: DatasetImportDeliveryCompletion | None = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self) -> _CompleteReply:
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("successful completion must contain only its receipt")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("failed completion must contain only error")
        return self


def decode_dataset_import_delivery_claim(
    payload: object,
) -> DatasetImportDelivery | None:
    reply = _ClaimReply.model_validate(payload)
    if not reply.ok:
        assert reply.error is not None
        raise DatasetImportDeliveryFailure(reply.error.code)
    return reply.data


def decode_dataset_import_delivery_completion(
    payload: object,
    *,
    delivery_id: UUID,
    lease_token: UUID,
) -> DatasetImportDeliveryCompletion:
    reply = _CompleteReply.model_validate(payload)
    if not reply.ok:
        assert reply.error is not None
        raise DatasetImportDeliveryFailure(reply.error.code)
    assert reply.data is not None
    if reply.data.delivery_id != delivery_id or reply.data.lease_token != lease_token:
        raise ValueError("completion receipt does not match the active lease")
    return reply.data


__all__ = [
    "DatasetImportDelivery",
    "DatasetImportDeliveryCompletion",
    "DatasetImportDeliveryFailure",
    "DatasetImportDeliveryFailureCode",
    "DatasetImportDeliveryKind",
    "decode_dataset_import_delivery_claim",
    "decode_dataset_import_delivery_completion",
]
