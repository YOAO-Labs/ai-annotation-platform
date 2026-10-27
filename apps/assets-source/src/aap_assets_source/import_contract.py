"""Language-neutral values for the independent Dataset connector importer."""

from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

ImportFailureCode = Literal[
    "invalid_request",
    "invalid_source_object",
    "account_not_active",
    "account_busy",
    "manager_required",
    "dataset_missing",
    "connection_missing",
    "connection_changed",
    "dataset_busy",
    "operation_conflict",
    "job_missing",
    "job_terminal",
    "lease_lost",
    "import_cancelled",
    "invalid_response",
    "storage_unavailable",
    "storage_maintenance",
    "source_changed",
]

_IMPORT_FAILURE_CODES = frozenset(ImportFailureCode.__args__)
_MAX_BIGINT = 9_223_372_036_854_775_807
_MAX_INTEGER = 2_147_483_647
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_connection_config(value: dict[str, Any]) -> dict[str, Any]:
    if len(value) > 12:
        raise ValueError("connection_config contains too many fields")
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError("connection_config keys must be bounded strings")
        lowered = key.lower()
        if any(
            marker in lowered
            for marker in (
                "secret",
                "password",
                "credential",
                "token",
                "private_key",
                "access_key",
            )
        ):
            raise ValueError("connection_config cannot contain credentials")
        if not isinstance(item, (str, int, bool, type(None))) or (
            isinstance(item, int)
            and not isinstance(item, bool)
            and abs(item) > _MAX_BIGINT
        ):
            raise ValueError("connection_config values must be bounded JSON scalars")
        if isinstance(item, str) and (
            len(item) > 2048 or any(unicodedata.category(char) == "Cc" for char in item)
        ):
            raise ValueError("connection_config values must be safe strings")
    return value


def _validate_connection_config(value: dict[str, Any], kind: str) -> dict[str, Any]:
    _safe_connection_config(value)
    allowed = (
        {"endpoint", "bucket", "region", "base_prefix", "use_ssl"}
        if kind == "s3"
        else {"host", "port", "username", "base_path", "auth_type"}
    )
    required = {"endpoint", "bucket"} if kind == "s3" else {"host", "username"}
    if set(value) - allowed or required - set(value):
        raise ValueError("connection_config does not match its connector kind")
    if any(not isinstance(value[key], str) or not value[key] for key in required):
        raise ValueError(
            "connector host, endpoint, bucket and username must be strings"
        )
    if kind == "s3" and "use_ssl" in value and not isinstance(value["use_ssl"], bool):
        raise ValueError("S3 use_ssl must be a boolean")
    if (
        kind == "sftp"
        and "port" in value
        and (type(value["port"]) is not int or not 1 <= value["port"] <= 65535)
    ):
        raise ValueError("SFTP port must be between 1 and 65535")
    return value


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImportFailure(Exception):
    """A stable failure returned by the Dataset import capability."""

    def __init__(self, code: ImportFailureCode):
        if code not in _IMPORT_FAILURE_CODES:
            raise ValueError("Unknown Dataset import failure code")
        super().__init__(code)
        self.code = code


class ImportContext(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "dataset_display_id is the persisted bounded string identifier",
                "connection_config contains no credentials",
            ]
        },
    )

    actor_id: UUID
    dataset_id: UUID
    dataset_display_id: Annotated[StrictStr, Field(min_length=1, max_length=20)]
    dataset_name: Annotated[StrictStr, Field(max_length=255)]
    connection_id: UUID
    connection_name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    connection_kind: Literal["s3", "sftp"]
    connection_config: dict[str, Any]

    @field_validator("connection_config")
    @classmethod
    def safe_connection_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_connection_config(value)

    @model_validator(mode="after")
    def config_matches_kind(self) -> ImportContext:
        _validate_connection_config(self.connection_config, self.connection_kind)
        return self


class ImportAcceptance(_Value):
    job_id: UUID
    actor_id: UUID
    dataset_id: UUID
    connection_id: UUID


class ImportLimits(_Value):
    dataset_import_max_files: Annotated[StrictInt, Field(gt=0, le=_MAX_INTEGER)]
    dataset_import_max_total_bytes: Annotated[StrictInt, Field(gt=0, le=_MAX_BIGINT)]
    version: Annotated[StrictStr, Field(min_length=1, max_length=128)]


