"""Strict Inference control operations and credential-safe public facts."""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .backend_endpoint import (
    BackendEndpointAuthority,
    BackendEndpointBinding,
    BackendEndpointSubject,
)

BACKEND_CONTROL_PROTOCOL = "aap-inference-backend-control/v1"
BACKEND_CONTROL_REQUEST_MAX_BYTES = 262144
BACKEND_CONTROL_REPLY_MAX_BYTES = 4194304
BackendControlOperation = Literal[
    "setup",
    "health_meta",
    "unload",
    "reload",
    "warmup",
    "catalog_fallback",
    "stats",
    "install_trained_model",
    "trained_model_receipt",
    "managed_evaluation_profile",
    "managed_evaluation_receipt",
    "managed_evaluation_seal",
    "versions",
]
BackendRequestOnlyOperation = Literal["manual_probe", "environment_observation"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BackendControlFailure(_StrictModel):
    """Sanitized operation failure to map at the original API caller."""

    code: StrictStr = Field(pattern=r"^[a-z0-9_]{1,96}$")
    status_code: StrictInt | None = Field(default=None, ge=100, le=599)
    detail: Any = None
    retry_after: StrictStr | None = None


class BackendCatalogFact(_StrictModel):
    """Catalog row safe for application consumers; never includes auth grouping keys."""

    registry_id: uuid.UUID
    name: StrictStr
    url: StrictStr
    state: StrictStr
    source: StrictStr
    is_interactive: StrictBool
    health_meta: dict[str, Any] | None


class BackendStatsFact(BackendCatalogFact):
    created_at: datetime


class BackendCredentialPolicyFact(_StrictModel):
    """Non-secret stored credential policy for a controlled-model admission check."""

    auth_method: StrictStr
    auth_token_length_valid: StrictBool


class BackendStatsSnapshotBinding(_StrictModel):
    backend_id: uuid.UUID
    backend_name: StrictStr
    project_id: None = None
    project_display_id: StrictStr | None = None
    project_name: StrictStr | None = None


class BackendStatsSnapshot(_StrictModel):
    """One sanitized public aggregate; endpoint credentials and group keys stay private."""

    physical_key: StrictStr
    url_host: StrictStr | None
    backend_id: uuid.UUID
    backend_name: StrictStr
    bindings: tuple[BackendStatsSnapshotBinding, ...]
    state: Literal["ok", "error"]
    timestamp: StrictStr
    meta: dict[str, Any] | None = None


class BackendStatsSnapshotsRequest(_StrictModel):
    kind: Literal["owner_stats_snapshots"]
    schema_: Literal["aap-inference-backend-control/v1"] = Field(alias="schema")
    request_id: uuid.UUID


class BackendStatsSnapshotsResult(_StrictModel):
    request_id: uuid.UUID
    snapshots: tuple[BackendStatsSnapshot, ...]


class BackendRegistrationLabel(_StrictModel):
    url: StrictStr
    source: StrictStr


class BackendRequestOnlyEndpoint(_StrictModel):
    """One caller-supplied endpoint; never looked up in the registry."""

    url: StrictStr = Field(min_length=1, max_length=1000)
    auth_method: Literal["none", "token"] = "none"
    auth_token: StrictStr | None = Field(default=None, max_length=500, repr=False)

    @model_validator(mode="after")
    def safe_endpoint(self) -> BackendRequestOnlyEndpoint:
        value = self.url
        if any(
            ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in value
        ):
            raise ValueError("invalid request-only endpoint URL")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("invalid request-only endpoint URL") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or port == 0
            or (self.auth_method == "token" and not self.auth_token)
            or (self.auth_method == "none" and self.auth_token is not None)
        ):
            raise ValueError("invalid request-only endpoint URL")
        return self


