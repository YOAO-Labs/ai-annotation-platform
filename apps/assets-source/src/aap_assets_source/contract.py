"""Public language-neutral wire values for Assets source operations."""

from __future__ import annotations

from typing import Annotated, Literal, TypeVar
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_audit_contract import ProjectDeletionAuditContext

from .upload_contract import DatasetUploadCompletedResult, DatasetUploadReserve

SourceFailureCode = Literal[
    "invalid_request",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "catalog_manager_required",
    "dataset_missing",
    "item_missing",
    "upload_missing",
    "upload_scope_mismatch",
    "upload_not_pending",
    "dataset_busy",
    "source_busy",
    "source_in_use",
    "active_learning_reference",
    "creation_in_progress",
    "operation_conflict",
    "source_changed",
    "service_unavailable",
    "invalid_response",
    "storage_unavailable",
]

_SOURCE_FAILURE_CODES = frozenset(SourceFailureCode.__args__)


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceFailure(Exception):
    """A stable public failure returned by the Assets source capability."""

    def __init__(self, code: SourceFailureCode):
        if code not in _SOURCE_FAILURE_CODES:
            raise ValueError("Unknown Assets source failure code")
        super().__init__(code)
        self.code = code


class UploadInitRequest(DatasetUploadReserve):
    """Public upload-init request; identical to the frozen native reserve input."""


class UploadInitResult(_Value):
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID
    upload_url: StrictStr
    expires_in: Annotated[int, Field(strict=True, ge=0)] = 900

    @field_validator("upload_url")
    @classmethod
    def validate_signed_http_url(cls, value: str) -> str:
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("upload_url must not contain control characters")
        if value.startswith("/"):
            parsed = urlsplit(value)
            if (
                value.startswith("//")
                or "\\" in value
                or parsed.scheme
                or parsed.netloc
                or parsed.fragment
            ):
                raise ValueError("upload_url must be a safe root-relative URL")
            return value
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError as exc:
            raise ValueError("upload_url must be a valid HTTP URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("upload_url must be a signed HTTP(S) URL")
        return value


class UploadCompleteRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID


class UploadOutcome(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID
    file_type: StrictStr
    result: DatasetUploadCompletedResult | None
    duplicate_of: UUID | None

    @model_validator(mode="after")
    def check_disposition(self) -> UploadOutcome:
        if self.result is not None and self.duplicate_of is not None:
            raise ValueError("Upload outcome cannot be both accepted and duplicate")
        if self.result is not None and self.result.item_id != self.item_id:
            raise ValueError("Upload result item does not match its request")
        if self.duplicate_of == self.item_id:
            raise ValueError("Duplicate outcome cannot refer to its own item")
        return self


class SourceDeletionRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID | None = None
    audit_context: ProjectDeletionAuditContext | None = None


class _FailureData(_Value):
    code: SourceFailureCode


class _SourceReply(_Value):
    schema_version: Literal["aap-assets-source/v1"] = Field(alias="schema")
    ok: StrictBool
    data: dict | None = None
    error: _FailureData | None = None

    @model_validator(mode="after")
    def check_branch(self) -> _SourceReply:
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("Successful source reply must contain only data")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("Failed source reply must contain only its stable error")
        return self


_Result = TypeVar("_Result", bound=BaseModel)


def decode_source_reply(payload: object, result_model: type[_Result]) -> _Result:
    """Decode a strict whole-operation source reply without leaking peer errors."""
    try:
        reply = _SourceReply.model_validate(payload)
    except ValidationError:
        raise SourceFailure("invalid_response") from None
    if reply.schema_version != "aap-assets-source/v1":
        raise SourceFailure("invalid_response")
    if not reply.ok:
        assert reply.error is not None
        raise SourceFailure(reply.error.code)
    try:
        return result_model.model_validate(reply.data)
    except ValidationError:
        raise SourceFailure("invalid_response") from None