def _safe_relpath(
    value: str, *, max_characters: int = 897, max_utf8_bytes: int = 897
) -> str:
    if (
        not value
        or len(value) > max_characters
        or len(value.encode("utf-8")) > max_utf8_bytes
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:[/\\]", value)
        or "\\" in value
        or any(unicodedata.category(char) == "Cc" for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("relpath must be a safe relative path")
    return value


class ImportSourceObject(_Value):
    relpath: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    size: Annotated[StrictInt, Field(ge=0, le=_MAX_BIGINT)]
    mtime: datetime | None
    etag: Annotated[StrictStr, Field(max_length=1024)] | None

    @field_validator("relpath")
    @classmethod
    def safe_relpath(cls, value: str) -> str:
        return _safe_relpath(value, max_characters=4096, max_utf8_bytes=4096)

    @field_validator("mtime")
    @classmethod
    def timezone_aware_mtime(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("mtime must include a timezone")
        return value

    @field_validator("etag")
    @classmethod
    def safe_etag(cls, value: str | None) -> str | None:
        if value is not None and any(
            unicodedata.category(char) == "Cc" for char in value
        ):
            raise ValueError("etag must not contain control characters")
        return value


ImportStage = Literal["enumerating", "importing", "completed", "failed", "cancelled"]


class ImportState(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "lease_token and lease_expires_at are both null or both present",
                "enumerating has no manifest and cursor zero",
                "importing has a frozen manifest",
                "cursor never exceeds total",
                "added plus skipped plus error_count equals cursor",
                "errors contains at most error_count entries",
                "total and total_bytes are both null or both present",
                "completed has a manifest and cursor equals total",
                "manifest and committed byte counts stay within accepted limits",
                "terminal states hold no lease",
            ]
        },
    )

    job_id: UUID
    actor_id: UUID
    dataset_id: UUID
    connection_id: UUID
    source_path: Annotated[StrictStr, Field(max_length=2048)]
    recursive: StrictBool
    include_globs: Annotated[
        list[Annotated[StrictStr, Field(min_length=1, max_length=255)]],
        Field(max_length=256),
    ]
    settings_snapshot: ImportLimits
    stage: ImportStage
    cursor: Annotated[StrictInt, Field(ge=0, le=_MAX_INTEGER)]
    total: Annotated[StrictInt, Field(ge=0, le=_MAX_INTEGER)] | None
    total_bytes: Annotated[StrictInt, Field(ge=0, le=_MAX_BIGINT)] | None
    added: Annotated[StrictInt, Field(ge=0, le=_MAX_INTEGER)]
    skipped: Annotated[StrictInt, Field(ge=0, le=_MAX_INTEGER)]
    error_count: Annotated[StrictInt, Field(ge=0, le=_MAX_INTEGER)]
    errors: Annotated[
        list[Annotated[StrictStr, Field(max_length=512)]], Field(max_length=100)
    ]
    linked_tasks: Annotated[StrictInt, Field(ge=0, le=_MAX_INTEGER)]
    committed_bytes: Annotated[StrictInt, Field(ge=0, le=_MAX_BIGINT)]
    lease_token: UUID | None
    lease_expires_at: datetime | None

    @field_validator("source_path")
    @classmethod
    def safe_source_path(cls, value: str) -> str:
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("source_path must not contain control characters")
        if any(part == ".." for part in value.replace("\\", "/").split("/")):
            raise ValueError("source_path must not contain '..'")
        return value

    @field_validator("include_globs")
    @classmethod
    def safe_globs(cls, values: list[str]) -> list[str]:
        if any(
            any(unicodedata.category(char) == "Cc" for char in item) for item in values
        ):
            raise ValueError("include_globs must not contain control characters")
        return values

    @field_validator("errors")
    @classmethod
    def safe_errors(cls, values: list[str]) -> list[str]:
        if any(
            any(unicodedata.category(char) == "Cc" for char in item) for item in values
        ):
            raise ValueError("errors must not contain control characters")
        return values

    @model_validator(mode="after")
    def consistent_progress_and_lease(self) -> ImportState:
        if (self.lease_token is None) != (self.lease_expires_at is None):
            raise ValueError("lease_token and lease_expires_at must be paired")
        if self.lease_expires_at is not None and (
            self.lease_expires_at.tzinfo is None
            or self.lease_expires_at.utcoffset() is None
        ):
            raise ValueError("lease_expires_at must include a timezone")
        if self.total is not None and self.cursor > self.total:
            raise ValueError("cursor cannot exceed total")
        if (self.total is None) != (self.total_bytes is None):
            raise ValueError("total and total_bytes must be paired")
        if self.added + self.skipped + self.error_count != self.cursor:
            raise ValueError("outcome counts must equal the processed cursor")
        if len(self.errors) > self.error_count:
            raise ValueError("errors cannot exceed error_count")
        if (
            self.total is not None
            and self.total > self.settings_snapshot.dataset_import_max_files
        ):
            raise ValueError("manifest exceeds accepted file-count limit")
        if (
            self.total_bytes is not None
            and self.total_bytes > self.settings_snapshot.dataset_import_max_total_bytes
        ):
            raise ValueError("manifest exceeds accepted byte limit")
        if self.committed_bytes > self.settings_snapshot.dataset_import_max_total_bytes:
            raise ValueError("committed bytes exceed accepted byte limit")
        if self.stage == "enumerating" and (self.total is not None or self.cursor != 0):
            raise ValueError("enumerating state cannot contain a manifest cursor")
        if self.stage == "importing" and (
            self.total is None or self.total_bytes is None
        ):
            raise ValueError("importing state requires a frozen manifest")
        if self.stage == "completed" and (
            self.total is None or self.cursor != self.total
        ):
            raise ValueError("completed state must consume the full manifest")
        if (
            self.stage in {"completed", "failed", "cancelled"}
            and self.lease_token is not None
        ):
            raise ValueError("terminal state cannot hold a lease")
        return self


class ImportConnection(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "secret_enc is base64-encoded encrypted connector data",
                "config contains no credentials",
            ]
        },
    )

    job_id: UUID
    lease_token: UUID
    kind: Literal["s3", "sftp"]
    config: dict[str, Any]
    secret_enc: Annotated[StrictStr, Field(min_length=1, max_length=65536)]
    allowlist: Annotated[
        list[Annotated[StrictStr, Field(min_length=1, max_length=253)]],
        Field(max_length=256),
    ]

    @field_validator("config")
    @classmethod
    def no_secret_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_connection_config(value)

    @model_validator(mode="after")
    def config_matches_kind(self) -> ImportConnection:
        _validate_connection_config(self.config, self.kind)
        return self

    @field_validator("secret_enc")
    @classmethod
    def base64_secret(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
            raise ValueError("secret_enc must be base64")
        return value


class ImportEntry(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "file_path is dataset-sources/{dataset_id}/{item_id}/{publication_id}/{file_name}",
                "dataset_id matches the active ImportState.dataset_id",
                "publication_id changes on every transfer attempt",
                "file_name fits DatasetItem.file_name and the S3 key byte limit",
            ]
        },
    )

    job_id: UUID
    lease_token: UUID
    index: Annotated[StrictInt, Field(ge=0, le=_MAX_INTEGER)]
    item_id: UUID
    publication_id: UUID
    file_name: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    file_type: Literal["image", "video", "point_cloud", "other"]
    source: ImportSourceObject

    @field_validator("file_name")
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        return _safe_relpath(value, max_characters=500, max_utf8_bytes=897)

    @model_validator(mode="after")
    def frozen_publication_path(self) -> ImportEntry:
        parts = self.file_path.split("/", 4)
        if (
            len(parts) != 5
            or parts[0] != "dataset-sources"
            or parts[2] != str(self.item_id)
            or parts[3] != str(self.publication_id)
            or parts[4] != self.file_name
            or len(self.file_name.encode("utf-8")) > 897
            or len(self.file_path.encode("utf-8")) > 1024
            or any(unicodedata.category(char) == "Cc" for char in self.file_path)
        ):
            raise ValueError("file_path does not match the frozen publication")
        return self


