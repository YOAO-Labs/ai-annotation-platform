"""Accepted image input identities and native capability contracts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from aap_assets_images.contract import (
    Count,
    Digest,
    ErrorCode as ImageErrorCode,
    ImageOwner,
    ImageSource,
    ObjectIdentity,
    Revision,
    Value,
    json_bytes,
)

INPUT_SCHEMA = "aap-assets-inputs/v1"
REQUEST_BYTES = 64 * 1024
REPLY_BYTES = 2 * 1024 * 1024
BATCH_SIZE = 100
InputStatus = Literal["pending", "materializing", "ready", "failed", "expired"]
ErrorCode = Literal[
    ImageErrorCode,
    "input_missing",
    "input_scope_mismatch",
    "input_not_ready",
    "input_expired",
    "input_corrupt",
    "input_busy",
    "job_not_active",
    "job_busy",
    "reference_busy",
    "versioning_required",
]
VersionId = Annotated[StrictStr, Field(min_length=1, max_length=255)]
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ATTEMPT_PREFIX_PATTERN = rf"^accepted-inputs/{_UUID}/g[1-9][0-9]*/a{_UUID}/$"
BLOB_KEY_PATTERN = ATTEMPT_PREFIX_PATTERN.removesuffix("$") + "source$"


def attempt_prefix(input_id: UUID, generation: int, lease_token: UUID) -> str:
    return f"accepted-inputs/{input_id}/g{generation}/a{lease_token}/"


class InputFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class InputRequest(Value):
    input_id: UUID
    job_id: UUID
    task_id: UUID


class FindInputRequest(Value):
    job_id: UUID
    task_id: UUID
    actor_id: UUID


class ReserveInputRequest(FindInputRequest):
    source_snapshot: Digest
    object_identity: ObjectIdentity


class InputRecord(Value):
    input_id: UUID
    job_id: UUID
    task_id: UUID
    project_id: UUID
    actor_id: UUID
    status: InputStatus
    task_version: Count
    task_source_generation: Revision
    dataset_item_id: UUID | None
    task_source_snapshot: Digest
    source_owner: ImageOwner
    source_generation: Revision
    source_snapshot: Digest
    byte_size: Count | None
    sha256: Digest | None
    error: ErrorCode | None
    created_at: AwareDatetime
    retire_after: AwareDatetime | None

    @model_validator(mode="after")
    def coherent_identity(self):
        if (self.sha256 is None) != (self.byte_size is None):
            raise ValueError("incomplete measured input identity")
        if self.status == "ready" and (self.sha256 is None or self.error is not None):
            raise ValueError("ready input must have measured bytes and no error")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed input requires an error")
        if self.dataset_item_id is None:
            if self.source_owner.kind != "task" or self.source_owner.id != self.task_id:
                raise ValueError("legacy input owner changed")
        elif (
            self.source_owner.kind != "dataset_item"
            or self.source_owner.id != self.dataset_item_id
        ):
            raise ValueError("linked input owner changed")
        return self


class FoundInput(Value):
    input: InputRecord | None


class InputBlob(Value):
    """Private exact object version; consumers receive a signed reference instead."""

    key: Annotated[
        StrictStr, Field(min_length=1, max_length=2048, pattern=BLOB_KEY_PATTERN)
    ]
    version_id: VersionId
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Count
    sha256: Digest

    @field_validator("version_id")
    @classmethod
    def real_version(cls, value):
        if not value.strip() or value == "null":
            raise ValueError("accepted input requires an immutable object version")
        return value


class InputWork(Value):
    input: InputRecord
    source: ImageSource
    blob: InputBlob | None

    @model_validator(mode="after")
    def coherent_source(self):
        record, source = self.input, self.source
        if (
            source.owner != record.source_owner
            or source.task_id != record.task_id
            or source.project_id != record.project_id
            or source.source_generation != record.source_generation
            or source.source_snapshot != record.source_snapshot
            or source.object_identity is None
            or source.source_drifted
            or not source.upload_ready
        ):
            raise ValueError("private input work changed accepted source")
        if self.blob is not None and (
            not self.blob.key.startswith(f"accepted-inputs/{record.input_id}/")
            or self.blob.sha256 != record.sha256
            or self.blob.byte_size != record.byte_size
        ):
            raise ValueError("private input work changed measured bytes")
        if record.status == "ready" and self.blob is None:
            raise ValueError("ready input omitted its object version")
        return self


class InputReference(Value):
    input: InputRecord
    url: Annotated[StrictStr, Field(min_length=1, max_length=16384)]
    version_id: VersionId
    expires_in: Annotated[StrictInt, Field(ge=1, le=3600)]

    @model_validator(mode="after")
    def immutable_reference(self):
        parsed = urlsplit(self.url)
        if (
            self.input.status != "ready"
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.fragment
            or any(c.isspace() for c in self.url)
            or not self.version_id.strip()
            or self.version_id == "null"
            or parse_qs(parsed.query).get("versionId") != [self.version_id]
        ):
            raise ValueError("reference is not bound to the accepted object version")
        return self


class ClaimInputRequest(Value):
    worker_id: UUID
    input_id: UUID | None = None
    lease_seconds: Annotated[StrictInt, Field(ge=60, le=7200)] = 2100


class InputClaim(Value):
    input: InputRecord
    source: ImageSource
    generation: Revision
    lease_token: UUID
    lease_expires_at: AwareDatetime
    blob_key: StrictStr

    @model_validator(mode="after")
    def exact_attempt(self):
        InputWork(input=self.input, source=self.source, blob=None)
        if (
            self.input.status != "materializing"
            or self.blob_key
            != attempt_prefix(self.input.input_id, self.generation, self.lease_token)
            + "source"
        ):
            raise ValueError("input claim escaped its writer attempt")
        return self


class ClaimedInput(Value):
    claim: InputClaim | None


class InputLeaseRequest(Value):
    input_id: UUID
    generation: Revision
    lease_token: UUID


class InputLease(InputLeaseRequest):
    lease_expires_at: AwareDatetime


class HeartbeatInputRequest(InputLeaseRequest):
    lease_seconds: Annotated[StrictInt, Field(ge=60, le=7200)] = 2100


class FinishInputRequest(InputLeaseRequest):
    blob: InputBlob

    @model_validator(mode="after")
    def exact_blob(self):
        if (
            self.blob.key
            != attempt_prefix(self.input_id, self.generation, self.lease_token)
            + "source"
        ):
            raise ValueError("input output escaped its writer attempt")
        return self


class FailInputRequest(InputLeaseRequest):
    error_code: ErrorCode


class LockInputRequest(Value):
    input_id: UUID
    task_id: UUID
    project_id: UUID


class SweepInputsRequest(Value):
    page_size: Annotated[StrictInt, Field(ge=1, le=BATCH_SIZE)] = BATCH_SIZE


class InputSweepResult(Value):
    retired: Count


class ClaimInputGarbageRequest(SweepInputsRequest):
    worker_id: UUID
    lease_seconds: Annotated[StrictInt, Field(ge=60, le=7200)] = 600


class InputGarbage(Value):
    garbage_id: UUID
    prefix: Annotated[StrictStr, Field(pattern=ATTEMPT_PREFIX_PATTERN)]
    lease_token: UUID


class InputGarbageBatch(Value):
    items: Annotated[list[InputGarbage], Field(max_length=BATCH_SIZE)]


class CompleteInputGarbageRequest(Value):
    garbage_id: UUID
    lease_token: UUID


class InputGarbageAck(Value):
    garbage_id: UUID
    completed: StrictBool


@dataclass(frozen=True)
class NativeOperation:
    request: type[BaseModel]
    result: type[BaseModel]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    role: Literal["control", "worker", "labeling", "system", "cleanup"]


OPERATIONS = {
    "find_accepted_image_input": NativeOperation(
        FindInputRequest,
        FoundInput,
        ("job_id", "task_id", "actor_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "reserve_accepted_image_input": NativeOperation(
        ReserveInputRequest,
        InputRecord,
        ("job_id", "task_id", "actor_id", "source_snapshot", "object_identity"),
        ("uuid", "uuid", "uuid", "text", "jsonb"),
        "control",
    ),
    "read_accepted_image_input": NativeOperation(
        InputRequest,
        InputRecord,
        ("input_id", "job_id", "task_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "read_accepted_image_input_work": NativeOperation(
        InputRequest,
        InputWork,
        ("input_id", "job_id", "task_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "claim_accepted_image_input": NativeOperation(
        ClaimInputRequest,
        ClaimedInput,
        ("worker_id", "input_id", "lease_seconds"),
        ("uuid", "uuid", "integer"),
        "worker",
    ),
    "heartbeat_accepted_image_input": NativeOperation(
        HeartbeatInputRequest,
        InputLease,
        ("input_id", "generation", "lease_token", "lease_seconds"),
        ("uuid", "bigint", "uuid", "integer"),
        "worker",
    ),
    "finish_accepted_image_input": NativeOperation(
        FinishInputRequest,
        InputRecord,
        ("input_id", "generation", "lease_token", "blob"),
        ("uuid", "bigint", "uuid", "jsonb"),
        "worker",
    ),
    "fail_accepted_image_input": NativeOperation(
        FailInputRequest,
        InputRecord,
        ("input_id", "generation", "lease_token", "error_code"),
        ("uuid", "bigint", "uuid", "text"),
        "worker",
    ),
    "lock_accepted_image_input": NativeOperation(
        LockInputRequest,
        InputRecord,
        ("input_id", "task_id", "project_id"),
        ("uuid", "uuid", "uuid"),
        "labeling",
    ),
    "sweep_accepted_image_inputs": NativeOperation(
        SweepInputsRequest, InputSweepResult, ("page_size",), ("integer",), "system"
    ),
    "claim_accepted_input_garbage": NativeOperation(
        ClaimInputGarbageRequest,
        InputGarbageBatch,
        ("worker_id", "lease_seconds", "page_size"),
        ("uuid", "integer", "integer"),
        "cleanup",
    ),
    "complete_accepted_input_garbage": NativeOperation(
        CompleteInputGarbageRequest,
        InputGarbageAck,
        ("garbage_id", "lease_token"),
        ("uuid", "uuid"),
        "cleanup",
    ),
}


class _Error(Value):
    code: ErrorCode


_Result = TypeVar("_Result", bound=BaseModel)


def decode_reply(raw: Any, result_type: type[_Result]) -> _Result:
    try:
        if isinstance(raw, (str, bytes)):
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > REPLY_BYTES:
                raise ValueError("oversized input reply")

            def reject_constant(_):
                raise ValueError("nonfinite JSON")

            raw = json.loads(raw, parse_constant=reject_constant)
        elif len(json_bytes(raw)) > REPLY_BYTES:
            raise ValueError("oversized input reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != INPUT_SCHEMA
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid input envelope")
        if not raw["ok"]:
            if set(raw) != {"schema", "ok", "error"}:
                raise ValueError("invalid input failure")
            raise InputFailure(_Error.model_validate(raw["error"]).code)
        if set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid input success")
        return result_type.model_validate(raw["data"])
    except InputFailure:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        raise InputFailure("invalid_response") from None


def require_scope(request: BaseModel, result: BaseModel) -> None:
    record = (
        result.input
        if isinstance(result, (FoundInput, InputWork, InputReference))
        else result
    )
    if isinstance(result, ClaimedInput):
        if result.claim is None:
            return
        record = result.claim.input
    if record is None:
        return
    for name in (
        "input_id",
        "job_id",
        "task_id",
        "project_id",
        "actor_id",
        "generation",
        "lease_token",
        "garbage_id",
    ):
        expected = getattr(request, name, None)
        if (
            expected is not None
            and hasattr(record, name)
            and expected != getattr(record, name)
        ):
            raise ValueError("accepted input reply escaped requested scope")
    if isinstance(request, FinishInputRequest) and isinstance(record, InputRecord):
        if (
            record.status != "ready"
            or record.sha256 != request.blob.sha256
            or record.byte_size != request.blob.byte_size
        ):
            raise ValueError("input finish changed its measured output")


def http_error_status(code: ErrorCode) -> int:
    if code == "account_not_active":
        return 401
    if code in {"permission_denied", "task_not_visible", "invalid_account_role"}:
        return 403
    if code in {"source_missing", "input_missing"}:
        return 404
    if code in {"invalid_request", "resource_budget_exceeded"}:
        return 422
    if code == "invalid_response":
        return 502
    if code in {"storage_unavailable", "timeout", "versioning_required"}:
        return 503
    return 409


def contract_document() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    models = [m for op in OPERATIONS.values() for m in (op.request, op.result)] + [
        InputReference,
        _Error,
    ]
    for model in models:
        schema = model.model_json_schema()
        for name, definition in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("conflicting accepted input definition")
            definitions[name] = definition
        if model.__name__ in definitions and definitions[model.__name__] != schema:
            raise ValueError("conflicting accepted input model")
        definitions[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": INPUT_SCHEMA,
        "$defs": definitions,
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "batch_size": BATCH_SIZE,
        },
        "x-native": {
            name: {
                "signature": "aap_assets_v1."
                + name
                + "("
                + ",".join(op.sql_types)
                + ")",
                "arguments": list(op.arguments),
                "request": op.request.__name__,
                "result": op.result.__name__,
                "role": op.role,
            }
            for name, op in OPERATIONS.items()
        },
    }
