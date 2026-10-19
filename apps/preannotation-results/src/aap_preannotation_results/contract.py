"""Portable closed DTOs and native-call validation for durable batch results."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import base64
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

SCHEMA = "aap-preannotation-results/v1"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PART_BYTES = 512 * 1024
MAX_PART_BASE64_CHARS = 699_052
MAX_MEMBER_PAGE = 100
DEFAULT_LEASE_SECONDS = 120
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300
MAX_INT8 = (1 << 63) - 1

ErrorCode = Literal[
    "invalid_request",
    "invalid_response",
    "job_missing",
    "job_not_writable",
    "scope_mismatch",
    "execution_busy",
    "lease_lost",
    "selection_conflict",
    "result_conflict",
    "result_not_ready",
    "result_incomplete",
    "content_deleting",
    "content_epoch_changed",
    "storage_unavailable",
]
Stage = Literal["raw", "prepared"]
JobState = Literal["preparing", "running", "completed", "failed", "cancelled"]
MemberState = Literal[
    "planned",
    "prepared",
    "capturing",
    "captured",
    "ready",
    "applied",
    "failed",
    "skipped",
]
ResultState = Literal[
    "capturing", "captured", "ready", "applied", "failed", "abandoned"
]
FinishStatus = Literal["completed", "failed", "cancelled"]


def _canonical_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("value must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("value must be a canonical UUID") from None
    if str(parsed) != value:
        raise ValueError("value must be a lowercase canonical UUID")
    return parsed


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError:
            raise ValueError("timestamp must be an aware UTC datetime") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be an aware UTC datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        offset = None
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must use UTC")
    return value.astimezone(timezone.utc)


CanonicalUUID = Annotated[UUID, BeforeValidator(_canonical_uuid)]
UtcDateTime = Annotated[AwareDatetime, BeforeValidator(_utc_datetime)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Base64Part = Annotated[
    StrictStr,
    Field(min_length=1, max_length=MAX_PART_BASE64_CHARS),
]
PositiveInt = Annotated[StrictInt, Field(ge=1, le=MAX_INT8)]
NonnegativeInt = Annotated[StrictInt, Field(ge=0, le=MAX_INT8)]
LeaseSeconds = Annotated[StrictInt, Field(ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)]
SmallPositiveInt = Annotated[StrictInt, Field(ge=1, le=MAX_INT8)]


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


def _as_tuple(value: Any, field_name: str) -> tuple:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    raise ValueError(f"{field_name} must be an array")


def _json_value(value: Any, *, seen: set[int] | None = None) -> None:
    """Reject values that cannot be represented as strict JSON without copying."""
    if (
        value is None
        or type(value) in {str, bool, int}
        or isinstance(value, (UUID, datetime))
    ):
        return
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("JSON objects must not contain non-finite numbers")
        return
    if type(value) not in {dict, list, tuple}:
        raise ValueError("value must contain only JSON-compatible data")
    seen = set() if seen is None else seen
    marker = id(value)
    if marker in seen:
        raise ValueError("JSON objects must not contain cycles")
    seen.add(marker)
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("JSON object keys must be strings")
            for child in value.values():
                _json_value(child, seen=seen)
        else:
            for child in value:
                _json_value(child, seen=seen)
    finally:
        seen.remove(marker)


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{field_name} must be an object")
    _json_value(value)
    return value


def _part_content(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_PART_BASE64_CHARS
        or not value.isascii()
    ):
        raise ValueError("encoded part exceeds the base64 size limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("content must be canonical base64") from None
    if (
        not decoded
        or len(decoded) > MAX_PART_BYTES
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise ValueError("decoded part exceeds the byte limit or is not canonical")
    return value


class Scope(Value):
    job_id: CanonicalUUID
    actor_id: CanonicalUUID
    project_id: CanonicalUUID
    request_digest: Digest


class LeaseScope(Scope):
    generation: PositiveInt
    lease_token: CanonicalUUID


class Lease(LeaseScope):
    worker_id: CanonicalUUID
    execution_id: StrictStr | None
    lease_expires_at: UtcDateTime


class Progress(Value):
    job_id: CanonicalUUID
    state: JobState
    total: NonnegativeInt | None
    selection_cursor: NonnegativeInt
    selection_complete: StrictBool
    next_ordinal: PositiveInt = Field(
        description="Current work ordinal; terminal executions use total + 1."
    )
    bulk_overwrite_done: StrictBool
    success_count: NonnegativeInt
    failed_count: NonnegativeInt
    skipped_count: NonnegativeInt
    total_cost: StrictStr
    pipeline_stages: dict[str, dict[str, NonnegativeInt]]
    gpu_arbiter_failures: tuple[dict[str, Any], ...]

    @field_validator("pipeline_stages", mode="before")
    @classmethod
    def stages_are_json_objects(cls, value):
        return _object(value, "pipeline_stages")

    @field_validator("gpu_arbiter_failures", mode="before")
    @classmethod
    def failures_are_tuple(cls, value):
        return _as_tuple(value, "gpu_arbiter_failures")

    @field_validator("gpu_arbiter_failures")
    @classmethod
    def failure_values_are_objects(cls, value):
        for item in value:
            _object(item, "gpu_arbiter_failures item")
        return value


class LabelingWriteScope(Value):
    """Portable shape of the existing frozen LabelingWriteScope dataclass."""

    task_id: CanonicalUUID
    project_id: CanonicalUUID
    actor_id: CanonicalUUID | None
    actor_known: StrictBool
    operation: Literal["preannotation"]
    execution_scope: StrictStr | None
    execution_scope_known: StrictBool
    task_version: NonnegativeInt | None
    task_status: StrictStr
    assignee_id: CanonicalUUID | None
    review_round_id: CanonicalUUID | None
    source_annotation_id: CanonicalUUID | None
    source_annotation_version: NonnegativeInt | None
    expected_batch_id: CanonicalUUID | None
    expected_batch_id_known: StrictBool
    job_id: CanonicalUUID | None
    job_id_known: StrictBool
    source_binding_known: StrictBool
    task_source_generation: PositiveInt | None
    source_dataset_item_id: CanonicalUUID | None
    source_file_path: StrictStr | None
    source_file_type: StrictStr | None
    accepted_input_id: CanonicalUUID | None


class Member(Value):
    ordinal: PositiveInt
    task_id: CanonicalUUID
    write_scope: LabelingWriteScope | None
    manifest_member: dict[str, Any] | None
    failure_id: CanonicalUUID
    overwrite_done: StrictBool
    state: MemberState
    result_id: CanonicalUUID | None
    failed_prediction_id: CanonicalUUID | None
    error_code: StrictStr | None

    @field_validator("manifest_member", mode="before")
    @classmethod
    def manifest_is_json_object(cls, value):
        return None if value is None else _object(value, "manifest_member")

    @model_validator(mode="after")
    def valid_applied_member(self):
        if self.state == "applied" and self.result_id is None:
            raise ValueError("applied member must identify its result")
        return self


class Result(Value):
    result_id: CanonicalUUID
    job_id: CanonicalUUID
    ordinal: PositiveInt
    task_id: CanonicalUUID
    result_count: NonnegativeInt
    raw_digest: Digest
    prepared_digest: Digest | None
    raw_complete: StrictBool
    prepared_complete: StrictBool
    state: ResultState
    context: dict[str, Any]
    receipt_count: NonnegativeInt

    @field_validator("context", mode="before")
    @classmethod
    def context_is_json_object(cls, value):
        return _object(value, "context")


class Receipt(Value):
    id: CanonicalUUID
    created_at: UtcDateTime
    tool_unit_id: StrictStr


class Payload(Value):
    payload_id: CanonicalUUID
    request_index: NonnegativeInt
    prediction_id: CanonicalUUID
    prediction_created_at: UtcDateTime
    stage: Stage
    sha256: Digest
    byte_size: NonnegativeInt
    part_count: SmallPositiveInt
    complete: StrictBool
    tool_unit_id: StrictStr | None
    receipt: Receipt | None


class ClaimBatchResultExecutionRequest(Scope):
    worker_id: CanonicalUUID
    execution_id: StrictStr | None
    lease_seconds: LeaseSeconds = DEFAULT_LEASE_SECONDS

    @field_validator("execution_id")
    @classmethod
    def nonempty_execution_id(cls, value):
        if value is not None and not value:
            raise ValueError("execution_id must not be empty")
        return value


class ClaimBatchResultExecutionResult(Value):
    lease: Lease
    progress: Progress


class HeartbeatBatchResultExecutionRequest(LeaseScope):
    lease_seconds: LeaseSeconds = DEFAULT_LEASE_SECONDS


class HeartbeatBatchResultExecutionResult(Value):
    lease: Lease
    progress: Progress


class ReleaseBatchResultExecutionRequest(LeaseScope):
    pass


class ReleaseBatchResultExecutionResult(Value):
    progress: Progress


class StageMember(Value):
    ordinal: PositiveInt
    task_id: CanonicalUUID
    write_scope: LabelingWriteScope | None
    manifest_member: dict[str, Any] | None

    @field_validator("manifest_member", mode="before")
    @classmethod
    def manifest_is_json_object(cls, value):
        return None if value is None else _object(value, "manifest_member")


class StageBatchResultMembersRequest(LeaseScope):
    after_ordinal: NonnegativeInt
    members: tuple[StageMember, ...]
    complete: StrictBool

    @field_validator("members", mode="before")
    @classmethod
    def members_are_tuple(cls, value):
        return _as_tuple(value, "members")

    @model_validator(mode="after")
    def contiguous_member_page(self):
        if len(self.members) > MAX_MEMBER_PAGE:
            raise ValueError(f"members may contain at most {MAX_MEMBER_PAGE} entries")
        expected = self.after_ordinal + 1
        for member in self.members:
            if member.ordinal != expected:
                raise ValueError("member ordinals must be contiguous and ordered")
            if member.write_scope is not None:
                scope = member.write_scope
                if (
                    scope.task_id != member.task_id
                    or scope.project_id != self.project_id
                    or scope.actor_id != self.actor_id
                    or scope.job_id != self.job_id
                    or not scope.actor_known
                    or not scope.job_id_known
                ):
                    raise ValueError(
                        "LabelingWriteScope does not match execution scope"
                    )
            expected += 1
        return self


class StageBatchResultMembersResult(Value):
    progress: Progress


class StartBatchResultExecutionRequest(LeaseScope):
    pass


class StartBatchResultExecutionResult(Value):
    progress: Progress


class StartBatchResultMemberRequest(LeaseScope):
    ordinal: PositiveInt


class StartBatchResultMemberResult(Value):
    progress: Progress
    member: Member


class ReadBatchResultExecutionRequest(Scope):
    after_ordinal: NonnegativeInt = Field(
        default=0,
        description="Exclusive member cursor; reads return ordinals greater than this value.",
    )
    limit: Annotated[StrictInt, Field(ge=1, le=MAX_MEMBER_PAGE)] = MAX_MEMBER_PAGE


class ReadBatchResultExecutionResult(Value):
    progress: Progress
    members: tuple[Member, ...]
    next_ordinal: PositiveInt | None = Field(
        description=(
            "Exclusive page cursor for the next read: the last returned ordinal, "
            "or null when the selection is drained."
        )
    )

    @field_validator("members", mode="before")
    @classmethod
    def members_are_tuple(cls, value):
        return _as_tuple(value, "members")


class MarkBatchResultOverwriteRequest(LeaseScope):
    ordinal: PositiveInt | None


class MarkBatchResultOverwriteResult(Value):
    progress: Progress
    member: Member | None


class SkipBatchResultMemberRequest(LeaseScope):
    ordinal: PositiveInt
    reason: StrictStr

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value):
        if not value:
            raise ValueError("reason must not be empty")
        return value


class SkipBatchResultMemberResult(Value):
    progress: Progress


class FinishBatchResultExecutionRequest(LeaseScope):
    status: FinishStatus
    error_code: StrictStr | None
    result_extra: dict[str, Any]

    @field_validator("result_extra", mode="before")
    @classmethod
    def result_extra_is_object(cls, value):
        return _object(value, "result_extra")


class FinishBatchResultExecutionResult(Value):
    progress: Progress


class BeginBatchResultRequest(LeaseScope):
    ordinal: PositiveInt
    task_id: CanonicalUUID
    result_count: NonnegativeInt
    bundle_digest: Digest
    stage: Stage
    context: dict[str, Any]
    restart_incomplete: StrictBool = False

    @field_validator("context", mode="before")
    @classmethod
    def context_is_object(cls, value):
        return _object(value, "context")


class BeginBatchResultResult(Value):
    result: Result


class PutBatchResultPartRequest(LeaseScope):
    ordinal: PositiveInt
    stage: Stage
    request_index: NonnegativeInt
    sha256: Digest
    byte_size: NonnegativeInt
    part_count: SmallPositiveInt
    part_index: NonnegativeInt
    content: Base64Part
    tool_unit_id: StrictStr | None

    @field_validator("content")
    @classmethod
    def bounded_canonical_base64(cls, value):
        return _part_content(value)

    @model_validator(mode="after")
    def valid_part_scope(self):
        if self.part_index >= self.part_count:
            raise ValueError("part_index must be less than part_count")
        decoded_size = len(base64.b64decode(self.content, validate=True))
        if (
            self.byte_size < 1
            or self.part_count > self.byte_size
            or decoded_size > self.byte_size
            or (self.part_count == 1 and decoded_size != self.byte_size)
        ):
            raise ValueError("part sizes do not match the declared payload size")
        if self.stage == "raw" and self.tool_unit_id is not None:
            raise ValueError("raw requests cannot carry prepared tool metadata")
        return self


class PutBatchResultPartResult(Value):
    result: Result
    payload: Payload


class SealBatchResultRequest(LeaseScope):
    ordinal: PositiveInt
    stage: Stage


class SealBatchResultResult(Value):
    result: Result


class ReadBatchResultRequest(Scope):
    ordinal: PositiveInt
    stage: Stage = "raw"
    request_index: NonnegativeInt = 0
    part_index: NonnegativeInt | None = None


class ReadBatchResultResult(Value):
    result: Result | None
    payload: Payload | None
    content: Base64Part | None

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value):
        if value is None:
            return None
        return _part_content(value)


class LockBatchResultWriteRequest(LeaseScope):
    ordinal: PositiveInt
    result_id: CanonicalUUID
    prepared_digest: Digest


class LockBatchResultWriteResult(Value):
    result: Result


class FinishBatchResultWriteRequest(LeaseScope):
    ordinal: PositiveInt
    result_id: CanonicalUUID
    prepared_digest: Digest


class FinishBatchResultWriteResult(Value):
    result: Result
    progress: Progress


class FinishBatchResultFailureRequest(LeaseScope):
    ordinal: PositiveInt
    failed_prediction_id: CanonicalUUID | None
    error_code: StrictStr
    context: dict[str, Any]

    @field_validator("context", mode="before")
    @classmethod
    def context_is_object(cls, value):
        return _object(value, "context")


class FinishBatchResultFailureResult(Value):
    progress: Progress


@dataclass(frozen=True, slots=True)
class NativeOperation:
    namespace: Literal["aap_jobs_v1", "aap_labeling_v1"]
    request: type[BaseModel]
    result: type[BaseModel]
    role: Literal["executor", "writer"]


OPERATION = MappingProxyType(
    {
        "claim_batch_result_execution": NativeOperation(
            "aap_jobs_v1",
            ClaimBatchResultExecutionRequest,
            ClaimBatchResultExecutionResult,
            "executor",
        ),
        "heartbeat_batch_result_execution": NativeOperation(
            "aap_jobs_v1",
            HeartbeatBatchResultExecutionRequest,
            HeartbeatBatchResultExecutionResult,
            "executor",
        ),
        "release_batch_result_execution": NativeOperation(
            "aap_jobs_v1",
            ReleaseBatchResultExecutionRequest,
            ReleaseBatchResultExecutionResult,
            "executor",
        ),
        "stage_batch_result_members": NativeOperation(
            "aap_jobs_v1",
            StageBatchResultMembersRequest,
            StageBatchResultMembersResult,
            "executor",
        ),
        "start_batch_result_execution": NativeOperation(
            "aap_jobs_v1",
            StartBatchResultExecutionRequest,
            StartBatchResultExecutionResult,
            "executor",
        ),
        "start_batch_result_member": NativeOperation(
            "aap_jobs_v1",
            StartBatchResultMemberRequest,
            StartBatchResultMemberResult,
            "executor",
        ),
        "read_batch_result_execution": NativeOperation(
            "aap_jobs_v1",
            ReadBatchResultExecutionRequest,
            ReadBatchResultExecutionResult,
            "executor",
        ),
        "mark_batch_result_overwrite": NativeOperation(
            "aap_jobs_v1",
            MarkBatchResultOverwriteRequest,
            MarkBatchResultOverwriteResult,
            "executor",
        ),
        "skip_batch_result_member": NativeOperation(
            "aap_jobs_v1",
            SkipBatchResultMemberRequest,
            SkipBatchResultMemberResult,
            "executor",
        ),
        "finish_batch_result_execution": NativeOperation(
            "aap_jobs_v1",
            FinishBatchResultExecutionRequest,
            FinishBatchResultExecutionResult,
            "executor",
        ),
        "begin_batch_result": NativeOperation(
            "aap_labeling_v1",
            BeginBatchResultRequest,
            BeginBatchResultResult,
            "executor",
        ),
        "put_batch_result_part": NativeOperation(
            "aap_labeling_v1",
            PutBatchResultPartRequest,
            PutBatchResultPartResult,
            "executor",
        ),
        "seal_batch_result": NativeOperation(
            "aap_labeling_v1", SealBatchResultRequest, SealBatchResultResult, "executor"
        ),
        "read_batch_result": NativeOperation(
            "aap_labeling_v1", ReadBatchResultRequest, ReadBatchResultResult, "executor"
        ),
        "lock_batch_result_write": NativeOperation(
            "aap_labeling_v1",
            LockBatchResultWriteRequest,
            LockBatchResultWriteResult,
            "writer",
        ),
        "finish_batch_result_write": NativeOperation(
            "aap_labeling_v1",
            FinishBatchResultWriteRequest,
            FinishBatchResultWriteResult,
            "writer",
        ),
        "finish_batch_result_failure": NativeOperation(
            "aap_labeling_v1",
            FinishBatchResultFailureRequest,
            FinishBatchResultFailureResult,
            "writer",
        ),
    }
)
OPERATIONS = OPERATION


class BatchResultFailure(Exception):
    """Closed protocol failure; database and driver details are never exposed."""

    _CODES = frozenset(ErrorCode.__args__)

    def __init__(self, code: ErrorCode):
        if code not in self._CODES:
            code = "invalid_response"
        self.code = code
        super().__init__(code)


class _Error(Value):
    code: ErrorCode


class _SuccessEnvelope(Value):
    schema_: Literal["aap-preannotation-results/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: dict[str, Any]


class _ErrorEnvelope(Value):
    schema_: Literal["aap-preannotation-results/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: _Error


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_json(raw: bytes):
    if not isinstance(raw, bytes):
        raise ValueError("JSON input must be bytes")
    return json.loads(
        raw.decode("utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )


def _wire_datetime(value: datetime) -> str:
    normalized = _utc_datetime(value)
    return normalized.isoformat(timespec="microseconds")


def _json_default(value: Any):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _wire_datetime(value)
    raise TypeError("value is not part of the portable JSON contract")


def _python_value(value: Any):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=True)
    return value


def json_bytes(value: Any) -> bytes:
    """Serialize one protocol message; callers enforce the message byte limit."""
    return json.dumps(
        _python_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")


def success_envelope(value: BaseModel) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ok": True,
        "data": value.model_dump(mode="python", by_alias=True),
    }


def error_envelope(code: ErrorCode) -> dict[str, Any]:
    if code not in ErrorCode.__args__:
        code = "invalid_response"
    return {"schema": SCHEMA, "ok": False, "error": {"code": code}}


def _decode_envelope(raw: Any) -> dict[str, Any]:
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if isinstance(raw, bytes):
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the size limit")
            document = parse_json(raw)
        elif isinstance(raw, dict):
            document = raw
            if len(json_bytes(document)) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the size limit")
        else:
            raise ValueError("response must be a JSON object")
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            raise ValueError("wrong preannotation result schema")
        if document.get("ok") is True:
            return _SuccessEnvelope.model_validate(document).data
        if document.get("ok") is False:
            envelope = _ErrorEnvelope.model_validate(document)
            raise BatchResultFailure(envelope.error.code)
        raise ValueError("invalid preannotation result envelope")
    except BatchResultFailure:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise BatchResultFailure("invalid_response") from None


def _validate_member_scope(scope: Scope, member: Member) -> None:
    if member.write_scope is None:
        return
    value = member.write_scope
    if (
        value.task_id != member.task_id
        or value.project_id != scope.project_id
        or value.actor_id != scope.actor_id
        or value.job_id != scope.job_id
        or not value.actor_known
        or not value.job_id_known
    ):
        raise BatchResultFailure("invalid_response")


def _validate_scope(operation: str, request: BaseModel, result: BaseModel) -> None:
    if hasattr(result, "progress") and result.progress.job_id != request.job_id:
        raise BatchResultFailure("invalid_response")

    if operation == "claim_batch_result_execution":
        lease = result.lease
        if (
            lease.job_id != request.job_id
            or lease.actor_id != request.actor_id
            or lease.project_id != request.project_id
            or lease.request_digest != request.request_digest
            or lease.worker_id != request.worker_id
            or lease.execution_id != request.execution_id
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "heartbeat_batch_result_execution":
        lease = result.lease
        if (
            lease.job_id != request.job_id
            or lease.actor_id != request.actor_id
            or lease.project_id != request.project_id
            or lease.request_digest != request.request_digest
            or lease.generation != request.generation
            or lease.lease_token != request.lease_token
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "read_batch_result_execution":
        if len(result.members) > request.limit:
            raise BatchResultFailure("invalid_response")
        for member in result.members:
            _validate_member_scope(request, member)
        ordinals = [member.ordinal for member in result.members]
        if ordinals != sorted(set(ordinals)) or any(
            ordinal <= request.after_ordinal for ordinal in ordinals
        ):
            raise BatchResultFailure("invalid_response")
        if result.next_ordinal is not None and (
            not ordinals or result.next_ordinal != ordinals[-1]
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "start_batch_result_member":
        _validate_member_scope(request, result.member)
        if (
            result.member.ordinal != request.ordinal
            or result.member.state not in {"prepared", "capturing", "captured", "ready"}
            or result.progress.next_ordinal != request.ordinal
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "mark_batch_result_overwrite":
        if (request.ordinal is None) != (result.member is None):
            raise BatchResultFailure("invalid_response")
        if result.member is not None:
            _validate_member_scope(request, result.member)
            if result.member.ordinal != request.ordinal:
                raise BatchResultFailure("invalid_response")
    elif operation == "begin_batch_result":
        value = result.result
        digest = value.raw_digest if request.stage == "raw" else value.prepared_digest
        if (
            value.job_id != request.job_id
            or value.ordinal != request.ordinal
            or value.task_id != request.task_id
            or value.result_count != request.result_count
            or digest != request.bundle_digest
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "put_batch_result_part":
        value = result.payload
        if (
            result.result.job_id != request.job_id
            or result.result.ordinal != request.ordinal
            or value.request_index != request.request_index
            or value.stage != request.stage
            or value.sha256 != request.sha256
            or value.byte_size != request.byte_size
            or value.part_count != request.part_count
            or value.tool_unit_id != request.tool_unit_id
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "seal_batch_result":
        value = result.result
        expected_state = "captured" if request.stage == "raw" else "ready"
        if (
            value.job_id != request.job_id
            or value.ordinal != request.ordinal
            or value.state != expected_state
            or not (
                value.raw_complete
                if request.stage == "raw"
                else value.prepared_complete
            )
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "read_batch_result":
        if result.result is None:
            if result.payload is not None or result.content is not None:
                raise BatchResultFailure("invalid_response")
        elif (
            result.result.job_id != request.job_id
            or result.result.ordinal != request.ordinal
        ):
            raise BatchResultFailure("invalid_response")
        if result.payload is None:
            if result.content is not None:
                raise BatchResultFailure("invalid_response")
        else:
            payload = result.payload
            if (
                payload.request_index != request.request_index
                or payload.stage != request.stage
                or (request.part_index is None) != (result.content is None)
                or (
                    request.part_index is not None
                    and request.part_index >= payload.part_count
                )
            ):
                raise BatchResultFailure("invalid_response")
            if result.content is not None:
                from .codec import decode_part

                raw_part = decode_part(result.content)
                if payload.part_count == 1 and (
                    len(raw_part) != payload.byte_size
                    or hashlib.sha256(raw_part).hexdigest() != payload.sha256
                ):
                    raise BatchResultFailure("invalid_response")
    elif operation in {"lock_batch_result_write", "finish_batch_result_write"}:
        value = result.result
        expected_state = (
            {"ready", "applied"} if operation.startswith("lock_") else {"applied"}
        )
        if (
            value.result_id != request.result_id
            or value.job_id != request.job_id
            or value.ordinal != request.ordinal
            or value.prepared_digest != request.prepared_digest
            or value.state not in expected_state
        ):
            raise BatchResultFailure("invalid_response")
    elif operation == "finish_batch_result_execution":
        if result.progress.state != request.status:
            raise BatchResultFailure("invalid_response")


def decode_reply(operation: str, request: BaseModel, raw: Any) -> BaseModel:
    definition = OPERATION.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid preannotation result operation/request pair")
    data = _decode_envelope(raw)
    try:
        result = definition.result.model_validate(data)
        _validate_scope(operation, request, result)
        return result
    except BatchResultFailure:
        raise
    except (ValueError, TypeError, AttributeError):
        raise BatchResultFailure("invalid_response") from None


def _int8send(value: int) -> bytes:
    if type(value) is not int or not -(1 << 63) <= value <= MAX_INT8:
        raise ValueError("bundle value is outside PostgreSQL signed int8")
    return value.to_bytes(8, "big", signed=True)


def _digest_bytes(value: str) -> bytes:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("bundle digest must be lowercase SHA-256")
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise ValueError("bundle digest must be lowercase SHA-256") from None
    if raw.hex() != value:
        raise ValueError("bundle digest must be lowercase SHA-256")
    return raw


def bundle_digest(
    scope: Scope,
    ordinal: int,
    stage: Stage,
    payload_sha256s: Iterable[str],
) -> str:
    """Compute the PostgreSQL-compatible hash chain without a bundle buffer."""
    if stage not in {"raw", "prepared"}:
        raise ValueError("stage must be raw or prepared")
    if type(ordinal) is not int or not 1 <= ordinal <= MAX_INT8:
        raise ValueError("ordinal must be a positive signed int8")

    if isinstance(payload_sha256s, Sequence):
        count = len(payload_sha256s)
        digests = payload_sha256s
        temporary = None
    else:
        import tempfile

        temporary = tempfile.TemporaryFile(mode="w+b")
        count = 0
        try:
            for digest in payload_sha256s:
                temporary.write(_digest_bytes(digest))
                count += 1
                if count > MAX_INT8:
                    raise ValueError("result count is outside PostgreSQL signed int8")
            temporary.seek(0)
        except BaseException:
            temporary.close()
            raise
        digests = None

    state = hashlib.sha256(
        SCHEMA.encode("utf-8")
        + b"\0"
        + stage.encode("utf-8")
        + b"\0"
        + scope.job_id.bytes
        + _int8send(ordinal)
        + _int8send(count)
        + _digest_bytes(scope.request_digest)
    ).digest()
    try:
        if digests is not None:
            iterator = enumerate(digests)
            for index, digest in iterator:
                state = hashlib.sha256(
                    state + _int8send(index) + _digest_bytes(digest)
                ).digest()
        else:
            assert temporary is not None
            for index in range(count):
                digest = temporary.read(32)
                if len(digest) != 32:
                    raise ValueError("digest stream is truncated")
                state = hashlib.sha256(state + _int8send(index) + digest).digest()
    finally:
        if temporary is not None:
            temporary.close()
    if digests is not None and count > MAX_INT8:
        raise ValueError("result count is outside PostgreSQL signed int8")
    return state.hex()


def contract_document() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    models: list[type[BaseModel]] = []
    for operation in OPERATION.values():
        models.extend((operation.request, operation.result))
    models.extend(
        (
            Scope,
            LeaseScope,
            Lease,
            Progress,
            LabelingWriteScope,
            Member,
            Result,
            Payload,
            Receipt,
            _Error,
        )
    )
    for model in dict.fromkeys(models):
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            definitions[name] = definition
        definitions[model.__name__] = schema
    request_names = list(
        dict.fromkeys(operation.request.__name__ for operation in OPERATION.values())
    )
    result_names = list(
        dict.fromkeys(operation.result.__name__ for operation in OPERATION.values())
    )
    definitions["_SuccessEnvelope"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "data"],
        "properties": {
            "schema": {"const": SCHEMA},
            "ok": {"const": True},
            "data": {"oneOf": [{"$ref": f"#/$defs/{name}"} for name in result_names]},
        },
    }
    definitions["_ErrorEnvelope"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "error"],
        "properties": {
            "schema": {"const": SCHEMA},
            "ok": {"const": False},
            "error": {"$ref": "#/$defs/_Error"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://schemas.aap.invalid/preannotation-results/v1",
        "title": "AAP durable preannotation batch results",
        "oneOf": [
            *[{"$ref": f"#/$defs/{name}"} for name in request_names],
            {"$ref": "#/$defs/_SuccessEnvelope"},
            {"$ref": "#/$defs/_ErrorEnvelope"},
        ],
        "x-aap-schema": SCHEMA,
        "x-native-operations": {
            name: {
                "namespace": operation.namespace,
                "request": operation.request.__name__,
                "result": operation.result.__name__,
                "role": operation.role,
                "arguments": ["payload"],
                "sql_types": ["jsonb"],
            }
            for name, operation in OPERATION.items()
        },
        "x-budgets": {
            "request_bytes": MAX_REQUEST_BYTES,
            "response_bytes": MAX_RESPONSE_BYTES,
            "snapshot_part_bytes": MAX_PART_BYTES,
            "snapshot_part_base64_chars": MAX_PART_BASE64_CHARS,
            "member_page": MAX_MEMBER_PAGE,
            "lease_seconds": [MIN_LEASE_SECONDS, MAX_LEASE_SECONDS],
        },
        "x-requests": [{"$ref": f"#/$defs/{name}"} for name in request_names],
        "x-results": [{"$ref": f"#/$defs/{name}"} for name in result_names],
        "$defs": definitions,
    }
