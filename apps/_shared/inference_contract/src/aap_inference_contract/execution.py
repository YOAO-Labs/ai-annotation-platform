"""Frozen, framework-neutral model execution request and result contracts."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias

from .backend_endpoint import (
    BackendEndpointAuthority,
    BackendEndpointBinding,
    BackendEndpointSubject,
    decode_backend_endpoint_subject,
)

MODEL_EXECUTION_PROTOCOL = "aap-inference-model-execution/v1"
MODEL_EXECUTION_RESULT_PROTOCOL = "aap-inference-model-execution-result/v1"
MODEL_EXECUTION_FAILURE_PROTOCOL = "aap-inference-model-execution-failure/v1"
MODEL_EXECUTION_PURPOSE = "execution"

ExecutionOperation: TypeAlias = Literal[
    "batch_predict", "interactive_predict", "pinned_tracker"
]
ExecutionFailureKind: TypeAlias = Literal[
    "invalid_request",
    "unavailable",
    "admission",
    "backend",
    "transport",
    "uncertain",
]

_UNSET = object()
_FROZEN_MAPPING_TYPE = type(MappingProxyType({}))


def freeze_json(value: Any) -> Any:
    """Copy JSON-shaped data once into read-only mappings and tuples."""
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value
    if isinstance(value, _FROZEN_MAPPING_TYPE):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    if type(value) is tuple:
        frozen = tuple(freeze_json(item) for item in value)
        return value if all(a is b for a, b in zip(value, frozen)) else frozen
    raise ValueError("Value is not JSON data")


def thaw_json(value: Any) -> Any:
    """Make one JSON-compatible copy from the immutable public boundary."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _uuid(value: Any, *, optional: bool = False) -> uuid.UUID | None:
    if optional and value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    if type(value) is str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError):
            parsed = None
        if parsed is not None and str(parsed) == value:
            return parsed
    raise ValueError("Invalid execution UUID")


def _nonempty(value: Any, *, limit: int, name: str) -> str:
    if type(value) is not str or not value or len(value) > limit:
        raise ValueError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class ModelExecutionRouteBinding:
    """Serializable proof for one existing route scope and one backend attempt."""

    scope_id: uuid.UUID
    attempt_id: uuid.UUID
    capability: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _uuid(self.scope_id))
        object.__setattr__(self, "attempt_id", _uuid(self.attempt_id))
        _nonempty(self.capability, limit=16384, name="route capability")

    def endpoint_binding(self) -> BackendEndpointBinding:
        return BackendEndpointBinding(
            kind="scope",
            scope_id=self.scope_id,
            control_id=None,
            attempt_id=self.attempt_id,
            capability=self.capability,
        )

    def endpoint_authority(self) -> BackendEndpointAuthority:
        return BackendEndpointAuthority(
            kind="scope",
            scope_id=self.scope_id,
            control_id=None,
            attempt_id=self.attempt_id,
        )

    def to_wire(self) -> dict[str, str]:
        return {
            "scope_id": str(self.scope_id),
            "attempt_id": str(self.attempt_id),
            "capability": self.capability,
        }

    @classmethod
    def from_wire(cls, value: Any) -> ModelExecutionRouteBinding | None:
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != {
            "scope_id",
            "attempt_id",
            "capability",
        }:
            raise ValueError("Invalid execution route binding")
        return cls(
            scope_id=_uuid(value["scope_id"]),
            attempt_id=_uuid(value["attempt_id"]),
            capability=value["capability"],
        )


