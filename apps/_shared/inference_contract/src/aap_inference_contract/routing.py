"""Closed private wire contracts for durable backend routing authority."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

ROUTING_PROTOCOL = "aap-inference-routing/v1"
ROUTING_RESULT_PROTOCOL = "aap-inference-routing-result/v1"
ROUTING_STATUS_PROTOCOL = "aap-inference-routing-status/v1"
ROUTING_RUNTIME_PROTOCOL = "aap-inference-routing-runtime/v1"
ROUTING_RUNTIME_RESULT_PROTOCOL = "aap-inference-routing-runtime-result/v1"
ROUTING_QUIESCENCE_PROTOCOL = "aap-inference-routing-quiescence/v1"
ROUTING_QUIESCENCE_RESULT_PROTOCOL = "aap-inference-routing-quiescence-result/v1"
ROUTING_NATIVE_POLICY_SCHEMA = "aap-inference-routing-native/v1"
ROUTING_DATA_PROTOCOL = "aap-inference-routing-data/v1"
ROUTING_REQUEST_MAX_BYTES = 1024 * 1024
ROUTING_REPLY_MAX_BYTES = 1024 * 1024
ROUTING_NATIVE_FUNCTIONS = (
    "aap_inference_routing_v1.routing_policy()",
    "aap_inference_routing_v1.routing_step(text,text)",
)
ROUTING_NATIVE_ACTIONS = (
    "command_status",
    "record_command",
    "admission_snapshot",
    "reserve_scope",
    "scope_status",
    "begin_attempt",
    "settle_attempt",
    "heartbeat_scope",
    "close_scope",
    "quiescence",
    "prepare_control",
    "finish_control",
    "prepare_epoch",
    "finish_epoch",
    "capture_transport_subject",
    "open_transport_subject",
    "begin_control_attempt",
    "settle_control_attempt",
    "runtime_snapshot",
)

RoutingOperation = Literal[
    "acquire",
    "video_acquire",
    "begin",
    "settle",
    "heartbeat",
    "close",
    "scope_status",
    "reconcile",
    "prepare_control",
    "finish_control",
    "begin_control",
    "settle_control",
]
RoutingState = Literal["caller_pending", "committed", "unknown"]
_OPERATIONS = (
    "acquire",
    "video_acquire",
    "begin",
    "settle",
    "heartbeat",
    "close",
    "scope_status",
    "reconcile",
    "prepare_control",
    "finish_control",
    "begin_control",
    "settle_control",
)
_REASONS = frozenset(
    {
        "pool_unavailable",
        "pool_not_enabled",
        "pool_saturated",
        "generation_mismatch",
        "scope_unavailable",
        "scope_expired",
        "scope_closed",
        "transport_changed",
        "management_busy",
        "member_not_draining",
        "member_not_quiescent",
        "continuity_unverified",
        "backend_not_ready",
        "invalid_command",
    }
)
_POLICY_KEYS = frozenset(
    {"schema", "data_protocol", "transaction", "functions", "actions", "ready"}
)
_JSON_ERRORS = (
    UnicodeError,
    ValueError,
    TypeError,
    RecursionError,
    OverflowError,
    ValidationError,
)


class RoutingUnavailable(Exception):
    """The selected durable routing owner is unavailable or malformed."""

    def __init__(self) -> None:
        super().__init__("Inference routing owner unavailable")


class _RoutingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _AcquireBody(_RoutingModel):
    pool_id: uuid.UUID | None
    registry_id: uuid.UUID | None
    project_id: uuid.UUID | None
    mode: Literal["off", "observe", "enforce", "direct"]
    required_instance_id: uuid.UUID | None
    owner: StrictStr = Field(min_length=1, max_length=512)
    operation: StrictStr = Field(min_length=1, max_length=96)


class RoutingVideoExecutionProof(_RoutingModel):
    job_id: uuid.UUID
    run_id: uuid.UUID
    worker_id: uuid.UUID
    generation: StrictInt = Field(ge=1, le=(1 << 63) - 1)
    lease_token: uuid.UUID = Field(repr=False)

    @field_validator("job_id", "run_id", "worker_id", "lease_token", mode="before")
    @classmethod
    def canonical_identity(cls, value):
        if type(value) is uuid.UUID:
            return value
        if type(value) is not str or not _canonical_wire_uuid(value):
            raise ValueError("Invalid video execution identity")
        return uuid.UUID(value)


class _VideoAcquireBody(_AcquireBody):
    execution: RoutingVideoExecutionProof = Field(repr=False)

    @model_validator(mode="after")
    def exact_tracker_subject(self):
        if (
            self.operation != "tracker"
            or self.owner != f"tracker:{self.execution.job_id}"
            or self.project_id is None
        ):
            raise ValueError("Invalid video execution subject")
        return self


class _BeginBody(_RoutingModel):
    scope_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)
    attempt_id: uuid.UUID
    method: StrictStr = Field(min_length=1, max_length=32)
    path: StrictStr = Field(min_length=1, max_length=4096)
    transport_subject: StrictStr = Field(
        min_length=1, max_length=ROUTING_REQUEST_MAX_BYTES, repr=False
    )

    @model_validator(mode="after")
    def valid_http_target(self) -> _BeginBody:
        _validate_http_target(self.method, self.path)
        return self


class _SettleBody(_RoutingModel):
    scope_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)
    attempt_id: uuid.UUID
    kind: Literal["response", "uncertain"]
    status_code: StrictInt | None
    reason: StrictStr | None = Field(max_length=96)

    @model_validator(mode="after")
    def valid_attempt_result(self) -> _SettleBody:
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ValueError("Invalid routing attempt status")
        _validate_shortcode(self.reason)
        return self


class _HeartbeatBody(_RoutingModel):
    scope_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)


class _CloseBody(_RoutingModel):
    scope_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)
    outcome: Literal["success", "error", "cancel"]


class _ScopeStatusBody(_RoutingModel):
    scope_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)


class RoutingExpectedTopology(_RoutingModel):
    registry_id: uuid.UUID
    member_id: uuid.UUID | None
    pool_id: uuid.UUID | None
    routing_generation: StrictInt | None
    traffic_state: Literal["active", "draining", "disabled"] | None

    @model_validator(mode="after")
    def positive_generation(self) -> RoutingExpectedTopology:
        if self.routing_generation is not None and self.routing_generation < 1:
            raise ValueError("Invalid routing generation")
        return self


class _ReconcileBody(_RoutingModel):
    registry_id: uuid.UUID
    control_id: uuid.UUID


class _PrepareControlBody(_RoutingModel):
    registry_id: uuid.UUID
    control_id: uuid.UUID
    kind: Literal["unload", "reload"]
    expected_topology: RoutingExpectedTopology | None


class _FinishControlBody(_RoutingModel):
    control_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)


class _BeginControlBody(_RoutingModel):
    control_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)
    attempt_id: uuid.UUID
    method: StrictStr = Field(min_length=1, max_length=32)
    path: StrictStr = Field(min_length=1, max_length=4096)
    transport_subject: StrictStr = Field(
        min_length=1, max_length=ROUTING_REQUEST_MAX_BYTES, repr=False
    )

    @model_validator(mode="after")
    def valid_http_target(self) -> _BeginControlBody:
        _validate_http_target(self.method, self.path)
        return self


class _SettleControlBody(_RoutingModel):
    control_id: uuid.UUID
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)
    attempt_id: uuid.UUID
    kind: Literal["response", "uncertain"]
    status_code: StrictInt | None
    reason: StrictStr | None = Field(max_length=96)

    @model_validator(mode="after")
    def valid_attempt_result(self) -> _SettleControlBody:
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ValueError("Invalid routing attempt status")
        _validate_shortcode(self.reason)
        return self


_COMMAND_BODY_TYPES: dict[str, type[_RoutingModel]] = {
    "acquire": _AcquireBody,
    "video_acquire": _VideoAcquireBody,
    "begin": _BeginBody,
    "settle": _SettleBody,
    "heartbeat": _HeartbeatBody,
    "close": _CloseBody,
    "scope_status": _ScopeStatusBody,
    "reconcile": _ReconcileBody,
    "prepare_control": _PrepareControlBody,
    "finish_control": _FinishControlBody,
    "begin_control": _BeginControlBody,
    "settle_control": _SettleControlBody,
}


def _validate_shortcode(value: str | None) -> None:
    if value is not None and (
        not value
        or len(value) > 96
        or value[0] not in "abcdefghijklmnopqrstuvwxyz"
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in value
        )
    ):
        raise ValueError("Invalid routing reason code")


def _validate_http_target(method: str, path: str) -> None:
    token_characters = "!#$%&'*+-.^_`|~"
    if (
        not method.isascii()
        or any(
            not character.isalnum() and character not in token_characters
            for character in method
        )
        or not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or any(ord(character) < 33 or ord(character) == 127 for character in path)
    ):
        raise ValueError("Invalid routing HTTP method or path")


def _body_adapter(operation: str) -> TypeAdapter:
    try:
        return TypeAdapter(_COMMAND_BODY_TYPES[operation])
    except KeyError:
        raise ValueError("Invalid routing operation") from None


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if type(value) is list:
        return [_json_ready(child) for child in value]
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Invalid JSON value")


def _json_input(value: object, *, max_bytes: int) -> tuple[bytes, dict[str, object]]:
    if isinstance(value, bytes):
        encoded = value
    elif type(value) is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise RoutingUnavailable() from None
    else:
        try:
            encoded = json.dumps(
                _json_ready(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise RoutingUnavailable() from None
    if len(encoded) > max_bytes:
        raise RoutingUnavailable()
    try:
        wire_text = encoded.decode("utf-8")
        parsed = json.loads(
            wire_text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None
    if type(parsed) is not dict:
        raise RoutingUnavailable()
    return encoded, parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate routing field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _typed_body(operation: str, body: object) -> _RoutingModel:
    try:
        encoded = json.dumps(
            _json_ready(body),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _body_adapter(operation).validate_json(encoded)
    except _JSON_ERRORS:
        raise ValueError("Invalid routing operation body") from None


class RoutingCommand(_RoutingModel):
    schema_: Literal["aap-inference-routing/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    operation: RoutingOperation
    body: Any = Field(repr=False)

    @model_validator(mode="after")
    def closed_body(self) -> RoutingCommand:
        object.__setattr__(self, "body", _typed_body(self.operation, self.body))
        return self


class RoutingStatusRequest(_RoutingModel):
    schema_: Literal["aap-inference-routing-status/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    command_sha256: StrictStr
    operation: RoutingOperation

    @model_validator(mode="after")
    def valid_digest(self) -> RoutingStatusRequest:
        _validate_digest(self.command_sha256)
        return self


class RoutingRuntimeRequest(_RoutingModel):
    schema_: Literal["aap-inference-routing-runtime/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    registry_ids: list[uuid.UUID] = Field(max_length=512)

    @model_validator(mode="after")
    def unique_registry_ids(self) -> RoutingRuntimeRequest:
        if len(set(self.registry_ids)) != len(self.registry_ids):
            raise ValueError("Duplicate routing runtime registry id")
        return self


class RoutingRuntimeRegistryView(_RoutingModel):
    registry_id: uuid.UUID
    open_scopes: StrictInt = Field(ge=0)
    active_attempts: StrictInt = Field(ge=0)
    uncertain_attempts: StrictInt = Field(ge=0)
    continuity: Literal["unverified", "ready"]
    authority_epoch: uuid.UUID | None
    backend_incarnation: uuid.UUID | None
    control_id: uuid.UUID | None


class RoutingRuntimeResult(_RoutingModel):
    schema_: Literal["aap-inference-routing-runtime-result/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    observed_at: datetime
    registries: list[RoutingRuntimeRegistryView] = Field(max_length=512)

    @model_validator(mode="after")
    def utc_ordered_registries(self) -> RoutingRuntimeResult:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timedelta(
            0
        ):
            raise ValueError("Routing runtime observation must use UTC")
        identifiers = [str(registry.registry_id) for registry in self.registries]
        if identifiers != sorted(set(identifiers)):
            raise ValueError("Routing runtime registries must be unique and ordered")
        return self


class RoutingQuiescenceRequest(_RoutingModel):
    schema_: Literal["aap-inference-routing-quiescence/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    registry_id: uuid.UUID


class RoutingQuiescenceTopology(_RoutingModel):
    registry_id: uuid.UUID
    member_id: uuid.UUID | None
    pool_id: uuid.UUID | None
    routing_generation: StrictInt | None
    traffic_state: Literal["active", "draining", "disabled"] | None

    @model_validator(mode="after")
    def consistent_member_topology(self) -> RoutingQuiescenceTopology:
        if self.member_id is None:
            if any(
                value is not None
                for value in (
                    self.pool_id,
                    self.routing_generation,
                    self.traffic_state,
                )
            ):
                raise ValueError("Routing quiescence topology has partial membership")
        elif (
            self.pool_id is None
            or self.routing_generation is None
            or self.routing_generation < 1
            or self.traffic_state is None
        ):
            raise ValueError("Routing quiescence topology has partial membership")
        return self


class RoutingQuiescenceResult(_RoutingModel):
    schema_: Literal["aap-inference-routing-quiescence-result/v1"] = Field(
        alias="schema"
    )
    request_id: uuid.UUID
    registry_id: uuid.UUID
    status: Literal["ok", "rejected"]
    reason: (
        Literal[
            "pool_unavailable",
            "management_busy",
            "member_not_draining",
            "member_not_quiescent",
            "continuity_unverified",
            "generation_mismatch",
            "backend_not_ready",
        ]
        | None
    )
    control_id: uuid.UUID | None
    topology: RoutingQuiescenceTopology | None
    active_scopes: StrictInt | None
    uncertain_scopes: StrictInt | None
    continuity: Literal["unverified", "ready"] | None

    @model_validator(mode="after")
    def status_matches_facts(self) -> RoutingQuiescenceResult:
        if self.status == "ok":
            if (
                self.reason is not None
                or self.control_id is not None
                or self.topology is None
                or self.topology.registry_id != self.registry_id
                or self.active_scopes != 0
                or self.uncertain_scopes != 0
                or self.continuity != "ready"
            ):
                raise ValueError("Invalid successful routing quiescence result")
        elif (
            self.reason is None
            or self.topology is not None
            or self.active_scopes is not None
            or self.uncertain_scopes is not None
            or self.continuity is not None
            or (self.control_id is not None and self.reason != "management_busy")
        ):
            raise ValueError("Invalid rejected routing quiescence result")
        return self


class RoutingScopeView(_RoutingModel):
    id: uuid.UUID
    pool_id: uuid.UUID | None
    registry_id: uuid.UUID
    project_id: uuid.UUID | None
    mode: Literal["off", "observe", "enforce", "direct"]
    owner: StrictStr = Field(min_length=1, max_length=512)
    operation: StrictStr = Field(min_length=1, max_length=96)
    generation: StrictInt | None
    config_revision: StrictInt = Field(ge=1)
    eligibility_revision: StrictInt = Field(ge=1)
    authority_epoch: uuid.UUID
    state: Literal[
        "reserved",
        "active",
        "uncertain",
        "finished",
        "cancelled",
        "expired",
        "reconciled",
    ]
    closed: StrictBool
    heartbeat_deadline_ms: StrictInt = Field(ge=0)
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)


class RoutingAttemptView(_RoutingModel):
    attempt_id: uuid.UUID
    state: Literal["active", "response", "uncertain", "reconciled"]
    status_code: StrictInt | None
    reason: StrictStr | None = Field(max_length=96)

    @model_validator(mode="after")
    def valid_attempt(self) -> RoutingAttemptView:
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ValueError("Invalid routing attempt status")
        _validate_shortcode(self.reason)
        return self


class RoutingControlView(_RoutingModel):
    control_id: uuid.UUID
    registry_id: uuid.UUID
    kind: Literal["unload", "reload"]
    phase: Literal["prepared", "draining", "completed", "uncertain"]
    capability: StrictStr = Field(min_length=1, max_length=16384, repr=False)


class RoutingEpochView(_RoutingModel):
    control_id: uuid.UUID
    registry_id: uuid.UUID
    phase: Literal["prepared", "draining", "drained", "bound", "completed", "uncertain"]
    authority_epoch: uuid.UUID | None
    backend_incarnation: uuid.UUID | None


class RoutingBeginResult(_RoutingModel):
    attempt_id: uuid.UUID
    scope: RoutingScopeView
    headers: dict[StrictStr, StrictStr] = Field(repr=False)

    @model_validator(mode="after")
    def permitted_admission_header(self) -> RoutingBeginResult:
        if set(self.headers) not in (set(), {"X-AAP-Route-Admission"}):
            raise ValueError("Invalid routing admission headers")
        _validate_header_values(self.headers)
        return self


class RoutingBeginControlResult(_RoutingModel):
    attempt_id: uuid.UUID
    control: RoutingControlView
    headers: dict[StrictStr, StrictStr] = Field(repr=False)

    @model_validator(mode="after")
    def permitted_admission_header(self) -> RoutingBeginControlResult:
        if set(self.headers) not in (set(), {"X-AAP-Route-Admission"}):
            raise ValueError("Invalid routing admission headers")
        _validate_header_values(self.headers)
        return self


class RoutingScopeStatusResult(_RoutingModel):
    scope: RoutingScopeView
    attempts: list[RoutingAttemptView]


def _validate_header_values(headers: dict[str, str]) -> None:
    if any(
        not value
        or len(value) > 16384
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
        for value in headers.values()
    ):
        raise ValueError("Invalid routing admission header value")


class RoutingRejectedResult(_RoutingModel):
    kind: Literal["rejected"]
    reason: Literal[
        "pool_unavailable",
        "pool_not_enabled",
        "pool_saturated",
        "generation_mismatch",
        "scope_unavailable",
        "scope_expired",
        "scope_closed",
        "transport_changed",
        "management_busy",
        "member_not_draining",
        "member_not_quiescent",
        "continuity_unverified",
        "backend_not_ready",
        "invalid_command",
    ]


_RESULT_BODY_TYPES: dict[str, type[_RoutingModel]] = {
    "acquire": RoutingScopeView,
    "video_acquire": RoutingScopeView,
    "begin": RoutingBeginResult,
    "settle": RoutingAttemptView,
    "heartbeat": RoutingScopeView,
    "close": RoutingScopeView,
    "scope_status": RoutingScopeStatusResult,
    "reconcile": RoutingEpochView,
    "prepare_control": RoutingControlView,
    "finish_control": RoutingControlView,
    "begin_control": RoutingBeginControlResult,
    "settle_control": RoutingAttemptView,
}


def _typed_result(operation: str, result: object) -> _RoutingModel:
    try:
        normalized = _json_ready(result)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("Invalid routing operation result") from None
    if type(normalized) is dict and normalized.get("kind") == "rejected":
        adapter = TypeAdapter(RoutingRejectedResult)
    else:
        try:
            adapter = TypeAdapter(_RESULT_BODY_TYPES[operation])
        except KeyError:
            raise ValueError("Invalid routing operation") from None
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return adapter.validate_json(encoded)
    except _JSON_ERRORS:
        raise ValueError("Invalid routing operation result") from None


class RoutingResult(_RoutingModel):
    schema_: Literal["aap-inference-routing-result/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    command_sha256: StrictStr
    operation: RoutingOperation
    state: RoutingState
    result: Any = Field(repr=False)

    @model_validator(mode="after")
    def closed_operation_result(self) -> RoutingResult:
        _validate_digest(self.command_sha256)
        if self.state == "unknown":
            if self.result is not None:
                raise ValueError("Unknown routing result must be null")
        else:
            if self.result is None:
                raise ValueError("Known routing result must be an object")
            object.__setattr__(
                self, "result", _typed_result(self.operation, self.result)
            )
        return self


_COMMAND_ADAPTER = TypeAdapter(RoutingCommand)
_STATUS_REQUEST_ADAPTER = TypeAdapter(RoutingStatusRequest)
_RESULT_ADAPTER = TypeAdapter(RoutingResult)
_RUNTIME_REQUEST_ADAPTER = TypeAdapter(RoutingRuntimeRequest)
_RUNTIME_RESULT_ADAPTER = TypeAdapter(RoutingRuntimeResult)
_QUIESCENCE_REQUEST_ADAPTER = TypeAdapter(RoutingQuiescenceRequest)
_QUIESCENCE_RESULT_ADAPTER = TypeAdapter(RoutingQuiescenceResult)


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("Invalid routing command digest")


def _command_wire(command: RoutingCommand) -> dict[str, Any]:
    return {
        "schema": command.schema_,
        "request_id": str(command.request_id),
        "operation": command.operation,
        "body": command.body.model_dump(mode="json", by_alias=True),
    }


def _result_wire(result: RoutingResult) -> dict[str, Any]:
    inner = result.result
    return {
        "schema": result.schema_,
        "request_id": str(result.request_id),
        "command_sha256": result.command_sha256,
        "operation": result.operation,
        "state": result.state,
        "result": inner.model_dump(mode="json", by_alias=True)
        if inner is not None
        else None,
    }


def _status_request_wire(request: RoutingStatusRequest) -> dict[str, Any]:
    return {
        "schema": request.schema_,
        "request_id": str(request.request_id),
        "command_sha256": request.command_sha256,
        "operation": request.operation,
    }


def _runtime_request_wire(request: RoutingRuntimeRequest) -> dict[str, Any]:
    return {
        "schema": request.schema_,
        "request_id": str(request.request_id),
        "registry_ids": [str(registry_id) for registry_id in request.registry_ids],
    }


def _runtime_result_wire(result: RoutingRuntimeResult) -> dict[str, Any]:
    return result.model_dump(mode="json", by_alias=True)


def _quiescence_request_wire(
    request: RoutingQuiescenceRequest,
) -> dict[str, Any]:
    return {
        "schema": request.schema_,
        "request_id": str(request.request_id),
        "registry_id": str(request.registry_id),
    }


def _quiescence_result_wire(
    result: RoutingQuiescenceResult,
) -> dict[str, Any]:
    return result.model_dump(mode="json", by_alias=True)


def _canonical_wire_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _decode_command(value: object) -> RoutingCommand:
    try:
        input_value = _command_wire(value) if type(value) is RoutingCommand else value
    except (AttributeError, TypeError, ValueError):
        raise RoutingUnavailable() from None
    encoded, _ = _json_input(input_value, max_bytes=ROUTING_REQUEST_MAX_BYTES)
    try:
        return _COMMAND_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None


def decode_routing_command(data: object) -> RoutingCommand:
    """Decode and revalidate a command model, mapping, or strict JSON body."""
    return _decode_command(data)


def make_routing_command(
    operation: RoutingOperation,
    body: dict[str, Any],
    *,
    request_id: uuid.UUID | None = None,
) -> RoutingCommand:
    """Build a closed public routing operation body."""
    if request_id is not None and type(request_id) is not uuid.UUID:
        raise RoutingUnavailable()
    try:
        command = _COMMAND_ADAPTER.validate_python(
            {
                "schema": ROUTING_PROTOCOL,
                "request_id": request_id or uuid.uuid4(),
                "operation": operation,
                "body": body,
            }
        )
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None
    routing_command_bytes(command)
    return command


def routing_command_bytes(command: object) -> bytes:
    """Encode a strict command as deterministic UTF-8 JSON within the wire cap."""
    typed = _decode_command(command)
    encoded = json.dumps(
        _command_wire(typed),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > ROUTING_REQUEST_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def routing_command_sha256(command: RoutingCommand | object) -> str:
    """Fingerprint canonical command UTF-8 JSON with request_id removed."""
    typed = _decode_command(command)
    canonical = _command_wire(typed)
    del canonical["request_id"]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decode_status_request(value: object) -> RoutingStatusRequest:
    try:
        input_value = (
            _status_request_wire(value)
            if type(value) is RoutingStatusRequest
            else value
        )
    except (AttributeError, TypeError, ValueError):
        raise RoutingUnavailable() from None
    encoded, _ = _json_input(input_value, max_bytes=ROUTING_REQUEST_MAX_BYTES)
    try:
        return _STATUS_REQUEST_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None


def decode_routing_status_request(data: object) -> RoutingStatusRequest:
    """Decode and revalidate a status request."""
    return _decode_status_request(data)


def routing_status_request_bytes(request: object) -> bytes:
    """Encode a strict status request as bounded UTF-8 JSON."""
    typed = _decode_status_request(request)
    encoded = json.dumps(
        _status_request_wire(typed),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > ROUTING_REQUEST_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def make_routing_status_request(
    command: RoutingCommand | object,
) -> RoutingStatusRequest:
    """Build a status request bound to the full original command fingerprint."""
    typed = _decode_command(command)
    return RoutingStatusRequest(
        schema=ROUTING_STATUS_PROTOCOL,
        request_id=typed.request_id,
        command_sha256=routing_command_sha256(typed),
        operation=typed.operation,
    )


def _decode_result(value: object) -> RoutingResult:
    try:
        input_value = _result_wire(value) if type(value) is RoutingResult else value
    except (AttributeError, TypeError, ValueError):
        raise RoutingUnavailable() from None
    encoded, _ = _json_input(input_value, max_bytes=ROUTING_REPLY_MAX_BYTES)
    try:
        return _RESULT_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None


def routing_result_bytes(result: object) -> bytes:
    """Encode a validated typed routing result within the reply cap."""
    typed = _decode_result(result)
    encoded = json.dumps(
        _result_wire(typed),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > ROUTING_REPLY_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def decode_routing_result(
    command: RoutingCommand | object,
    data: object,
) -> RoutingResult:
    """Decode and bind a result to request ID, operation, and command digest."""
    typed_command = _decode_command(command)
    result = _decode_result(data)
    if (
        result.request_id != typed_command.request_id
        or result.command_sha256 != routing_command_sha256(typed_command)
        or result.operation != typed_command.operation
    ):
        raise RoutingUnavailable()
    return result


def decode_routing_status_result(
    request: RoutingStatusRequest | object,
    data: object,
) -> RoutingResult:
    """Decode and bind a receipt to the requested command identity and digest."""
    typed_request = _decode_status_request(request)
    result = _decode_result(data)
    if (
        result.request_id != typed_request.request_id
        or result.command_sha256 != typed_request.command_sha256
        or result.operation != typed_request.operation
    ):
        raise RoutingUnavailable()
    return result


def _decode_runtime_request(value: object) -> RoutingRuntimeRequest:
    try:
        input_value = (
            _runtime_request_wire(value)
            if type(value) is RoutingRuntimeRequest
            else value
        )
    except (AttributeError, TypeError, ValueError):
        raise RoutingUnavailable() from None
    encoded, parsed = _json_input(input_value, max_bytes=ROUTING_REQUEST_MAX_BYTES)
    try:
        request = _RUNTIME_REQUEST_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None
    if (
        not _canonical_wire_uuid(parsed.get("request_id"))
        or type(parsed.get("registry_ids")) is not list
        or any(not _canonical_wire_uuid(value) for value in parsed["registry_ids"])
    ):
        raise RoutingUnavailable()
    return request


def decode_routing_runtime_request(data: object) -> RoutingRuntimeRequest:
    """Decode a strict bounded runtime observation request."""
    return _decode_runtime_request(data)


def make_routing_runtime_request(
    registry_ids: Sequence[uuid.UUID | str],
    *,
    request_id: uuid.UUID | None = None,
) -> RoutingRuntimeRequest:
    if (
        not isinstance(registry_ids, Sequence)
        or isinstance(registry_ids, (str, bytes))
        or len(registry_ids) > 512
        or (request_id is not None and type(request_id) is not uuid.UUID)
    ):
        raise RoutingUnavailable()
    return _decode_runtime_request(
        {
            "schema": ROUTING_RUNTIME_PROTOCOL,
            "request_id": request_id if request_id is not None else uuid.uuid4(),
            "registry_ids": list(registry_ids),
        }
    )


def routing_runtime_request_bytes(request: object) -> bytes:
    """Encode a closed runtime observation request within the request budget."""
    typed = _decode_runtime_request(request)
    encoded = json.dumps(
        _runtime_request_wire(typed),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > ROUTING_REQUEST_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def _decode_runtime_result(value: object) -> RoutingRuntimeResult:
    try:
        input_value = (
            _runtime_result_wire(value)
            if type(value) is RoutingRuntimeResult
            else value
        )
    except (AttributeError, TypeError, ValueError):
        raise RoutingUnavailable() from None
    encoded, parsed = _json_input(input_value, max_bytes=ROUTING_REPLY_MAX_BYTES)
    try:
        result = _RUNTIME_RESULT_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None
    rows = parsed.get("registries")
    observed_at = parsed.get("observed_at")
    if (
        not _canonical_wire_uuid(parsed.get("request_id"))
        or type(observed_at) is not str
        or not (observed_at.endswith("Z") or observed_at.endswith("+00:00"))
        or type(rows) is not list
        or any(
            type(row) is not dict or not _canonical_wire_uuid(row.get("registry_id"))
            for row in rows
        )
    ):
        raise RoutingUnavailable()
    return result


def routing_runtime_result_bytes(result: object) -> bytes:
    """Encode a validated runtime observation result within the reply budget."""
    typed = _decode_runtime_result(result)
    encoded = json.dumps(
        _runtime_result_wire(typed),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > ROUTING_REPLY_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def decode_routing_runtime_result(
    request: RoutingRuntimeRequest | object,
    data: object,
) -> RoutingRuntimeResult:
    """Decode and bind an observation to the requested IDs and request ID."""
    typed_request = _decode_runtime_request(request)
    result = _decode_runtime_result(data)
    requested_ids = set(typed_request.registry_ids)
    returned_ids = [row.registry_id for row in result.registries]
    if result.request_id != typed_request.request_id or any(
        registry_id not in requested_ids for registry_id in returned_ids
    ):
        raise RoutingUnavailable()
    return result


def _decode_quiescence_request(value: object) -> RoutingQuiescenceRequest:
    try:
        input_value = (
            _quiescence_request_wire(value)
            if type(value) is RoutingQuiescenceRequest
            else value
        )
    except (AttributeError, TypeError, ValueError):
        raise RoutingUnavailable() from None
    encoded, parsed = _json_input(input_value, max_bytes=ROUTING_REQUEST_MAX_BYTES)
    try:
        request = _QUIESCENCE_REQUEST_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None
    if not all(
        _canonical_wire_uuid(parsed.get(field))
        for field in ("request_id", "registry_id")
    ):
        raise RoutingUnavailable()
    return request


def decode_routing_quiescence_request(data: object) -> RoutingQuiescenceRequest:
    """Decode a bounded, closed routing quiescence request."""
    return _decode_quiescence_request(data)


def make_routing_quiescence_request(
    registry_id: uuid.UUID | str,
    *,
    request_id: uuid.UUID | None = None,
) -> RoutingQuiescenceRequest:
    if request_id is not None and type(request_id) is not uuid.UUID:
        raise RoutingUnavailable()
    return _decode_quiescence_request(
        {
            "schema": ROUTING_QUIESCENCE_PROTOCOL,
            "request_id": request_id if request_id is not None else uuid.uuid4(),
            "registry_id": registry_id,
        }
    )


def routing_quiescence_request_bytes(request: object) -> bytes:
    """Encode a bounded routing quiescence request."""
    typed = _decode_quiescence_request(request)
    encoded = json.dumps(
        _quiescence_request_wire(typed),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > ROUTING_REQUEST_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def _decode_quiescence_result(value: object) -> RoutingQuiescenceResult:
    try:
        input_value = (
            _quiescence_result_wire(value)
            if type(value) is RoutingQuiescenceResult
            else value
        )
    except (AttributeError, TypeError, ValueError):
        raise RoutingUnavailable() from None
    encoded, parsed = _json_input(input_value, max_bytes=ROUTING_REPLY_MAX_BYTES)
    try:
        result = _QUIESCENCE_RESULT_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RoutingUnavailable() from None
    if (
        not _canonical_wire_uuid(parsed.get("request_id"))
        or not _canonical_wire_uuid(parsed.get("registry_id"))
        or (
            parsed.get("control_id") is not None
            and not _canonical_wire_uuid(parsed.get("control_id"))
        )
        or (
            type(parsed.get("topology")) is dict
            and any(
                value is not None and not _canonical_wire_uuid(value)
                for field in ("registry_id", "member_id", "pool_id")
                for value in (parsed["topology"].get(field),)
            )
        )
    ):
        raise RoutingUnavailable()
    return result


def routing_quiescence_result_bytes(result: object) -> bytes:
    """Encode a validated routing quiescence observation."""
    typed = _decode_quiescence_result(result)
    encoded = json.dumps(
        _quiescence_result_wire(typed),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > ROUTING_REPLY_MAX_BYTES:
        raise RoutingUnavailable()
    return encoded


def decode_routing_quiescence_result(
    request: RoutingQuiescenceRequest | object,
    data: object,
) -> RoutingQuiescenceResult:
    """Decode and bind a quiescence observation to its registry and request."""
    typed_request = _decode_quiescence_request(request)
    result = _decode_quiescence_result(data)
    if (
        result.request_id != typed_request.request_id
        or result.registry_id != typed_request.registry_id
    ):
        raise RoutingUnavailable()
    return result


def routing_native_policy_ready(value: str | bytes | dict) -> bool:
    """Require the exact two-function, nineteen-action Native routing policy."""
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
            parsed = json.loads(
                value,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        elif type(value) is str:
            parsed = json.loads(
                value,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        elif type(value) is dict:
            parsed = value
        else:
            return False
    except _JSON_ERRORS:
        return False
    if type(parsed) is not dict or set(parsed) != _POLICY_KEYS:
        return False
    return bool(
        type(parsed["schema"]) is str
        and parsed["schema"] == ROUTING_NATIVE_POLICY_SCHEMA
        and type(parsed["data_protocol"]) is str
        and parsed["data_protocol"] == ROUTING_DATA_PROTOCOL
        and type(parsed["transaction"]) is str
        and parsed["transaction"] == "caller_owned"
        and type(parsed["functions"]) is list
        and all(type(name) is str for name in parsed["functions"])
        and parsed["functions"] == list(ROUTING_NATIVE_FUNCTIONS)
        and type(parsed["actions"]) is list
        and all(type(name) is str for name in parsed["actions"])
        and parsed["actions"] == list(ROUTING_NATIVE_ACTIONS)
        and type(parsed["ready"]) is bool
        and parsed["ready"] is True
    )


__all__ = [
    "ROUTING_DATA_PROTOCOL",
    "ROUTING_NATIVE_ACTIONS",
    "ROUTING_NATIVE_FUNCTIONS",
    "ROUTING_NATIVE_POLICY_SCHEMA",
    "ROUTING_PROTOCOL",
    "ROUTING_REPLY_MAX_BYTES",
    "ROUTING_REQUEST_MAX_BYTES",
    "ROUTING_RESULT_PROTOCOL",
    "ROUTING_RUNTIME_PROTOCOL",
    "ROUTING_RUNTIME_RESULT_PROTOCOL",
    "ROUTING_QUIESCENCE_PROTOCOL",
    "ROUTING_QUIESCENCE_RESULT_PROTOCOL",
    "RoutingQuiescenceRequest",
    "RoutingQuiescenceResult",
    "RoutingQuiescenceTopology",
    "decode_routing_quiescence_request",
    "decode_routing_quiescence_result",
    "make_routing_quiescence_request",
    "routing_quiescence_request_bytes",
    "routing_quiescence_result_bytes",
    "ROUTING_STATUS_PROTOCOL",
    "RoutingAttemptView",
    "RoutingBeginControlResult",
    "RoutingBeginResult",
    "RoutingCommand",
    "RoutingControlView",
    "RoutingEpochView",
    "RoutingExpectedTopology",
    "RoutingRejectedResult",
    "RoutingResult",
    "RoutingRuntimeRegistryView",
    "RoutingRuntimeRequest",
    "RoutingRuntimeResult",
    "RoutingScopeStatusResult",
    "RoutingScopeView",
    "RoutingStatusRequest",
    "RoutingUnavailable",
    "decode_routing_command",
    "decode_routing_result",
    "decode_routing_runtime_request",
    "decode_routing_runtime_result",
    "decode_routing_status_request",
    "decode_routing_status_result",
    "make_routing_command",
    "make_routing_status_request",
    "make_routing_runtime_request",
    "routing_command_bytes",
    "routing_command_sha256",
    "routing_native_policy_ready",
    "routing_result_bytes",
    "routing_runtime_request_bytes",
    "routing_runtime_result_bytes",
    "routing_status_request_bytes",
]