class ImportEntryCompletion(_Value):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-invariants": [
                "copied requires non-negative file_size, lowercase MD5 and paired optional dimensions",
                "error requires only a bounded safe error_code",
            ]
        },
    )

    status: Literal["copied", "error"]
    file_size: Annotated[StrictInt, Field(ge=0, le=_MAX_BIGINT)] | None = None
    content_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    width: Annotated[StrictInt, Field(gt=0, le=_MAX_INTEGER)] | None = None
    height: Annotated[StrictInt, Field(gt=0, le=_MAX_INTEGER)] | None = None
    error_code: Annotated[StrictStr, Field(min_length=1, max_length=64)] | None = None

    @field_validator("error_code")
    @classmethod
    def safe_error_code(cls, value: str | None) -> str | None:
        if value is not None and not _SAFE_ERROR_CODE.fullmatch(value):
            raise ValueError("error_code must be a safe identifier")
        return value

    @model_validator(mode="after")
    def check_outcome(self) -> ImportEntryCompletion:
        if self.status == "copied":
            if (
                self.file_size is None
                or self.content_hash is None
                or self.error_code is not None
            ):
                raise ValueError("copied completion requires size and MD5 hash")
            if (self.width is None) != (self.height is None):
                raise ValueError("width and height must be paired")
        elif (
            any(
                value is not None
                for value in (
                    self.file_size,
                    self.content_hash,
                    self.width,
                    self.height,
                )
            )
            or self.error_code is None
        ):
            raise ValueError("error completion requires only an error_code")
        return self


class _ImportFailure(_Value):
    code: ImportFailureCode


class _ImportReply(_Value):
    schema_version: Literal["aap-dataset-import/v1"] = Field(alias="schema")
    ok: StrictBool
    data: dict[str, Any] | None = None
    error: _ImportFailure | None = None

    @model_validator(mode="after")
    def check_branch(self) -> _ImportReply:
        if self.ok:
            if "data" not in self.model_fields_set or "error" in self.model_fields_set:
                raise ValueError("Successful import reply must contain only data")
        elif (
            self.error is None
            or "error" not in self.model_fields_set
            or "data" in self.model_fields_set
        ):
            raise ValueError("Failed import reply must contain only its stable error")
        return self


_Result = TypeVar("_Result", bound=BaseModel)


def decode_import_reply(payload: object, result_model: type[_Result]) -> _Result | None:
    """Decode a strict native result or null without leaking database details."""
    try:
        reply = _ImportReply.model_validate(payload)
    except ValidationError:
        raise ImportFailure("invalid_response") from None
    if reply.schema_version != "aap-dataset-import/v1":
        raise ImportFailure("invalid_response")
    if not reply.ok:
        assert reply.error is not None
        raise ImportFailure(reply.error.code)
    if reply.data is None:
        return None
    try:
        return result_model.model_validate(reply.data)
    except ValidationError:
        raise ImportFailure("invalid_response") from None
