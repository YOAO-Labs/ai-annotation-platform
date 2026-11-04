"""Frozen value boundary for Dataset upload intents owned by Assets."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

UploadFileType = Literal["image", "video", "other"]
UploadState = Literal["pending", "completed", "discarded"]
ContentHash = Annotated[
    str,
    Field(pattern=r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})$"),
]
UploadErrorCode = Literal[
    "invalid_request",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "dataset_missing",
    "upload_missing",
    "upload_scope_mismatch",
    "upload_not_pending",
    "dataset_busy",
    "operation_conflict",
    "source_changed",
]

FileSize = Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)]
Dimension = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]


def validate_relative_file_name(value: str) -> str:
    if (
        not value
        or len(value) > 500
        # UUID publication paths leave 897 bytes within the S3 object-key limit.
        or len(value.encode("utf-8")) > 897
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:/", value)
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("file_name must be a safe relative path")
    return value


def _file_type(content_type: str) -> UploadFileType:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"


def canonical_content_hash(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(
        r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})", value
    ):
        raise ValueError("content_hash must be 32 or 64 hexadecimal characters")
    return value.lower() if value is not None else None


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetUploadFailure(Exception):
    def __init__(self, code: UploadErrorCode):
        super().__init__(code)
        self.code = code


class DatasetUploadReserve(_Value):
    item_id: UUID
    actor_id: UUID
    dataset_id: UUID
    file_name: str = Field(
        min_length=1,
        max_length=500,
        description="Safe relative file path; may contain legitimate subdirectories.",
        json_schema_extra={"x-max-utf8-bytes": 897},
    )
    content_type: str = Field(min_length=1, max_length=255)

    @field_validator("file_name")
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        return validate_relative_file_name(value)

    @field_validator("content_type")
    @classmethod
    def canonical_content_type(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("content_type must be a canonical header value")
        return value


class DatasetUploadCompletion(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "content_hash is null or 32/64 hexadecimal characters",
                "width and height are both null or both positive integers",
            ]
        },
    )

    actor_id: UUID
    dataset_id: UUID
    item_id: UUID
    publication_id: UUID
    file_size: FileSize
    content_hash: ContentHash | None
    width: Dimension | None
    height: Dimension | None

    @field_validator("content_hash")
    @classmethod
    def canonical_content_hash(cls, value: str | None) -> str | None:
        return canonical_content_hash(value)

    @model_validator(mode="after")
    def paired_dimensions(self):
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must both be present or both be absent")
        return self


class DatasetUploadDiscard(_Value):
    actor_id: UUID
    dataset_id: UUID
    item_id: UUID
    reason: Literal["missing"]


class DatasetUploadCompletedResult(_Value):
    item_id: UUID
    linked_tasks: Annotated[int, Field(strict=True, ge=0)]


class DatasetUploadDiscardedResult(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "exactly one of duplicate_of or reason is present",
                "reason is missing or source_deleted",
            ]
        },
    )

    duplicate_of: UUID | None = None
    reason: Literal["missing", "source_deleted"] | None = None

    @model_validator(mode="after")
    def exactly_one_disposition(self):
        if ("duplicate_of" in self.model_fields_set) == (
            "reason" in self.model_fields_set
        ):
            raise ValueError("discarded result must contain exactly one disposition")
        if self.duplicate_of is None and self.reason is None:
            raise ValueError("discarded result disposition cannot be null")
        return self


class DatasetUploadIntent(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "publication_id and file_path are both null or both present",
                "file_path is dataset-sources/{dataset_id}/{item_id}/{publication_id}/{file_name}",
                "pending has no result; completed has item_id and linked_tasks; discarded has one disposition",
            ]
        },
    )

    item_id: UUID
    actor_id: UUID
    dataset_id: UUID
    file_name: str = Field(
        min_length=1, max_length=500, json_schema_extra={"x-max-utf8-bytes": 897}
    )
    file_path: str | None = Field(max_length=1000)
    file_type: UploadFileType
    content_type: str = Field(min_length=1, max_length=255)
    staging_key: str
    publication_id: UUID | None
    state: UploadState
    result: DatasetUploadCompletedResult | DatasetUploadDiscardedResult | None

    @field_validator("file_name")
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        return validate_relative_file_name(value)

    @field_validator("content_type")
    @classmethod
    def canonical_content_type(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("content_type must be a canonical header value")
        return value

    @model_validator(mode="after")
    def check_frozen_facts(self):
        if self.file_type != _file_type(self.content_type):
            raise ValueError("file_type does not match content_type")
        if self.staging_key != f"dataset-uploads/{self.item_id}/source":
            raise ValueError("staging_key does not match the reserved item")
        if (self.publication_id is None) != (self.file_path is None):
            raise ValueError("publication_id and file_path must be paired")
        if self.publication_id is not None:
            expected_path = (
                f"dataset-sources/{self.dataset_id}/{self.item_id}/"
                f"{self.publication_id}/{self.file_name}"
            )
            if self.file_path != expected_path or len(expected_path) > 1000:
                raise ValueError("file_path does not match the frozen publication")
        if self.state == "pending" and self.result is not None:
            raise ValueError("pending upload cannot have a terminal result")
        if self.state == "completed" and (
            self.file_path is None
            or not isinstance(self.result, DatasetUploadCompletedResult)
            or self.result.item_id != self.item_id
        ):
            raise ValueError("completed upload has an invalid frozen result")
        if self.state == "discarded" and not isinstance(
            self.result, DatasetUploadDiscardedResult
        ):
            raise ValueError("discarded upload must have a frozen disposition")
        if (
            isinstance(self.result, DatasetUploadDiscardedResult)
            and self.result.duplicate_of == self.item_id
        ):
            raise ValueError("duplicate result cannot refer to the upload itself")
        return self


class DatasetUploadAcceptance(DatasetUploadIntent):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "accepted contains the completed frozen result",
                "duplicate contains the discarded duplicate_of result",
            ]
        },
    )

    disposition: Literal["accepted", "duplicate"]
    duplicate_of: UUID | None = None

    @model_validator(mode="after")
    def check_disposition(self):
        if self.disposition == "accepted":
            if self.state != "completed" or self.duplicate_of is not None:
                raise ValueError("accepted upload must contain its completed receipt")
        elif (
            self.state != "discarded"
            or self.duplicate_of is None
            or not isinstance(self.result, DatasetUploadDiscardedResult)
            or self.result.duplicate_of != self.duplicate_of
        ):
            raise ValueError("duplicate upload must contain its duplicate receipt")
        return self


class DatasetUploadDuplicate(_Value):
    duplicate_of: UUID | None


class _Failure(_Value):
    code: UploadErrorCode


class _Reply(_Value):
    schema_version: Literal["aap-dataset-upload/v1"] = Field(alias="schema")
    ok: StrictBool
    data: dict | None = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("Successful upload reply must contain only data")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("Failed upload reply must contain only its stable error")
        return self


def decode_dataset_upload_reply(payload: object, model: type[_Value]) -> _Value:
    reply = _Reply.model_validate(payload)
    if reply.schema_version != "aap-dataset-upload/v1":
        raise ValueError("Unexpected Dataset upload contract")
    if not reply.ok:
        assert reply.error is not None
        raise DatasetUploadFailure(reply.error.code)
    return model.model_validate(reply.data)


class DatasetUploadDataPort(Protocol):
    async def reserve(self, request: DatasetUploadReserve) -> DatasetUploadIntent: ...

    async def allocate(self, request: dict): ...

    async def open_operation(self, creation_id): ...

    async def original_prepared(self, creation_id, prepared) -> None: ...

    async def original_writing(self, creation_id) -> None: ...

    async def original_written(self, creation_id, receipt) -> None: ...

    async def original_verified(self, creation_id, verification) -> None: ...

    async def fail_original(
        self, creation_id, outcome, error_code, evidence
    ) -> None: ...

    async def close_operation(
        self, creation_id, operation_id, physical_settled
    ) -> None: ...

    async def lock(
        self, *, actor_id: UUID, dataset_id: UUID, item_id: UUID
    ) -> DatasetUploadIntent: ...

    async def find_duplicate(
        self,
        *,
        actor_id: UUID,
        dataset_id: UUID,
        item_id: UUID,
        content_hash: str | None,
    ) -> DatasetUploadDuplicate: ...

    async def complete(
        self, request: DatasetUploadCompletion
    ) -> DatasetUploadAcceptance: ...

    async def discard(self, request: DatasetUploadDiscard) -> DatasetUploadIntent: ...