@dataclass(frozen=True, slots=True)
class FrozenModelOperation:
    """One fully frozen model operation; endpoint and route proof are per-call."""

    request_id: uuid.UUID
    operation: ExecutionOperation
    task_inputs: tuple[Mapping[str, Any], ...] = field(repr=False)
    context: Mapping[str, Any] | None = field(repr=False)
    project_id: uuid.UUID | None
    requested_registry_id: uuid.UUID | None
    requested_pool_id: uuid.UUID | None
    endpoint_registry_id: uuid.UUID | None
    required_instance_id: uuid.UUID | None
    route_owner: str
    route_operation: str
    selected_pool_id: uuid.UUID | None = None
    selected_member_id: uuid.UUID | None = None
    topology_generation: int | None = None
    max_response_bytes: int | None = None
    correlation: Mapping[str, Any] | None = field(default=None, repr=False)
    input_provenance: Mapping[str, Any] | None = field(default=None, repr=False)
    health_meta: Mapping[str, Any] | None = field(default=None, repr=False)
    route_binding: ModelExecutionRouteBinding | None = field(default=None, repr=False)
    endpoint_subject: BackendEndpointSubject | None = field(default=None, repr=False)
    protocol: str = MODEL_EXECUTION_PROTOCOL

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _uuid(self.request_id))
        for name in (
            "project_id",
            "requested_registry_id",
            "requested_pool_id",
            "endpoint_registry_id",
            "required_instance_id",
            "selected_pool_id",
            "selected_member_id",
        ):
            object.__setattr__(self, name, _uuid(getattr(self, name), optional=True))
        if self.topology_generation is not None and (
            type(self.topology_generation) is not int or self.topology_generation < 0
        ):
            raise ValueError("Invalid selected topology generation")
        if self.protocol != MODEL_EXECUTION_PROTOCOL:
            raise ValueError("Unsupported model execution protocol")
        if self.operation not in {
            "batch_predict",
            "interactive_predict",
            "pinned_tracker",
        }:
            raise ValueError("Unsupported model operation")
        if self.operation != "batch_predict" and len(self.task_inputs) != 1:
            raise ValueError("Interactive operations require exactly one task")
        object.__setattr__(
            self,
            "route_owner",
            _nonempty(self.route_owner, limit=512, name="route owner"),
        )
        object.__setattr__(
            self,
            "route_operation",
            _nonempty(self.route_operation, limit=96, name="route operation"),
        )
        if self.max_response_bytes is not None and (
            type(self.max_response_bytes) is not int or self.max_response_bytes < 1
        ):
            raise ValueError("Invalid model response limit")
        frozen_inputs = []
        if not self.task_inputs:
            raise ValueError("Model execution requires at least one task")
        for item in self.task_inputs:
            if not isinstance(item, Mapping):
                raise ValueError("Model task input must be an object")
            frozen = freeze_json(item)
            task_id = frozen.get("id")
            file_path = frozen.get("file_path")
            if type(task_id) is not str or not task_id:
                raise ValueError("Model task input requires a task identity")
            if self.operation == "batch_predict" and type(file_path) is not str:
                raise ValueError("Batch model task input requires a file path")
            if file_path is not None and type(file_path) is not str:
                raise ValueError("Model task input file path must be text")
            frozen_inputs.append(frozen)
        if len({item["id"] for item in frozen_inputs}) != len(frozen_inputs):
            raise ValueError("Model execution task identities must be unique")
        object.__setattr__(self, "task_inputs", tuple(frozen_inputs))
        for name in ("context", "correlation", "input_provenance", "health_meta"):
            value = getattr(self, name)
            if value is not None:
                frozen = freeze_json(value)
                if not isinstance(frozen, Mapping):
                    raise ValueError(f"Model execution {name} must be an object")
                object.__setattr__(self, name, frozen)
        if self.route_binding is not None:
            if not isinstance(self.route_binding, ModelExecutionRouteBinding):
                raise ValueError("Invalid model route binding")
            if self.route_binding.attempt_id != self.request_id:
                raise ValueError("Execution request and route attempt differ")
        if self.endpoint_subject is not None:
            if not isinstance(self.endpoint_subject, BackendEndpointSubject):
                raise ValueError("Invalid model endpoint subject")
            if (
                self.endpoint_subject.request_id != self.request_id
                or self.endpoint_subject.purpose != MODEL_EXECUTION_PURPOSE
                or self.endpoint_subject.registry_id != self.endpoint_registry_id
            ):
                raise ValueError("Model endpoint subject does not match request")
            if (
                self.route_binding is None
                or self.endpoint_subject.authority
                != self.route_binding.endpoint_authority()
            ):
                raise ValueError("Model endpoint subject does not match route attempt")

    def with_authority(
        self,
        *,
        route_binding: ModelExecutionRouteBinding,
        endpoint_subject: BackendEndpointSubject,
        endpoint_registry_id: uuid.UUID,
        requested_pool_id: uuid.UUID | None = None,
        required_instance_id: uuid.UUID | None = None,
        selected_pool_id: uuid.UUID | None = None,
        selected_member_id: uuid.UUID | None = None,
        topology_generation: int | None = None,
        health_meta: Any = _UNSET,
    ) -> FrozenModelOperation:
        """Return a request sealed to this exact selected backend attempt."""
        return FrozenModelOperation(
            request_id=self.request_id,
            operation=self.operation,
            task_inputs=self.task_inputs,
            context=self.context,
            project_id=self.project_id,
            requested_registry_id=self.requested_registry_id,
            requested_pool_id=(
                self.requested_pool_id
                if requested_pool_id is None
                else requested_pool_id
            ),
            endpoint_registry_id=endpoint_registry_id,
            required_instance_id=(
                self.required_instance_id
                if required_instance_id is None
                else required_instance_id
            ),
            selected_pool_id=selected_pool_id,
            selected_member_id=selected_member_id,
            topology_generation=topology_generation,
            route_owner=self.route_owner,
            route_operation=self.route_operation,
            max_response_bytes=self.max_response_bytes,
            correlation=self.correlation,
            input_provenance=self.input_provenance,
            health_meta=(self.health_meta if health_meta is _UNSET else health_meta),
            route_binding=route_binding,
            endpoint_subject=endpoint_subject,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "request_id": str(self.request_id),
            "operation": self.operation,
            "task_inputs": thaw_json(self.task_inputs),
            "context": None if self.context is None else thaw_json(self.context),
            "project_id": None if self.project_id is None else str(self.project_id),
            "requested_registry_id": None
            if self.requested_registry_id is None
            else str(self.requested_registry_id),
            "requested_pool_id": None
            if self.requested_pool_id is None
            else str(self.requested_pool_id),
            "endpoint_registry_id": None
            if self.endpoint_registry_id is None
            else str(self.endpoint_registry_id),
            "required_instance_id": None
            if self.required_instance_id is None
            else str(self.required_instance_id),
            "selected_pool_id": None
            if self.selected_pool_id is None
            else str(self.selected_pool_id),
            "selected_member_id": None
            if self.selected_member_id is None
            else str(self.selected_member_id),
            "topology_generation": self.topology_generation,
            "route_owner": self.route_owner,
            "route_operation": self.route_operation,
            "max_response_bytes": self.max_response_bytes,
            "correlation": None
            if self.correlation is None
            else thaw_json(self.correlation),
            "input_provenance": None
            if self.input_provenance is None
            else thaw_json(self.input_provenance),
            "health_meta": None
            if self.health_meta is None
            else thaw_json(self.health_meta),
            "route_binding": None
            if self.route_binding is None
            else self.route_binding.to_wire(),
            "endpoint_subject": None
            if self.endpoint_subject is None
            else self.endpoint_subject.model_dump(mode="json", by_alias=True),
        }

    @classmethod
    def from_wire(cls, value: Any) -> FrozenModelOperation:
        expected = {
            "protocol",
            "request_id",
            "operation",
            "task_inputs",
            "context",
            "project_id",
            "requested_registry_id",
            "requested_pool_id",
            "endpoint_registry_id",
            "required_instance_id",
            "selected_pool_id",
            "selected_member_id",
            "topology_generation",
            "route_owner",
            "route_operation",
            "max_response_bytes",
            "correlation",
            "input_provenance",
            "health_meta",
            "route_binding",
            "endpoint_subject",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("Invalid model execution request")
        subject = value["endpoint_subject"]
        if subject is not None:
            subject = decode_backend_endpoint_subject(subject)
        return cls(
            protocol=value["protocol"],
            request_id=_uuid(value["request_id"]),
            operation=value["operation"],
            task_inputs=tuple(value["task_inputs"]),
            context=value["context"],
            project_id=_uuid(value["project_id"], optional=True),
            requested_registry_id=_uuid(value["requested_registry_id"], optional=True),
            requested_pool_id=_uuid(value["requested_pool_id"], optional=True),
            endpoint_registry_id=_uuid(value["endpoint_registry_id"], optional=True),
            required_instance_id=_uuid(value["required_instance_id"], optional=True),
            selected_pool_id=_uuid(value["selected_pool_id"], optional=True),
            selected_member_id=_uuid(value["selected_member_id"], optional=True),
            topology_generation=value["topology_generation"],
            route_owner=value["route_owner"],
            route_operation=value["route_operation"],
            max_response_bytes=value["max_response_bytes"],
            correlation=value["correlation"],
            input_provenance=value["input_provenance"],
            health_meta=value["health_meta"],
            route_binding=ModelExecutionRouteBinding.from_wire(value["route_binding"]),
            endpoint_subject=subject,
        )


@dataclass(frozen=True, slots=True)
class ModelTaskResult:
    task_id: str
    result: tuple[Any, ...] = field(repr=False)
    score: float | None = None
    model_version: str | None = None
    inference_time_ms: int | None = None
    cache_hit: bool | None = None
    model_load_ms: int | None = None
    meta: Mapping[str, Any] | None = field(default=None, repr=False)
    mask_input_next: str | None = field(default=None, repr=False)
    diagnostic: Mapping[str, Any] | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _nonempty(self.task_id, limit=4096, name="result task identity")
        frozen_result = freeze_json(self.result)
        if not isinstance(frozen_result, tuple):
            raise ValueError("Model result shapes must be an array")
        object.__setattr__(self, "result", frozen_result)
        for name in ("meta", "diagnostic"):
            value = getattr(self, name)
            if value is not None:
                frozen = freeze_json(value)
                if not isinstance(frozen, Mapping):
                    raise ValueError(f"Model result {name} must be an object")
                object.__setattr__(self, name, frozen)
        if self.score is not None and (
            type(self.score) not in {int, float} or not math.isfinite(float(self.score))
        ):
            raise ValueError("Invalid model score")
        if self.model_version is not None and type(self.model_version) is not str:
            raise ValueError("Invalid model version")
        for name in ("inference_time_ms", "model_load_ms"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Invalid {name}")
        if self.cache_hit is not None and type(self.cache_hit) is not bool:
            raise ValueError("Invalid cache fact")
        if self.mask_input_next is not None and type(self.mask_input_next) is not str:
            raise ValueError("Invalid mask continuation")
        if self.session_token is not None and type(self.session_token) is not str:
            raise ValueError("Invalid tracker session token")


@dataclass(frozen=True, slots=True)
class ExecutionProvenance:
    requested_registry_id: uuid.UUID | None
    requested_pool_id: uuid.UUID | None
    registry_id: uuid.UUID
    pool_id: uuid.UUID | None
    instance_id: uuid.UUID
    route_scope_id: uuid.UUID
    attempt_id: uuid.UUID
    topology_generation: int | None = None
    member_id: uuid.UUID | None = None
    endpoint_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "requested_registry_id",
            "requested_pool_id",
            "registry_id",
            "pool_id",
            "instance_id",
            "route_scope_id",
            "attempt_id",
            "member_id",
        ):
            object.__setattr__(
                self,
                name,
                _uuid(
                    getattr(self, name),
                    optional=name
                    in {
                        "requested_registry_id",
                        "requested_pool_id",
                        "pool_id",
                        "member_id",
                    },
                ),
            )
        if self.topology_generation is not None and (
            type(self.topology_generation) is not int or self.topology_generation < 0
        ):
            raise ValueError("Invalid execution topology generation")
        if self.endpoint_sha256 is not None and (
            type(self.endpoint_sha256) is not str
            or len(self.endpoint_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.endpoint_sha256
            )
        ):
            raise ValueError("Invalid endpoint snapshot fingerprint")


@dataclass(frozen=True, slots=True)
class ModelExecutionObservation:
    backend_id: str
    operation: Literal["predict", "predict_interactive"]
    outcome: Literal["success", "error"]
    elapsed_seconds: float

    def __post_init__(self) -> None:
        _nonempty(self.backend_id, limit=128, name="observed backend identity")
        if self.operation not in {"predict", "predict_interactive"}:
            raise ValueError("Invalid observed backend operation")
        if self.outcome not in {"success", "error"}:
            raise ValueError("Invalid backend observation outcome")
        if (
            type(self.elapsed_seconds) not in {int, float}
            or isinstance(self.elapsed_seconds, bool)
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("Invalid backend observation duration")

    def to_wire(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "operation": self.operation,
            "outcome": self.outcome,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _provenance_to_wire(value: ExecutionProvenance) -> dict[str, Any]:
    return {
        "requested_registry_id": None
        if value.requested_registry_id is None
        else str(value.requested_registry_id),
        "requested_pool_id": None
        if value.requested_pool_id is None
        else str(value.requested_pool_id),
        "registry_id": str(value.registry_id),
        "pool_id": None if value.pool_id is None else str(value.pool_id),
        "instance_id": str(value.instance_id),
        "route_scope_id": str(value.route_scope_id),
        "attempt_id": str(value.attempt_id),
        "topology_generation": value.topology_generation,
        "member_id": None if value.member_id is None else str(value.member_id),
        "endpoint_sha256": value.endpoint_sha256,
    }


def _provenance_from_wire(value: Any) -> ExecutionProvenance:
    expected = {
        "requested_registry_id",
        "requested_pool_id",
        "registry_id",
        "pool_id",
        "instance_id",
        "route_scope_id",
        "attempt_id",
        "topology_generation",
        "member_id",
        "endpoint_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("Invalid model execution provenance")
    uuid_fields = {
        "requested_registry_id",
        "requested_pool_id",
        "registry_id",
        "pool_id",
        "instance_id",
        "route_scope_id",
        "attempt_id",
        "member_id",
    }
    return ExecutionProvenance(
        **{
            key: _uuid(
                entry,
                optional=key
                in {
                    "requested_registry_id",
                    "requested_pool_id",
                    "pool_id",
                    "member_id",
                },
            )
            if key in uuid_fields
            else entry
            for key, entry in value.items()
        }
    )


def _observation_from_wire(value: Any) -> ModelExecutionObservation:
    if not isinstance(value, Mapping) or set(value) != {
        "backend_id",
        "operation",
        "outcome",
        "elapsed_seconds",
    }:
        raise ValueError("Invalid model execution observation")
    return ModelExecutionObservation(**value)


@dataclass(frozen=True, slots=True)
class ModelOperationResult:
    request_id: uuid.UUID
    operation: ExecutionOperation
    results: tuple[ModelTaskResult, ...] = field(repr=False)
    provenance: ExecutionProvenance
    observation: ModelExecutionObservation
    wall_time_ms: int
    response_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _uuid(self.request_id))
        results = tuple(self.results)
        if any(not isinstance(result, ModelTaskResult) for result in results):
            raise ValueError("Model operation results must be immutable task results")
        if len({result.task_id for result in results}) != len(results):
            raise ValueError("Model operation task identities must be unique")
        object.__setattr__(self, "results", results)
        if self.operation not in {
            "batch_predict",
            "interactive_predict",
            "pinned_tracker",
        }:
            raise ValueError("Invalid model operation result kind")
        if not isinstance(self.provenance, ExecutionProvenance):
            raise ValueError("Model operation result requires provenance")
        if not isinstance(self.observation, ModelExecutionObservation):
            raise ValueError("Model operation result requires transport observation")
        if self.provenance.registry_id != self.provenance.instance_id:
            raise ValueError("Selected registry and instance differ")
        if self.provenance.attempt_id != self.request_id:
            raise ValueError("Model result and route attempt differ")
        expected_observation = (
            "predict" if self.operation == "batch_predict" else "predict_interactive"
        )
        if (
            self.observation.backend_id != str(self.provenance.instance_id)
            or self.observation.operation != expected_observation
            or self.observation.outcome != "success"
        ):
            raise ValueError("Model result observation does not match provenance")
        if self.operation != "batch_predict" and len(self.results) != 1:
            raise ValueError("Interactive operation requires one model result")
        if self.wall_time_ms < 0 or type(self.wall_time_ms) is not int:
            raise ValueError("Invalid model operation wall time")
        if self.operation == "pinned_tracker":
            if len(self.results) != 1 or self.response_digest is None:
                raise ValueError(
                    "Pinned tracker result requires one digest-bearing response"
                )
            if (
                type(self.response_digest) is not str
                or len(self.response_digest) != 64
                or any(c not in "0123456789abcdef" for c in self.response_digest)
            ):
                raise ValueError("Invalid tracker response digest")
        elif self.response_digest is not None:
            raise ValueError("Only pinned tracker results include a response digest")

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(result.task_id for result in self.results)

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol": MODEL_EXECUTION_RESULT_PROTOCOL,
            "request_id": str(self.request_id),
            "operation": self.operation,
            "results": [
                {
                    "task_id": result.task_id,
                    "result": thaw_json(result.result),
                    "score": result.score,
                    "model_version": result.model_version,
                    "inference_time_ms": result.inference_time_ms,
                    "cache_hit": result.cache_hit,
                    "model_load_ms": result.model_load_ms,
                    "meta": None if result.meta is None else thaw_json(result.meta),
                    "mask_input_next": result.mask_input_next,
                    "diagnostic": None
                    if result.diagnostic is None
                    else thaw_json(result.diagnostic),
                    "session_token": result.session_token,
                }
                for result in self.results
            ],
            "provenance": _provenance_to_wire(self.provenance),
            "observation": self.observation.to_wire(),
            "wall_time_ms": self.wall_time_ms,
            "response_digest": self.response_digest,
        }

    @classmethod
    def from_wire(cls, value: Any) -> ModelOperationResult:
        expected = {
            "protocol",
            "request_id",
            "operation",
            "results",
            "provenance",
            "observation",
            "wall_time_ms",
            "response_digest",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value["protocol"] != MODEL_EXECUTION_RESULT_PROTOCOL
            or not isinstance(value["results"], list)
        ):
            raise ValueError("Invalid model execution result")
        results = []
        for item in value["results"]:
            expected_item = {
                "task_id",
                "result",
                "score",
                "model_version",
                "inference_time_ms",
                "cache_hit",
                "model_load_ms",
                "meta",
                "mask_input_next",
                "diagnostic",
                "session_token",
            }
            if not isinstance(item, Mapping) or set(item) != expected_item:
                raise ValueError("Invalid model task result")
            results.append(ModelTaskResult(**item))
        return cls(
            request_id=_uuid(value["request_id"]),
            operation=value["operation"],
            results=tuple(results),
            provenance=_provenance_from_wire(value["provenance"]),
            observation=_observation_from_wire(value["observation"]),
            wall_time_ms=value["wall_time_ms"],
            response_digest=value["response_digest"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionFailure:
    request_id: uuid.UUID
    kind: ExecutionFailureKind
    code: str
    status_code: int | None
    detail: Any = field(repr=False)
    retry_after: str | None = None
    uncertain: bool = False
    provenance: ExecutionProvenance | None = None
    observation: ModelExecutionObservation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _uuid(self.request_id))
        if self.kind not in {
            "invalid_request",
            "unavailable",
            "admission",
            "backend",
            "transport",
            "uncertain",
        }:
            raise ValueError("Invalid model execution failure kind")
        _nonempty(self.code, limit=128, name="execution failure code")
        if self.status_code is not None and (
            type(self.status_code) is not int or not 400 <= self.status_code <= 599
        ):
            raise ValueError("Invalid execution failure status")
        if self.retry_after is not None and type(self.retry_after) is not str:
            raise ValueError("Invalid execution Retry-After")
        if type(self.uncertain) is not bool:
            raise ValueError("Invalid execution uncertainty fact")
        if self.provenance is not None:
            if not isinstance(self.provenance, ExecutionProvenance):
                raise ValueError("Invalid execution failure provenance")
            if self.provenance.attempt_id != self.request_id:
                raise ValueError("Execution failure provenance has a different attempt")
        if self.observation is not None:
            if not isinstance(self.observation, ModelExecutionObservation):
                raise ValueError("Invalid execution failure observation")
            if self.provenance is None:
                raise ValueError("Execution failure observation requires provenance")
            if self.observation.backend_id != str(self.provenance.instance_id):
                raise ValueError(
                    "Execution failure observation does not match provenance"
                )
        object.__setattr__(self, "detail", freeze_json(self.detail))

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol": MODEL_EXECUTION_FAILURE_PROTOCOL,
            "request_id": str(self.request_id),
            "kind": self.kind,
            "code": self.code,
            "status_code": self.status_code,
            "detail": thaw_json(self.detail),
            "retry_after": self.retry_after,
            "uncertain": self.uncertain,
            "provenance": None
            if self.provenance is None
            else _provenance_to_wire(self.provenance),
            "observation": None
            if self.observation is None
            else self.observation.to_wire(),
        }

    @classmethod
    def from_wire(cls, value: Any) -> ExecutionFailure:
        expected = {
            "protocol",
            "request_id",
            "kind",
            "code",
            "status_code",
            "detail",
            "retry_after",
            "uncertain",
            "provenance",
            "observation",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value["protocol"] != MODEL_EXECUTION_FAILURE_PROTOCOL
        ):
            raise ValueError("Invalid model execution failure")
        return cls(
            request_id=_uuid(value["request_id"]),
            kind=value["kind"],
            code=value["code"],
            status_code=value["status_code"],
            detail=value["detail"],
            retry_after=value["retry_after"],
            uncertain=value["uncertain"],
            provenance=(
                None
                if value["provenance"] is None
                else _provenance_from_wire(value["provenance"])
            ),
            observation=(
                None
                if value["observation"] is None
                else _observation_from_wire(value["observation"])
            ),
        )


class ModelExecutionError(Exception):
    """A stable framework-neutral model-operation failure."""

    def __init__(self, failure: ExecutionFailure):
        self.failure = failure
        super().__init__(failure.code)


class ModelExecutionPort(Protocol):
    async def execute(self, request: FrozenModelOperation) -> ModelOperationResult: ...


_TRACKER_RESPONSE_MAX_BYTES = 64 * 1024 * 1024


class _FrozenJSONEncoder(json.JSONEncoder):
    def default(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return dict(value)
        return super().default(value)


def tracker_response_digest(
    result: ModelTaskResult, *, raw_meta: Mapping[str, Any] | None = None
) -> str:
    """Match the existing bounded digest, including private raw session metadata."""
    digest = hashlib.sha256()
    size = 0
    encoder = _FrozenJSONEncoder(
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    digest_meta = result.meta if raw_meta is None else raw_meta
    for chunk in encoder.iterencode({"result": result.result, "meta": digest_meta}):
        content = chunk.encode("utf-8")
        size += len(content)
        if size > _TRACKER_RESPONSE_MAX_BYTES:
            raise ValueError("tracker_response_too_large")
        digest.update(content)
    return digest.hexdigest()


def model_operation_request_bytes(request: FrozenModelOperation) -> bytes:
    return _json_bytes(request.to_wire())


def decode_model_operation_request(
    value: bytes | str | Mapping[str, Any],
) -> FrozenModelOperation:
    try:
        if type(value) is bytes:
            value = json.loads(value)
        elif type(value) is str:
            value = json.loads(value)
        return FrozenModelOperation.from_wire(value)
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        json.JSONDecodeError,
    ):
        raise ValueError("Invalid model execution request") from None


def decode_model_operation_result(
    value: bytes | str | Mapping[str, Any],
) -> ModelOperationResult:
    try:
        if type(value) is bytes:
            value = json.loads(value)
        elif type(value) is str:
            value = json.loads(value)
        return ModelOperationResult.from_wire(value)
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        json.JSONDecodeError,
    ):
        raise ValueError("Invalid model execution result") from None


def model_operation_result_bytes(result: ModelOperationResult) -> bytes:
    return _json_bytes(result.to_wire())


def model_execution_failure_bytes(failure: ExecutionFailure) -> bytes:
    return _json_bytes(failure.to_wire())


def decode_model_execution_failure(
    value: bytes | str | Mapping[str, Any],
) -> ExecutionFailure:
    try:
        if type(value) is bytes:
            value = json.loads(value)
        elif type(value) is str:
            value = json.loads(value)
        return ExecutionFailure.from_wire(value)
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        json.JSONDecodeError,
    ):
        raise ValueError("Invalid model execution failure") from None
