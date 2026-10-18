"""Frozen public values for completing uploads of existing legacy source rows."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from .upload_contract import (
    ContentHash,
    DatasetUploadCompletedResult,
    DatasetUploadFailure,
    Dimension,
    FileSize,
    UploadErrorCode,
    canonical_content_hash,
)

_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LegacyUploadState(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "pending has neither result nor duplicate_of",
                "completed has a result and no duplicate_of",
                "duplicate has duplicate_of and no result",
            ]
        },
    )

    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    file_type: StrictStr
    width: Dimension | None
    height: Dimension | None
    state: Literal["pending", "completed", "duplicate"]
    result: DatasetUploadCompletedResult | None
    duplicate_of: UUID | None

    @model_validator(mode="after")
    def check_state(self) -> LegacyUploadState:
        if self.state == "pending" and (
            self.result is not None or self.duplicate_of is not None
        ):
            raise ValueError("pending legacy upload cannot have a terminal disposition")
        if self.state == "completed" and (
            self.result is None
            or self.result.item_id != self.item_id
            or self.duplicate_of is not None
        ):
            raise ValueError("completed legacy upload must contain its frozen result")
        if self.state == "duplicate" and (
            self.result is not None
            or self.duplicate_of is None
            or self.duplicate_of == self.item_id
        ):
            raise ValueError("duplicate legacy upload must identify another item")
        return self


class LegacyUploadLockRequest(_Value):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID


class LegacyUploadCompletion(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "content_hash is null or 32/64 hexadecimal characters and canonicalized to lowercase",
                "width and height are both null or both positive integers",
                "file_size is a signed bigint non-negative value",
            ]
        },
    )

    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID
    expected_file_path: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    file_size: FileSize
    content_hash: ContentHash | None
    width: Dimension | None
    height: Dimension | None

    @field_validator("content_hash")
    @classmethod
    def canonical_hash(cls, value: str | None) -> str | None:
        return canonical_content_hash(value)

    @model_validator(mode="after")
    def check_completion(self) -> LegacyUploadCompletion:
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must both be present or both be absent")
        if self.file_size > _MAX_SIGNED_BIGINT:
            raise ValueError("file_size exceeds PostgreSQL bigint range")
        return self


class _LegacyFailure(_Value):
    code: UploadErrorCode


class _LegacyReply(_Value):
    schema_version: Literal["aap-legacy-dataset-upload/v1"] = Field(alias="schema")
    ok: StrictBool
    data: LegacyUploadState | None = None
    error: _LegacyFailure | None = None

    @model_validator(mode="after")
    def check_branch(self) -> _LegacyReply:
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError(
                    "Successful legacy upload reply must contain only state"
                )
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError(
                "Failed legacy upload reply must contain only its stable error"
            )
        return self


def decode_legacy_upload_reply(payload: object) -> LegacyUploadState:
    """Decode the exact native legacy upload envelope and typed state."""
    reply = _LegacyReply.model_validate(payload)
    if not reply.ok:
        assert reply.error is not None
        raise DatasetUploadFailure(reply.error.code)
    assert reply.data is not None
    return reply.data


class LegacyUploadDataPort(Protocol):
    """Transactional persistence boundary used by the source upload workflow."""

    async def read(self, request: LegacyUploadLockRequest) -> LegacyUploadState: ...

    async def lock(self, request: LegacyUploadLockRequest) -> LegacyUploadState: ...

    async def complete(self, request: LegacyUploadCompletion) -> LegacyUploadState: ...
