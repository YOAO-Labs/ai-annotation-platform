"""Closed JSON contract for Mask write fences and bounded garbage collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from types import MappingProxyType
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from aap_assets_mask_content.contract import validate_object_key
from aap_assets_mask_content.gc_contract import (
    DeleteOperationStatus,
    GcScanPage,
    GcScanRequest,
    MAX_GC_PAGE,
)

SCHEMA = "aap-labeling-mask-gc/v1"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_OBJECT_KEYS = 1000
MAX_SCAN_PAGES = 20
MAX_DELETE_LIMIT = 100

ErrorCode = Literal[
    "invalid_request",
    "invalid_response",
    "content_deleting",
    "content_epoch_changed",
    "lease_lost",
    "cursor_conflict",
    "idempotency_conflict",
    "storage_unavailable",
]
DeleteState = Literal["pending", "indeterminate", "completed"]
DeleteErrorCode = Literal["peer_unavailable", "invalid_response"]


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


ObjectKey = Annotated[StrictStr, Field(min_length=1, max_length=255)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]
Epoch = Annotated[StrictInt, Field(ge=0)]
LeaseSeconds = Annotated[StrictInt, Field(ge=5, le=300)]
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]


def _uuid(value):
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("value must be a canonical UUID")
    parsed = UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("value must be a canonical UUID")
    return parsed


def _aware_datetime(value):
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(normalized)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _cursor(value):
    return GcScanRequest(after_key=value).after_key


def _object_keys(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("object_keys must be an array")
    if not 1 <= len(values) <= MAX_OBJECT_KEYS:
        raise ValueError(f"object_keys must contain 1-{MAX_OBJECT_KEYS} keys")
    if any(not isinstance(value, str) for value in values):
        raise ValueError("object_keys must contain strings")
    normalized = tuple(validate_object_key(value) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("object_keys must be unique")
    return normalized


class MaskGcFailure(Exception):
    """Closed failure value; raw database or provider messages are never stored."""

    _CODES = frozenset(ErrorCode.__args__)

    def __init__(self, code: ErrorCode):
        if code not in self._CODES:
            code = "invalid_response"
        self.code = code
        super().__init__(code)


class MaskWriteEpoch(Value):
    object_key: ObjectKey
    epoch: Epoch

    @field_validator("object_key")
    @classmethod
    def canonical_key(cls, value):
        return validate_object_key(value)


class MaskWriteEpochs(Value):
    items: tuple[MaskWriteEpoch, ...]

    @field_validator("items", mode="before")
    @classmethod
    def list_to_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def unique_keys(self):
        keys = [item.object_key for item in self.items]
        if len(keys) > MAX_OBJECT_KEYS or len(set(keys)) != len(keys):
            raise ValueError("write epoch keys must be unique and bounded")
        return self


class ReadMaskWriteEpochsRequest(Value):
    object_keys: tuple[ObjectKey, ...]

    @field_validator("object_keys", mode="before")
    @classmethod
    def canonical_keys(cls, value):
        return _object_keys(value)


class BeginMaskContentWriteRequest(Value):
    object_keys: tuple[ObjectKey, ...]

    @field_validator("object_keys", mode="before")
    @classmethod
    def canonical_keys(cls, value):
        return _object_keys(value)


class LockMaskWriteEpochsRequest(Value):
    items: tuple[MaskWriteEpoch, ...]

    @field_validator("items", mode="before")
    @classmethod
    def list_to_tuple(cls, value):
        if not isinstance(value, (list, tuple)):
            raise ValueError("items must be an array")
        return tuple(
            item
            if isinstance(item, MaskWriteEpoch)
            else MaskWriteEpoch.model_validate(item)
            for item in value
        )

    @model_validator(mode="after")
    def bounded_unique_items(self):
        keys = [item.object_key for item in self.items]
        if not 1 <= len(keys) <= MAX_OBJECT_KEYS or len(set(keys)) != len(keys):
            raise ValueError("write epoch items must be non-empty, unique and bounded")
        return self


class RegisterTrackerRunningMaskRootRequest(Value):
    """Retain one generated Mask under a currently-running tracker job."""

    job_id: UUID
    task_id: UUID
    object_key: ObjectKey

    @field_validator("job_id", "task_id", mode="before")
    @classmethod
    def canonical_ids(cls, value):
        return _uuid(value)

    @field_validator("object_key")
    @classmethod
    def canonical_key(cls, value):
        return validate_object_key(value)


class RegisterTrackerRunningMaskRootResult(Value):
    job_id: UUID
    task_id: UUID
    object_key: ObjectKey
    registered: Literal[True]

    @field_validator("job_id", "task_id", mode="before")
    @classmethod
    def canonical_ids(cls, value):
        return _uuid(value)

    @field_validator("object_key")
    @classmethod
    def canonical_key(cls, value):
        return validate_object_key(value)


class ClaimMaskGcSweepRequest(Value):
    worker_id: UUID
    lease_seconds: LeaseSeconds = 30

    @field_validator("worker_id", mode="before")
    @classmethod
    def canonical_worker_id(cls, value):
        return _uuid(value)


class SweepClaim(Value):
    sweep_id: UUID
    generation: PositiveInt
    lease_token: UUID
    lease_expires_at: AwareDatetime
    cutoff: AwareDatetime
    after_key: Annotated[StrictStr, Field(min_length=1, max_length=1024)] | None
    complete: StrictBool

    @field_validator("sweep_id", "lease_token", mode="before")
    @classmethod
    def canonical_id(cls, value):
        return _uuid(value)

    @field_validator("lease_expires_at", "cutoff", mode="before")
    @classmethod
    def aware_timestamps(cls, value):
        return _aware_datetime(value)

    @field_validator("after_key")
    @classmethod
    def bounded_cursor(cls, value):
        return _cursor(value)


class ClaimMaskGcSweepResult(Value):
    sweep: SweepClaim | None


class StageMaskGcPageRequest(Value):
    sweep_id: UUID
    generation: PositiveInt
    lease_token: UUID
    after_key: Annotated[StrictStr, Field(min_length=1, max_length=1024)] | None
    page: GcScanPage

    @field_validator("sweep_id", "lease_token", mode="before")
    @classmethod
    def canonical_id(cls, value):
        return _uuid(value)

    @field_validator("after_key")
    @classmethod
    def bounded_cursor(cls, value):
        return _cursor(value)


class StageMaskGcPageResult(Value):
    sweep: SweepClaim
    scanned: Annotated[StrictInt, Field(ge=0)]
    staged: Annotated[StrictInt, Field(ge=0)]
    skipped: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def staged_counts_fit_scan(self):
        if self.staged + self.skipped > self.scanned:
            raise ValueError("staged and skipped counts exceed scanned keys")
        return self


class ClaimMaskGcDeletesRequest(Value):
    worker_id: UUID
    limit: Annotated[StrictInt, Field(ge=1, le=MAX_DELETE_LIMIT)] = 100
    lease_seconds: LeaseSeconds = 30

    @field_validator("worker_id", mode="before")
    @classmethod
    def canonical_worker_id(cls, value):
        return _uuid(value)


class DeleteClaim(Value):
    operation_id: UUID
    object_key: ObjectKey
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    last_modified: AwareDatetime
    byte_size: Annotated[StrictInt, Field(ge=0)]
    epoch: Epoch
    generation: PositiveInt
    lease_token: UUID
    lease_expires_at: AwareDatetime

    @field_validator("operation_id", "lease_token", mode="before")
    @classmethod
    def canonical_id(cls, value):
        return _uuid(value)

    @field_validator("object_key")
    @classmethod
    def canonical_key(cls, value):
        return validate_object_key(value)

    @field_validator("last_modified", "lease_expires_at", mode="before")
    @classmethod
    def aware_timestamps(cls, value):
        return _aware_datetime(value)


class ClaimMaskGcDeletesResult(Value):
    items: tuple[DeleteClaim, ...]

    @field_validator("items", mode="before")
    @classmethod
    def list_to_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def bounded_unique_claims(self):
        if len(self.items) > MAX_DELETE_LIMIT:
            raise ValueError("delete claim batch exceeds its maximum size")
        operation_ids = [item.operation_id for item in self.items]
        object_keys = [item.object_key for item in self.items]
        if len(set(operation_ids)) != len(operation_ids) or len(
            set(object_keys)
        ) != len(object_keys):
            raise ValueError("delete claims must be unique by operation and object")
        return self


class FinishMaskGcDeleteRequest(Value):
    operation_id: UUID
    generation: PositiveInt
    lease_token: UUID
    receipt: DeleteOperationStatus | None
    error_code: DeleteErrorCode | None

    @field_validator("operation_id", "lease_token", mode="before")
    @classmethod
    def canonical_id(cls, value):
        return _uuid(value)

    @model_validator(mode="after")
    def valid_evidence(self):
        if (self.receipt is None) != (self.error_code is not None):
            raise ValueError(
                "supply either a status receipt or a controlled error code"
            )
        if self.receipt is not None and self.receipt.operation_id != self.operation_id:
            raise ValueError("receipt operation_id must match the claim")
        return self


class FinishMaskGcDeleteResult(Value):
    operation_id: UUID
    state: DeleteState
    completed: StrictBool

    @field_validator("operation_id", mode="before")
    @classmethod
    def canonical_id(cls, value):
        return _uuid(value)

    @model_validator(mode="after")
    def completion_matches_state(self):
        if self.completed != (self.state == "completed"):
            raise ValueError("completed must match the terminal state")
        return self


class ReadMaskGcProgressRequest(Value):
    pass


class ReadMaskGcProgressResult(Value):
    active_intents: Annotated[StrictInt, Field(ge=0)]
    indeterminate_intents: Annotated[StrictInt, Field(ge=0)]
    completed_intents: Annotated[StrictInt, Field(ge=0)]
    sweep: SweepClaim | None


class _NativeError(Value):
    code: ErrorCode


class _SuccessEnvelope(Value):
    schema_: Literal["aap-labeling-mask-gc/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: dict


class _ErrorEnvelope(Value):
    schema_: Literal["aap-labeling-mask-gc/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: _NativeError


@dataclass(frozen=True)
class NativeOperation:
    """Function-only metadata consumed by native SQL adapters."""

    request: type[BaseModel]
    result: type[BaseModel]
    role: Literal["labeling", "cleanup"]
    arguments: tuple[str, ...] = ("payload",)
    sql_types: tuple[str, ...] = ("jsonb",)


OPERATIONS = MappingProxyType(
    {
        "read_mask_write_epochs": NativeOperation(
            ReadMaskWriteEpochsRequest, MaskWriteEpochs, "labeling"
        ),
        "begin_mask_content_write": NativeOperation(
            BeginMaskContentWriteRequest, MaskWriteEpochs, "labeling"
        ),
        "lock_mask_write_epochs": NativeOperation(
            LockMaskWriteEpochsRequest, MaskWriteEpochs, "labeling"
        ),
        "register_tracker_running_mask_root": NativeOperation(
            RegisterTrackerRunningMaskRootRequest,
            RegisterTrackerRunningMaskRootResult,
            "labeling",
        ),
        "claim_mask_gc_sweep": NativeOperation(
            ClaimMaskGcSweepRequest, ClaimMaskGcSweepResult, "cleanup"
        ),
        "stage_mask_gc_page": NativeOperation(
            StageMaskGcPageRequest, StageMaskGcPageResult, "cleanup"
        ),
        "claim_mask_gc_deletes": NativeOperation(
            ClaimMaskGcDeletesRequest, ClaimMaskGcDeletesResult, "cleanup"
        ),
        "finish_mask_gc_delete": NativeOperation(
            FinishMaskGcDeleteRequest, FinishMaskGcDeleteResult, "cleanup"
        ),
        "read_mask_gc_progress": NativeOperation(
            ReadMaskGcProgressRequest, ReadMaskGcProgressResult, "cleanup"
        ),
    }
)


class RunRequest(Value):
    scan_pages: Annotated[StrictInt, Field(ge=1, le=MAX_SCAN_PAGES)] = 1
    delete_limit: Annotated[StrictInt, Field(ge=1, le=MAX_DELETE_LIMIT)] = 100


class ProgressRequest(Value):
    pass


class CycleStats(Value):
    scan_pages: NonnegativeInt = 0
    scanned: NonnegativeInt = 0
    staged: NonnegativeInt = 0
    skipped: NonnegativeInt = 0
    scan_errors: NonnegativeInt = 0
    delete_claims: NonnegativeInt = 0
    completed: NonnegativeInt = 0
    deleted: NonnegativeInt = 0
    missing: NonnegativeInt = 0
    content_changed: NonnegativeInt = 0
    pending: NonnegativeInt = 0
    indeterminate: NonnegativeInt = 0
    durable_uncertainties: NonnegativeInt = 0
    finish_errors: NonnegativeInt = 0
    data_errors: NonnegativeInt = 0


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


def json_bytes(value) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode_envelope(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    elif isinstance(raw, dict):
        raw = json_bytes(raw)
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise MaskGcFailure("invalid_response")
    try:
        document = parse_json(raw)
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            raise ValueError("wrong Labeling Mask GC schema")
        if document.get("ok") is True:
            return _SuccessEnvelope.model_validate(document).data
        if document.get("ok") is False:
            envelope = _ErrorEnvelope.model_validate(document)
            raise MaskGcFailure(envelope.error.code)
        raise ValueError("invalid Labeling Mask GC envelope")
    except MaskGcFailure:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise MaskGcFailure("invalid_response") from None


def _same_epoch_scope(request: BaseModel, result: MaskWriteEpochs) -> None:
    requested = (
        request.object_keys
        if isinstance(
            request, (ReadMaskWriteEpochsRequest, BeginMaskContentWriteRequest)
        )
        else tuple(item.object_key for item in request.items)
    )
    actual = tuple(item.object_key for item in result.items)
    if len(actual) != len(requested) or set(actual) != set(requested):
        raise MaskGcFailure("invalid_response")
    if isinstance(request, LockMaskWriteEpochsRequest):
        expected = {item.object_key: item.epoch for item in request.items}
        if any(expected[item.object_key] != item.epoch for item in result.items):
            raise MaskGcFailure("invalid_response")


def validate_scope(operation: str, request: BaseModel, result: BaseModel) -> None:
    if operation in {
        "read_mask_write_epochs",
        "begin_mask_content_write",
        "lock_mask_write_epochs",
    }:
        _same_epoch_scope(request, result)
    elif operation == "stage_mask_gc_page":
        if (
            result.sweep.sweep_id != request.sweep_id
            or result.sweep.generation != request.generation
            or result.sweep.lease_token != request.lease_token
            or result.sweep.after_key != request.page.after_key
            or result.sweep.complete != request.page.complete
        ):
            raise MaskGcFailure("invalid_response")
        if result.scanned < len(request.page.items):
            raise MaskGcFailure("invalid_response")
    elif operation == "register_tracker_running_mask_root":
        if (
            result.job_id != request.job_id
            or result.task_id != request.task_id
            or result.object_key != request.object_key
            or result.registered is not True
        ):
            raise MaskGcFailure("invalid_response")
    elif operation == "claim_mask_gc_deletes":
        if len(result.items) > request.limit:
            raise MaskGcFailure("invalid_response")
        operation_ids = [item.operation_id for item in result.items]
        object_keys = [item.object_key for item in result.items]
        if len(set(operation_ids)) != len(operation_ids) or len(
            set(object_keys)
        ) != len(object_keys):
            raise MaskGcFailure("invalid_response")
    elif operation == "finish_mask_gc_delete":
        if result.operation_id != request.operation_id:
            raise MaskGcFailure("invalid_response")
        expected_completed = (
            request.receipt is not None and request.receipt.state == "completed"
        )
        if result.completed != expected_completed:
            raise MaskGcFailure("invalid_response")


def decode_reply(operation: str, request: BaseModel, raw) -> BaseModel:
    definition = OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid Labeling Mask GC operation/request pair")
    data = _decode_envelope(raw)
    try:
        result = definition.result.model_validate(data)
        validate_scope(operation, request, result)
        return result
    except MaskGcFailure:
        raise
    except (ValueError, TypeError, AttributeError):
        raise MaskGcFailure("invalid_response") from None


def contract_document() -> dict:
    definitions = {}
    models = [
        model
        for operation in OPERATIONS.values()
        for model in (operation.request, operation.result)
    ]
    models.extend(
        (SweepClaim, DeleteClaim, RunRequest, ProgressRequest, CycleStats, _NativeError)
    )
    for model in models:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            definitions[name] = definition
        definitions[model.__name__] = schema
    request_names = list(
        dict.fromkeys(op.request.__name__ for op in OPERATIONS.values())
    )
    request_names.extend((RunRequest.__name__, ProgressRequest.__name__))
    result_names = list(dict.fromkeys(op.result.__name__ for op in OPERATIONS.values()))
    result_names.append(CycleStats.__name__)
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
            "error": {"$ref": "#/$defs/_HttpError"},
        },
    }
    definitions["_HttpError"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["code"],
        "properties": {
            "code": {
                "type": "string",
                "enum": sorted((*ErrorCode.__args__, "permission_denied")),
            }
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://schemas.aap.invalid/labeling-mask-gc/v1",
        "title": "AAP Labeling Mask GC native and HTTP calls",
        "oneOf": [
            *({"$ref": f"#/$defs/{name}"} for name in request_names),
            {"$ref": "#/$defs/_SuccessEnvelope"},
            {"$ref": "#/$defs/_ErrorEnvelope"},
        ],
        "x-aap-schema": SCHEMA,
        "x-native-operations": {
            name: {
                "request": operation.request.__name__,
                "result": operation.result.__name__,
                "arguments": list(operation.arguments),
                "sql_types": list(operation.sql_types),
                "role": operation.role,
            }
            for name, operation in OPERATIONS.items()
        },
        "x-http-operations": {
            "run": {
                "method": "POST",
                "path": "/v1/mask-gc/run",
                "request": "RunRequest",
                "result": "CycleStats",
            },
            "progress": {
                "method": "POST",
                "path": "/v1/mask-gc/progress",
                "request": "ProgressRequest",
                "result": "ReadMaskGcProgressResult",
            },
        },
        "x-budgets": {
            "request_bytes": MAX_REQUEST_BYTES,
            "response_bytes": MAX_RESPONSE_BYTES,
            "object_keys": MAX_OBJECT_KEYS,
            "gc_page": MAX_GC_PAGE,
            "scan_pages_per_cycle": MAX_SCAN_PAGES,
            "delete_claims_per_cycle": MAX_DELETE_LIMIT,
        },
        "x-requests": [{"$ref": f"#/$defs/{name}"} for name in request_names],
        "x-results": [{"$ref": f"#/$defs/{name}"} for name in result_names],
        "$defs": definitions,
    }
