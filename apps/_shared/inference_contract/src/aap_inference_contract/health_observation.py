"""Closed wire contract for sealed backend health observations."""

from __future__ import annotations

import base64
import binascii
import hashlib
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

HEALTH_OBSERVATION_PROTOCOL = "aap-inference-health-observation/v1"
HEALTH_OBSERVATION_REQUEST_MAX_BYTES = 24 * 1024 * 1024
HEALTH_OBSERVATION_REPLY_MAX_BYTES = 24 * 1024 * 1024
HEALTH_OBSERVATION_NATIVE_POLICY_SCHEMA = "aap-inference-health-observation-native/v1"
HEALTH_OBSERVATION_SUBJECT_PROTOCOL = "aap-inference-health-observation-subject/v1"
HEALTH_OBSERVATION_EVIDENCE_PROTOCOL = "aap-inference-health-observation-evidence/v1"
HEALTH_OBSERVATION_NATIVE_FUNCTIONS = (
    "aap_inference_health_observation_v1.health_observation_policy()",
    "aap_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision)",
    "aap_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid)",
    "aap_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)",
    "aap_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)",
)

_NATIVE_POLICY_KEYS = frozenset(
    {
        "schema",
        "observation_protocol",
        "subject_protocol",
        "evidence_protocol",
        "origin_transaction",
        "evidence_protection",
        "functions",
        "ready",
    }
)
_JSON_PARSE_ERRORS = (
    UnicodeError,
    ValueError,
    TypeError,
    RecursionError,
    OverflowError,
    ValidationError,
)


class BackendHealthObservationInvalidRequest(ValueError):
    """The local request or provider wire data is malformed or out of bounds."""

    def __init__(self) -> None:
        super().__init__("Invalid Inference health observation contract")


class BackendHealthObservationUnavailable(Exception):
    """The selected health observation provider is unavailable or malformed."""

    def __init__(self) -> None:
        super().__init__("Inference health observation unavailable")


class _HealthObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _validate_opaque(value: str, *, max_bytes: int) -> None:
    if type(value) is not str or len(value) > max_bytes or not value.startswith("v1."):
        raise ValueError("Invalid opaque health observation value")
    encoded = value[3:]
    if not encoded or not encoded.isascii():
        raise ValueError("Invalid opaque health observation value")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid opaque health observation value") from exc
    if not decoded or base64.b64encode(decoded).decode("ascii") != encoded:
        raise ValueError("Invalid opaque health observation value")


