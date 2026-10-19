"""Portable, closed DTOs for durable prediction retry operations."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from . import contract as c

SCHEMA = "aap-prediction-retries/v1"
MAX_REQUEST_BYTES = c.MAX_REQUEST_BYTES
MAX_RESPONSE_BYTES = c.MAX_RESPONSE_BYTES
MAX_INT8 = c.MAX_INT8

ErrorCode = Literal[
    "invalid_request",
    "invalid_response",
    "not_found",
    "permission_denied",
    "account_not_active",
    "already_pending",
    "retry_limit",
    "dismissed",
    "scope_mismatch",
    "execution_busy",
    "lease_lost",
    "job_not_writable",
    "result_conflict",
    "result_not_ready",
    "content_deleting",
    "content_epoch_changed",
    "storage_unavailable",
]

CanonicalUUID = c.CanonicalUUID
UtcDateTime = c.UtcDateTime
Digest = c.Digest
NonemptyString = c.NonemptyString
PositiveInt = c.PositiveInt
NonnegativeInt = c.NonnegativeInt
LeaseSeconds = Annotated[StrictInt, Field(ge=5, le=300)]
DeliveryLimit = Annotated[StrictInt, Field(ge=1, le=100)]
ErrorCodeText = Annotated[StrictStr, Field(min_length=1, max_length=256)]
ReasonText = Annotated[StrictStr, Field(max_length=200)]


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


def _object(value: Any, field_name: str, *, nullable: bool = False):
    if nullable and value is None:
        return None
    try:
        c._json_value(value)
    except ValueError:
        raise ValueError(f"{field_name} must be a JSON object") from None
    if type(value) is not dict:
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _array(value: Any, field_name: str) -> tuple:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise ValueError(f"{field_name} must be an array")


class RetryAttempt(Value):
    job_id: CanonicalUUID
    failed_prediction_id: CanonicalUUID
    actor_id: CanonicalUUID
    project_id: CanonicalUUID
    task_id: CanonicalUUID | None
    ml_backend_id: CanonicalUUID | None
    ml_backend_pool_id: CanonicalUUID | None
    source_extra: dict[str, Any]
    retry_count_at_admission: NonnegativeInt
    write_scope: c.LabelingWriteScope | None = Field(
        json_schema_extra={
            "description": "When present, the scope operation is prediction_retry and its identity matches this attempt.",
            "properties": {"operation": {"const": "prediction_retry"}},
        }
    )
    request_digest: Digest
    execution_queue: StrictStr | None
    state: Literal[
        "accepted", "applying", "failing", "succeeded", "failed", "abandoned"
    ]
    outcome: dict[str, Any] | None

    @field_validator("source_extra", mode="before")
    @classmethod
    def source_extra_is_object(cls, value):
        return _object(value, "source_extra")

    @field_validator("outcome", mode="before")
    @classmethod
    def outcome_is_nullable_object(cls, value):
        return _object(value, "outcome", nullable=True)

    @model_validator(mode="after")
    def retry_scope_matches_attempt(self):
        scope = self.write_scope
        if scope is None:
            return self
        if (
            self.task_id is None
            or scope.operation != "prediction_retry"
            or scope.task_id != self.task_id
            or scope.project_id != self.project_id
            or scope.actor_id != self.actor_id
            or scope.job_id != self.job_id
            or not scope.actor_known
            or not scope.job_id_known
        ):
            raise ValueError("retry write scope does not match attempt identity")
        return self


class RetryDelivery(Value):
    job_id: CanonicalUUID
    failed_prediction_id: CanonicalUUID
    actor_id: CanonicalUUID
    execution_queue: StrictStr | None
    generation: PositiveInt
    lease_token: CanonicalUUID
    lease_expires_at: UtcDateTime


class AdmitPredictionRetryRequest(Value):
    job_id: CanonicalUUID
    failed_prediction_id: CanonicalUUID
    actor_id: CanonicalUUID
    execution_queue: StrictStr | None
    legacy_delivery: StrictBool = False


class AdmitPredictionRetryResult(Value):
    attempt: RetryAttempt
    created: StrictBool


class ReadPredictionRetryRequest(Value):
    job_id: CanonicalUUID
    failed_prediction_id: CanonicalUUID
    actor_id: CanonicalUUID


class ReadPredictionRetryResult(Value):
    attempt: RetryAttempt


class FinishPredictionRetryWriteRequest(c.LeaseScope):
    result_id: CanonicalUUID
    prepared_digest: Digest


class FinishPredictionRetryWriteResult(Value):
    outcome: dict[str, Any]
    receipt: c.Receipt
    progress: c.Progress

    @field_validator("outcome", mode="before")
    @classmethod
    def outcome_is_object(cls, value):
        return _object(value, "outcome")


class FinishPredictionRetryFailureRequest(c.LeaseScope):
    error_code: ErrorCodeText
    reason: ReasonText
    gpu_arbiter_error: dict[str, Any] | None
    context: dict[str, Any]

    @field_validator("gpu_arbiter_error", mode="before")
    @classmethod
    def gpu_error_is_nullable_object(cls, value):
        return _object(value, "gpu_arbiter_error", nullable=True)

    @field_validator("context", mode="before")
    @classmethod
    def context_is_object(cls, value):
        return _object(value, "context")


class FinishPredictionRetryFailureResult(Value):
    outcome: dict[str, Any]
    progress: c.Progress

    @field_validator("outcome", mode="before")
    @classmethod
    def outcome_is_object(cls, value):
        return _object(value, "outcome")


class ClaimPredictionRetryDeliveriesRequest(Value):
    worker_id: CanonicalUUID
    job_id: CanonicalUUID | None = None
    limit: DeliveryLimit = 100
    lease_seconds: LeaseSeconds = 30


class ClaimPredictionRetryDeliveriesResult(Value):
    items: tuple[RetryDelivery, ...]

    @field_validator("items", mode="before")
    @classmethod
    def items_are_tuple(cls, value):
        return _array(value, "items")


class FinishPredictionRetryDeliveryRequest(Value):
    job_id: CanonicalUUID
    generation: PositiveInt
    lease_token: CanonicalUUID
    submitted: StrictBool
    error_code: StrictStr | None


class FinishPredictionRetryDeliveryResult(Value):
    job_id: CanonicalUUID
    submitted: StrictBool


@dataclass(frozen=True, slots=True)
class NativeOperation:
    namespace: Literal["aap_jobs_v1", "aap_labeling_v1"]
    request: type[BaseModel]
    result: type[BaseModel]
    role: Literal["executor", "writer"]


OPERATION = MappingProxyType(
    {
        "admit_prediction_retry": NativeOperation(
            "aap_labeling_v1",
            AdmitPredictionRetryRequest,
            AdmitPredictionRetryResult,
            "writer",
        ),
        "read_prediction_retry": NativeOperation(
            "aap_labeling_v1",
            ReadPredictionRetryRequest,
            ReadPredictionRetryResult,
            "executor",
        ),
        "finish_prediction_retry_write": NativeOperation(
            "aap_labeling_v1",
            FinishPredictionRetryWriteRequest,
            FinishPredictionRetryWriteResult,
            "writer",
        ),
        "finish_prediction_retry_failure": NativeOperation(
            "aap_labeling_v1",
            FinishPredictionRetryFailureRequest,
            FinishPredictionRetryFailureResult,
            "writer",
        ),
        "claim_prediction_retry_deliveries": NativeOperation(
            "aap_jobs_v1",
            ClaimPredictionRetryDeliveriesRequest,
            ClaimPredictionRetryDeliveriesResult,
            "executor",
        ),
        "finish_prediction_retry_delivery": NativeOperation(
            "aap_jobs_v1",
            FinishPredictionRetryDeliveryRequest,
            FinishPredictionRetryDeliveryResult,
            "executor",
        ),
    }
)
OPERATIONS = OPERATION


class PredictionRetryFailure(Exception):
    """Closed retry protocol failure with sanitized provider details."""

    _CODES = frozenset(ErrorCode.__args__)

    def __init__(self, code: ErrorCode):
        if code not in self._CODES:
            code = "invalid_response"
        self.code = code
        super().__init__(code)


RetryFailure = PredictionRetryFailure


class _Error(Value):
    code: ErrorCode


class _SuccessEnvelope(Value):
    schema_: Literal["aap-prediction-retries/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: dict[str, Any]


class _ErrorEnvelope(Value):
    schema_: Literal["aap-prediction-retries/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: _Error


def json_bytes(value: Any) -> bytes:
    return c.json_bytes(value)


def parse_json(raw: bytes):
    return c.parse_json(raw)


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
                raise ValueError("response exceeds size limit")
            document = c.parse_json(raw)
        elif isinstance(raw, dict):
            document = raw
            if len(json_bytes(document)) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds size limit")
        else:
            raise ValueError("response must be an object")
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            raise ValueError("wrong retry schema")
        if document.get("ok") is True:
            return _SuccessEnvelope.model_validate(document).data
        if document.get("ok") is False:
            envelope = _ErrorEnvelope.model_validate(document)
            raise PredictionRetryFailure(envelope.error.code)
        raise ValueError("invalid retry envelope")
    except PredictionRetryFailure:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PredictionRetryFailure("invalid_response") from None


def _validate_scope(operation: str, request: BaseModel, result: BaseModel) -> None:
    if hasattr(result, "progress") and result.progress.job_id != request.job_id:
        raise PredictionRetryFailure("invalid_response")

    if operation == "admit_prediction_retry":
        attempt = result.attempt
        if (
            attempt.job_id != request.job_id
            or attempt.failed_prediction_id != request.failed_prediction_id
            or attempt.actor_id != request.actor_id
            or attempt.execution_queue != request.execution_queue
        ):
            raise PredictionRetryFailure("invalid_response")
    elif operation == "read_prediction_retry":
        attempt = result.attempt
        if (
            attempt.job_id != request.job_id
            or attempt.failed_prediction_id != request.failed_prediction_id
            or attempt.actor_id != request.actor_id
        ):
            raise PredictionRetryFailure("invalid_response")
    elif operation == "finish_prediction_retry_write":
        if result.progress.state != "completed":
            raise PredictionRetryFailure("invalid_response")
    elif operation == "finish_prediction_retry_failure":
        if result.progress.state != "failed":
            raise PredictionRetryFailure("invalid_response")
    elif operation == "claim_prediction_retry_deliveries":
        if len(result.items) > request.limit:
            raise PredictionRetryFailure("invalid_response")
        job_ids = [item.job_id for item in result.items]
        if len(set(job_ids)) != len(job_ids):
            raise PredictionRetryFailure("invalid_response")
        if request.job_id is not None and any(
            item.job_id != request.job_id for item in result.items
        ):
            raise PredictionRetryFailure("invalid_response")
    elif operation == "finish_prediction_retry_delivery":
        if result.job_id != request.job_id or result.submitted != request.submitted:
            raise PredictionRetryFailure("invalid_response")


def decode_reply(operation: str, request: BaseModel, raw: Any) -> BaseModel:
    definition = OPERATION.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid prediction retry operation/request pair")
    data = _decode_envelope(raw)
    try:
        result = definition.result.model_validate(data)
        _validate_scope(operation, request, result)
        return result
    except PredictionRetryFailure:
        raise
    except (ValueError, TypeError, AttributeError):
        raise PredictionRetryFailure("invalid_response") from None


def contract_document() -> dict[str, Any]:
    models: list[type[BaseModel]] = []
    for operation in OPERATION.values():
        models.extend((operation.request, operation.result))
    models.extend(
        (
            RetryAttempt,
            RetryDelivery,
            c.LabelingWriteScope,
            c.LeaseScope,
            c.Progress,
            c.Receipt,
            _Error,
        )
    )
    definitions: dict[str, Any] = {}
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
        "$id": "https://schemas.aap.invalid/prediction-retries/v1",
        "title": "AAP durable prediction retries",
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
            "snapshot_part_bytes": c.MAX_PART_BYTES,
            "snapshot_part_base64_chars": c.MAX_PART_BASE64_CHARS,
            "lease_seconds": [5, 300],
            "delivery_limit": [1, 100],
        },
        "x-requests": [{"$ref": f"#/$defs/{name}"} for name in request_names],
        "x-results": [{"$ref": f"#/$defs/{name}"} for name in result_names],
        "$defs": definitions,
    }
