"""Private, purpose-bound capability for opening exact registered backend endpoints."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

BACKEND_ENDPOINT_PROTOCOL = "aap-inference-backend-endpoint/v1"
BACKEND_ENDPOINT_NATIVE_SCHEMA = "aap-inference-backend-endpoint-native/v1"
BACKEND_ENDPOINT_SUBJECT_PROTOCOL = "aap-inference-backend-endpoint-subject/v1"
BACKEND_ENDPOINT_MAX_BYTES = 25_165_824
BACKEND_ENDPOINT_NATIVE_FUNCTIONS = (
    "aap_inference_backend_endpoint_v1.backend_endpoint_policy()",
    "aap_inference_backend_endpoint_v1.controlled_model_credential_policy(uuid)",
    "aap_inference_backend_endpoint_v1.capture_backend_endpoint_subject(uuid,uuid,text,text)",
    "aap_inference_backend_endpoint_v1.open_health_observation_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.open_setup_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.open_catalog_fallback_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.open_stats_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.open_backend_read_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.open_lifecycle_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.open_execution_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.open_gpu_control_endpoint(text,uuid,uuid,text)",
    "aap_inference_backend_endpoint_v1.health_backend_row(uuid,boolean)",
    "aap_inference_backend_endpoint_v1.stats_endpoint_rows()",
    "aap_inference_backend_endpoint_v1.catalog_endpoint_rows()",
    "aap_inference_backend_endpoint_v1.observation_registration_labels(text[])",
)

BackendEndpointPurpose = Literal[
    "health_observation",
    "setup",
    "catalog_fallback",
    "stats",
    "backend_read",
    "lifecycle",
    "execution",
    "gpu_control",
]


class _EndpointModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BackendEndpointAuthority(_EndpointModel):
    """Non-secret route/control identity retained in the sealed subject reply."""

    kind: Literal["scope", "control"]
    scope_id: uuid.UUID | None = Field(...)
    control_id: uuid.UUID | None = Field(...)
    attempt_id: uuid.UUID

    @model_validator(mode="after")
    def valid_authority(self) -> BackendEndpointAuthority:
        if self.kind == "scope":
            if self.scope_id is None or self.control_id is not None:
                raise ValueError("Scope endpoint binding requires only a scope ID")
        elif self.control_id is None or self.scope_id is not None:
            raise ValueError("Control endpoint binding requires only a control ID")
        return self


class BackendEndpointBinding(BackendEndpointAuthority):
    """Origin-only route/control binding; capability is never returned to callers."""

    capability: StrictStr = Field(repr=False)

    @model_validator(mode="after")
    def valid_capability(self) -> BackendEndpointBinding:
        if len(self.capability) != 64 or any(
            character not in "0123456789abcdef" for character in self.capability
        ):
            raise ValueError("Endpoint authority requires a valid capability")
        return self


class BackendEndpointSubject(_EndpointModel):
    """Purpose-bound sealed exact endpoint snapshot."""

    schema_: Literal["aap-inference-backend-endpoint/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    registry_id: uuid.UUID
    purpose: BackendEndpointPurpose
    authority: BackendEndpointAuthority | None = Field(...)
    subject: StrictStr = Field(repr=False)

    @model_validator(mode="after")
    def valid_subject(self) -> BackendEndpointSubject:
        read_only = self.purpose in {
            "health_observation",
            "setup",
            "catalog_fallback",
            "stats",
            "backend_read",
        }
        if read_only:
            if self.authority is not None:
                raise ValueError("Read endpoint subjects cannot carry route authority")
        else:
            if self.authority is None or self.request_id != self.authority.attempt_id:
                raise ValueError(
                    "Stateful endpoint subjects require matching authority"
                )
            if self.purpose == "execution" and self.authority.kind != "scope":
                raise ValueError("Execution endpoint subjects require a route scope")
            if self.purpose == "lifecycle" and self.authority.kind != "control":
                raise ValueError(
                    "Lifecycle endpoint subjects require control authority"
                )
        value = self.subject
        if (
            type(value) is not str
            or len(value) > BACKEND_ENDPOINT_MAX_BYTES
            or not value.startswith("v1.")
        ):
            raise ValueError("Invalid opaque backend endpoint subject")
        encoded = value[3:]
        if not encoded or not encoded.isascii():
            raise ValueError("Invalid opaque backend endpoint subject")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid opaque backend endpoint subject") from exc
        if not decoded or base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError("Invalid opaque backend endpoint subject")
        return self


_POLICY_KEYS = frozenset(
    {
        "schema",
        "subject_protocol",
        "origin_transaction",
        "open_transaction",
        "protection",
        "functions",
        "ready",
    }
)
_JSON_ERRORS = (
    UnicodeError,
    ValueError,
    TypeError,
    RecursionError,
    OverflowError,
    ValidationError,
)
_SUBJECT_ADAPTER = TypeAdapter(BackendEndpointSubject)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def backend_endpoint_policy_ready(value: str | bytes | dict) -> bool:
    """Accept only the exact endpoint Native policy and function inventory."""
    try:
        if isinstance(value, bytes):
            parsed = json.loads(
                value, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
        elif type(value) is str:
            parsed = json.loads(
                value, object_pairs_hook=_unique_object, parse_constant=_reject_constant
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
        parsed["schema"] == BACKEND_ENDPOINT_NATIVE_SCHEMA
        and type(parsed["schema"]) is str
        and parsed["subject_protocol"] == BACKEND_ENDPOINT_SUBJECT_PROTOCOL
        and type(parsed["subject_protocol"]) is str
        and parsed["origin_transaction"] == "caller_owned"
        and type(parsed["origin_transaction"]) is str
        and parsed["open_transaction"] == "read_in_progress_mutation_live"
        and type(parsed["open_transaction"]) is str
        and parsed["protection"] == "pgcrypto-pgp-aes256-mdc"
        and type(parsed["protection"]) is str
        and type(parsed["functions"]) is list
        and all(type(item) is str for item in parsed["functions"])
        and parsed["functions"] == list(BACKEND_ENDPOINT_NATIVE_FUNCTIONS)
        and type(parsed["ready"]) is bool
        and parsed["ready"] is True
    )


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Invalid JSON value")


def decode_backend_endpoint_subject(value: object) -> BackendEndpointSubject:
    """Strictly decode a subject from a model, mapping, JSON text, or bytes."""
    try:
        if isinstance(value, BackendEndpointSubject):
            typed = _SUBJECT_ADAPTER.validate_python(
                value.model_dump(mode="python", by_alias=True)
            )
        elif isinstance(value, bytes):
            typed = _SUBJECT_ADAPTER.validate_json(value)
        elif type(value) is str:
            typed = _SUBJECT_ADAPTER.validate_json(value)
        else:
            encoded = json.dumps(
                _json_ready(value),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            if len(encoded) > BACKEND_ENDPOINT_MAX_BYTES:
                raise ValueError("Endpoint subject request is too large")
            typed = _SUBJECT_ADAPTER.validate_json(encoded)
        if len(backend_endpoint_subject_bytes(typed)) > BACKEND_ENDPOINT_MAX_BYTES:
            raise ValueError("Endpoint subject request is too large")
        return typed
    except _JSON_ERRORS:
        raise ValueError("Invalid backend endpoint subject") from None


def backend_endpoint_subject_bytes(value: object) -> bytes:
    typed = _SUBJECT_ADAPTER.validate_python(
        value.model_dump(mode="python", by_alias=True)
        if isinstance(value, BackendEndpointSubject)
        else value
    )
    encoded = json.dumps(
        typed.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > BACKEND_ENDPOINT_MAX_BYTES:
        raise ValueError("Endpoint subject request is too large")
    return encoded


def make_backend_endpoint_subject(
    registry_id: uuid.UUID,
    *,
    request_id: uuid.UUID,
    purpose: BackendEndpointPurpose,
    binding: BackendEndpointBinding | None,
    subject: str,
) -> BackendEndpointSubject:
    """Construct the private opener request from the Native endpoint subject."""
    authority = (
        {
            "kind": binding.kind,
            "scope_id": binding.scope_id,
            "control_id": binding.control_id,
            "attempt_id": binding.attempt_id,
        }
        if binding is not None
        else None
    )
    return decode_backend_endpoint_subject(
        {
            "schema": BACKEND_ENDPOINT_PROTOCOL,
            "request_id": request_id,
            "registry_id": registry_id,
            "purpose": purpose,
            "authority": authority,
            "subject": subject,
        }
    )


__all__ = [
    "BACKEND_ENDPOINT_MAX_BYTES",
    "BACKEND_ENDPOINT_NATIVE_FUNCTIONS",
    "BACKEND_ENDPOINT_NATIVE_SCHEMA",
    "BACKEND_ENDPOINT_PROTOCOL",
    "BACKEND_ENDPOINT_SUBJECT_PROTOCOL",
    "BackendEndpointAuthority",
    "BackendEndpointBinding",
    "BackendEndpointPurpose",
    "BackendEndpointSubject",
    "backend_endpoint_policy_ready",
    "backend_endpoint_subject_bytes",
    "decode_backend_endpoint_subject",
    "make_backend_endpoint_subject",
]