class BackendControlRequest(_StrictModel):
    kind: Literal["registered"]
    schema_: Literal["aap-inference-backend-control/v1"] = Field(alias="schema")
    operation: BackendControlOperation
    request_id: uuid.UUID
    registry_id: uuid.UUID
    endpoint_subject: BackendEndpointSubject
    binding: BackendEndpointBinding | None = Field(..., repr=False)
    body: dict[str, Any] | None = None

    @model_validator(mode="after")
    def subject_matches(self) -> BackendControlRequest:
        expected_purpose = {
            "setup": "setup",
            "health_meta": "health_observation",
            "unload": "lifecycle",
            "reload": "lifecycle",
            "warmup": "execution",
            "catalog_fallback": "catalog_fallback",
            "stats": "stats",
            "install_trained_model": "execution",
            "trained_model_receipt": "backend_read",
            "managed_evaluation_profile": "backend_read",
            "managed_evaluation_receipt": "backend_read",
            "managed_evaluation_seal": "execution",
            "versions": "backend_read",
        }[self.operation]
        if (
            self.endpoint_subject.registry_id != self.registry_id
            or self.endpoint_subject.request_id != self.request_id
            or self.endpoint_subject.purpose != expected_purpose
        ):
            raise ValueError("backend control endpoint does not match operation")
        binding_kind = {
            "unload": "control",
            "reload": "control",
            "warmup": "scope",
            "install_trained_model": "scope",
            "managed_evaluation_seal": "scope",
        }.get(self.operation)
        if binding_kind is None:
            if self.binding is not None or self.endpoint_subject.authority is not None:
                raise ValueError("read endpoint cannot carry route authority")
        elif (
            self.binding is None
            or self.binding.kind != binding_kind
            or self.binding.attempt_id != self.request_id
            or self.endpoint_subject.authority
            != BackendEndpointAuthority(
                kind=self.binding.kind,
                scope_id=self.binding.scope_id,
                control_id=self.binding.control_id,
                attempt_id=self.binding.attempt_id,
            )
        ):
            raise ValueError("backend control authority does not match endpoint")
        if (
            self.operation
            in {
                "setup",
                "health_meta",
                "unload",
                "catalog_fallback",
                "stats",
                "managed_evaluation_profile",
                "versions",
            }
            and self.body is not None
        ):
            raise ValueError("backend control operation does not accept a body")
        if (
            self.operation == "reload"
            and self.body is not None
            and (
                any(
                    key not in {"sam_variant", "dino_variant", "modality", "task_type"}
                    for key in self.body
                )
                or any(
                    type(value) is not str or not value for value in self.body.values()
                )
            )
        ):
            raise ValueError("invalid backend reload request")
        if (
            self.operation
            in {
                "warmup",
                "install_trained_model",
                "trained_model_receipt",
                "managed_evaluation_receipt",
                "managed_evaluation_seal",
            }
            and self.body is None
        ):
            raise ValueError("backend control operation requires its typed body")
        if self.operation == "trained_model_receipt":
            version_id = (self.body or {}).get("version_id")
            if set(self.body or {}) != {"version_id"} or type(version_id) is not str:
                raise ValueError("invalid trained model receipt request")
            try:
                uuid.UUID(version_id)
            except (TypeError, ValueError, AttributeError):
                raise ValueError("invalid trained model receipt request") from None
        if self.operation == "managed_evaluation_receipt":
            body = self.body or {}
            if set(body) != {"request_id", "binding_digest", "storage_identity"}:
                raise ValueError("invalid managed evaluation receipt request")
            try:
                uuid.UUID(body["request_id"])
            except (TypeError, ValueError, AttributeError):
                raise ValueError("invalid managed evaluation receipt request") from None
            if (
                type(body["binding_digest"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", body["binding_digest"]) is None
                or type(body["storage_identity"]) is not str
            ):
                raise ValueError("invalid managed evaluation receipt request")
        if self.operation == "managed_evaluation_seal":
            body = self.body or {}
            expected = {
                "request_id",
                "binding_digest",
                "profile_digest",
                "storage_identity",
                "issued_at",
                "expires_at",
            }
            if set(body) != expected:
                raise ValueError("invalid managed evaluation seal request")
            try:
                uuid.UUID(body["request_id"])
            except (TypeError, ValueError, AttributeError):
                raise ValueError("invalid managed evaluation seal request") from None
            if (
                any(
                    type(body[key]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", body[key]) is None
                    for key in ("binding_digest", "profile_digest")
                )
                or type(body["storage_identity"]) is not str
                or type(body["issued_at"]) is not int
                or type(body["expires_at"]) is not int
            ):
                raise ValueError("invalid managed evaluation seal request")
        return self


class BackendRequestOnlyProbeRequest(_StrictModel):
    kind: Literal["request_only"]
    schema_: Literal["aap-inference-backend-control/v1"] = Field(alias="schema")
    operation: BackendRequestOnlyOperation
    request_id: uuid.UUID
    endpoints: tuple[BackendRequestOnlyEndpoint, ...]
    timeout_seconds: int | float

    @model_validator(mode="after")
    def probe_cardinality(self) -> BackendRequestOnlyProbeRequest:
        if (
            type(self.timeout_seconds) not in {int, float}
            or self.timeout_seconds <= 0
            or not math.isfinite(float(self.timeout_seconds))
        ):
            raise ValueError("invalid request-only probe timeout")
        if self.operation == "manual_probe" and len(self.endpoints) != 1:
            raise ValueError("manual probe requires exactly one endpoint")
        if not self.endpoints:
            raise ValueError("request-only probe requires an endpoint")
        if len({endpoint.url for endpoint in self.endpoints}) != len(self.endpoints):
            raise ValueError("request-only endpoints must be unique")
        if self.operation == "environment_observation" and any(
            endpoint.auth_method != "none" for endpoint in self.endpoints
        ):
            raise ValueError("environment observations cannot carry credentials")
        return self


class BackendUnregisteredSmokeOptions(_StrictModel):
    sam_variant: StrictStr | None = None
    dino_variant: StrictStr | None = None


class BackendSmokeRequest(_StrictModel):
    kind: Literal["smoke_unregistered"]
    schema_: Literal["aap-inference-backend-control/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    endpoint: BackendRequestOnlyEndpoint
    body: BackendUnregisteredSmokeOptions


class BackendUnregisteredSmokeResult(_StrictModel):
    reload: dict[str, Any]
    auto_unloaded: StrictBool
    load_latency_ms: StrictInt = Field(ge=0)


class BackendControlObservation(_StrictModel):
    """One original BackendTransport observer callback fact from the owner."""

    backend_id: StrictStr | None
    outcome: StrictStr
    duration_seconds: StrictFloat = Field(ge=0)

    @field_validator("duration_seconds")
    @classmethod
    def finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("invalid backend observation duration")
        return value


class BackendControlReply(_StrictModel):
    schema_: Literal["aap-inference-backend-control/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    result: dict[str, Any] | list[Any] | None = None
    observations: tuple[BackendControlObservation, ...] = ()
    error: BackendControlFailure | None = None

    @model_validator(mode="after")
    def one_result(self) -> BackendControlReply:
        if (self.result is None) == (self.error is None):
            raise ValueError("backend control reply must contain exactly one result")
        return self


class BackendRequestOnlyProbeFact(_StrictModel):
    url: StrictStr
    ok: StrictBool
    latency_ms: StrictInt = Field(ge=0)
    status_code: StrictInt | None = Field(default=None, ge=100, le=599)
    error: StrictStr | None = None
    health: dict[str, Any] | None = None
    setup: dict[str, Any] | None = None


class BackendRequestOnlyProbeReply(_StrictModel):
    request_id: uuid.UUID
    results: tuple[BackendRequestOnlyProbeFact, ...]


class BackendControlSetupResult(_StrictModel):
    registry_id: uuid.UUID
    setup: dict[str, Any] | None = None
    error: BackendControlFailure | None = None

    @model_validator(mode="after")
    def setup_or_error(self) -> BackendControlSetupResult:
        if (self.setup is None) == (self.error is None):
            raise ValueError("backend setup result must contain setup or error")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _request_model(value: object, *, json_data: str | None = None):
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    kind = value.get("kind")
    models = {
        "registered": BackendControlRequest,
        "request_only": BackendRequestOnlyProbeRequest,
        "smoke_unregistered": BackendSmokeRequest,
        "owner_stats_snapshots": BackendStatsSnapshotsRequest,
    }
    try:
        model = models[kind]
        return (
            model.model_validate_json(json_data)
            if json_data is not None
            else model.model_validate(value)
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise ValueError("invalid backend control request") from None


def backend_control_request_bytes(value) -> bytes:
    try:
        request = (
            value
            if isinstance(
                value,
                (
                    BackendControlRequest,
                    BackendRequestOnlyProbeRequest,
                    BackendSmokeRequest,
                    BackendStatsSnapshotsRequest,
                ),
            )
            else _request_model(value)
        )
        raw = json.dumps(
            request.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, ValidationError):
        raise ValueError("invalid backend control request") from None
    if len(raw) > BACKEND_CONTROL_REQUEST_MAX_BYTES:
        raise ValueError("backend control request is too large")
    return raw


def decode_backend_control_request(value):
    json_data = None
    try:
        if isinstance(
            value,
            (
                BackendControlRequest,
                BackendRequestOnlyProbeRequest,
                BackendSmokeRequest,
                BackendStatsSnapshotsRequest,
            ),
        ):
            return value
        if isinstance(value, bytes):
            if len(value) > BACKEND_CONTROL_REQUEST_MAX_BYTES:
                raise ValueError("request too large")
            value = value.decode("ascii")
        if type(value) is str:
            if len(value.encode("ascii")) > BACKEND_CONTROL_REQUEST_MAX_BYTES:
                raise ValueError("request too large")
            json_data = value
            value = json.loads(
                value, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
        return _request_model(value, json_data=json_data)
    except (
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        OverflowError,
        ValidationError,
    ):
        raise ValueError("invalid backend control request") from None


def backend_control_reply_bytes(value) -> bytes:
    try:
        reply = (
            value
            if isinstance(value, BackendControlReply)
            else BackendControlReply.model_validate(value)
        )
        raw = json.dumps(
            reply.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, ValidationError):
        raise ValueError("invalid backend control reply") from None
    if len(raw) > BACKEND_CONTROL_REPLY_MAX_BYTES:
        raise ValueError("backend control reply is too large")
    return raw


def decode_backend_control_reply(request, value) -> BackendControlReply:
    json_data = None
    try:
        if isinstance(value, BackendControlReply):
            reply = value
        else:
            if isinstance(value, bytes):
                if len(value) > BACKEND_CONTROL_REPLY_MAX_BYTES:
                    raise ValueError("reply too large")
                value = value.decode("ascii")
            if type(value) is str:
                if len(value.encode("ascii")) > BACKEND_CONTROL_REPLY_MAX_BYTES:
                    raise ValueError("reply too large")
                json_data = value
                value = json.loads(
                    value,
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
            reply = (
                BackendControlReply.model_validate_json(json_data)
                if json_data is not None
                else BackendControlReply.model_validate(value)
            )
        if reply.request_id != request.request_id:
            raise ValueError("reply request identity mismatch")
        return reply
    except (
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        OverflowError,
        ValidationError,
    ):
        raise ValueError("invalid backend control reply") from None


__all__ = [
    "BACKEND_CONTROL_PROTOCOL",
    "BACKEND_CONTROL_REPLY_MAX_BYTES",
    "BACKEND_CONTROL_REQUEST_MAX_BYTES",
    "BackendCredentialPolicyFact",
    "BackendCatalogFact",
    "BackendControlObservation",
    "BackendControlFailure",
    "BackendControlOperation",
    "BackendControlReply",
    "BackendControlRequest",
    "BackendControlSetupResult",
    "BackendUnregisteredSmokeOptions",
    "BackendUnregisteredSmokeResult",
    "BackendRegistrationLabel",
    "BackendRequestOnlyEndpoint",
    "BackendRequestOnlyOperation",
    "BackendRequestOnlyProbeFact",
    "BackendRequestOnlyProbeReply",
    "BackendRequestOnlyProbeRequest",
    "BackendSmokeRequest",
    "BackendStatsFact",
    "BackendStatsSnapshot",
    "BackendStatsSnapshotBinding",
    "BackendStatsSnapshotsRequest",
    "BackendStatsSnapshotsResult",
    "backend_control_reply_bytes",
    "backend_control_request_bytes",
    "decode_backend_control_reply",
    "decode_backend_control_request",
]