class BackendHealthObservationRequest(_HealthObservationModel):
    schema_: Literal["aap-inference-health-observation/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    registry_id: uuid.UUID
    subject: StrictStr = Field(repr=False)

    @model_validator(mode="after")
    def valid_subject(self) -> BackendHealthObservationRequest:
        _validate_opaque(self.subject, max_bytes=HEALTH_OBSERVATION_REQUEST_MAX_BYTES)
        return self


class BackendHealthObservationReply(_HealthObservationModel):
    schema_: Literal["aap-inference-health-observation/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    registry_id: uuid.UUID
    subject_sha256: StrictStr
    evidence: StrictStr = Field(repr=False)

    @model_validator(mode="after")
    def valid_digest_and_evidence(self) -> BackendHealthObservationReply:
        digest = self.subject_sha256
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("Invalid health observation subject digest")
        _validate_opaque(self.evidence, max_bytes=HEALTH_OBSERVATION_REPLY_MAX_BYTES)
        return self


_REQUEST_ADAPTER = TypeAdapter(BackendHealthObservationRequest)
_REPLY_ADAPTER = TypeAdapter(BackendHealthObservationReply)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def health_observation_policy_ready(value: str | bytes | dict) -> bool:
    """Validate exact Native readiness facts, returning false for malformed data."""
    try:
        if isinstance(value, bytes):
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
    except _JSON_PARSE_ERRORS:
        return False

    if type(parsed) is not dict or set(parsed) != _NATIVE_POLICY_KEYS:
        return False
    functions = parsed["functions"]
    return bool(
        type(parsed["schema"]) is str
        and parsed["schema"] == HEALTH_OBSERVATION_NATIVE_POLICY_SCHEMA
        and type(parsed["observation_protocol"]) is str
        and parsed["observation_protocol"] == HEALTH_OBSERVATION_PROTOCOL
        and type(parsed["subject_protocol"]) is str
        and parsed["subject_protocol"] == HEALTH_OBSERVATION_SUBJECT_PROTOCOL
        and type(parsed["evidence_protocol"]) is str
        and parsed["evidence_protocol"] == HEALTH_OBSERVATION_EVIDENCE_PROTOCOL
        and type(parsed["origin_transaction"]) is str
        and parsed["origin_transaction"] == "in_progress"
        and type(parsed["evidence_protection"]) is str
        and parsed["evidence_protection"] == "pgcrypto-pgp-aes256-mdc"
        and type(functions) is list
        and all(type(name) is str for name in functions)
        and functions == list(HEALTH_OBSERVATION_NATIVE_FUNCTIONS)
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


def _json_input(value: object, *, max_bytes: int) -> bytes:
    if isinstance(value, bytes):
        encoded = value
    elif type(value) is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise BackendHealthObservationInvalidRequest() from None
    else:
        try:
            encoded = json.dumps(
                _json_ready(value),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise BackendHealthObservationInvalidRequest() from None
    if len(encoded) > max_bytes:
        raise BackendHealthObservationInvalidRequest()
    try:
        parsed = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except _JSON_PARSE_ERRORS:
        raise BackendHealthObservationInvalidRequest() from None
    if type(parsed) is not dict:
        raise BackendHealthObservationInvalidRequest()
    return encoded


def _decode_request(value: object) -> BackendHealthObservationRequest:
    encoded = _json_input(value, max_bytes=HEALTH_OBSERVATION_REQUEST_MAX_BYTES)
    try:
        return _REQUEST_ADAPTER.validate_json(encoded)
    except _JSON_PARSE_ERRORS:
        raise BackendHealthObservationInvalidRequest() from None


def decode_health_observation_request(data: object) -> BackendHealthObservationRequest:
    """Decode and revalidate a request model, mapping, or JSON payload."""
    return _decode_request(data)


def _subject_sha256(subject: str) -> str:
    return hashlib.sha256(subject.encode("ascii")).hexdigest()


def make_health_observation_request(
    registry_id: uuid.UUID,
    subject: str,
    request_id: uuid.UUID | None = None,
) -> BackendHealthObservationRequest:
    """Build the closed private request from a captured opaque Native subject."""
    if type(registry_id) is not uuid.UUID or (
        request_id is not None and type(request_id) is not uuid.UUID
    ):
        raise BackendHealthObservationInvalidRequest()
    try:
        request = _REQUEST_ADAPTER.validate_python(
            {
                "schema": HEALTH_OBSERVATION_PROTOCOL,
                "request_id": request_id or uuid.uuid4(),
                "registry_id": registry_id,
                "subject": subject,
            }
        )
    except _JSON_PARSE_ERRORS:
        raise BackendHealthObservationInvalidRequest() from None
    health_observation_request_bytes(request)
    return request


def health_observation_request_bytes(request: object) -> bytes:
    """Serialize a validated request as deterministic finite ASCII JSON."""
    typed = _decode_request(request)
    encoded = json.dumps(
        typed.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > HEALTH_OBSERVATION_REQUEST_MAX_BYTES:
        raise BackendHealthObservationInvalidRequest()
    return encoded


def decode_health_observation_reply(
    request: BackendHealthObservationRequest | object,
    data: object,
) -> BackendHealthObservationReply:
    """Decode a reply and bind it to the request and its opaque subject."""
    typed_request = _decode_request(request)
    try:
        encoded = _json_input(data, max_bytes=HEALTH_OBSERVATION_REPLY_MAX_BYTES)
    except BackendHealthObservationInvalidRequest:
        raise BackendHealthObservationUnavailable() from None
    try:
        reply = _REPLY_ADAPTER.validate_json(encoded)
    except _JSON_PARSE_ERRORS:
        raise BackendHealthObservationUnavailable() from None
    if (
        reply.request_id != typed_request.request_id
        or reply.registry_id != typed_request.registry_id
        or reply.subject_sha256 != _subject_sha256(typed_request.subject)
    ):
        raise BackendHealthObservationUnavailable()
    return reply


def health_observation_reply_bytes(reply: object) -> bytes:
    """Serialize a validated sealed observation reply within the wire budget."""
    try:
        typed = _REPLY_ADAPTER.validate_json(
            _json_input(reply, max_bytes=HEALTH_OBSERVATION_REPLY_MAX_BYTES)
        )
        encoded = json.dumps(
            typed.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except _JSON_PARSE_ERRORS:
        raise BackendHealthObservationInvalidRequest() from None
    if len(encoded) > HEALTH_OBSERVATION_REPLY_MAX_BYTES:
        raise BackendHealthObservationInvalidRequest()
    return encoded


__all__ = [
    "HEALTH_OBSERVATION_EVIDENCE_PROTOCOL",
    "HEALTH_OBSERVATION_NATIVE_FUNCTIONS",
    "HEALTH_OBSERVATION_NATIVE_POLICY_SCHEMA",
    "HEALTH_OBSERVATION_PROTOCOL",
    "HEALTH_OBSERVATION_REPLY_MAX_BYTES",
    "HEALTH_OBSERVATION_REQUEST_MAX_BYTES",
    "HEALTH_OBSERVATION_SUBJECT_PROTOCOL",
    "BackendHealthObservationInvalidRequest",
    "BackendHealthObservationReply",
    "BackendHealthObservationRequest",
    "BackendHealthObservationUnavailable",
    "decode_health_observation_reply",
    "decode_health_observation_request",
    "health_observation_policy_ready",
    "health_observation_reply_bytes",
    "health_observation_request_bytes",
    "make_health_observation_request",
]
