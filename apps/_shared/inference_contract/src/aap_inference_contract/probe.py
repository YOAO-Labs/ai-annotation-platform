"""Closed request/reply contract for credential-owning backend setup probes."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .capabilities import CapabilityLimits

BACKEND_PROBE_PROTOCOL = "aap-inference-backend-probe/v1"
BACKEND_PROBE_REQUEST_MAX_BYTES = 16 * 1024 * 1024
BACKEND_PROBE_REPLY_MAX_BYTES = 4096
BACKEND_PROBE_NATIVE_POLICY_SCHEMA = "aap-inference-backend-probe-native/v1"
BACKEND_PROBE_SUBJECT_PROTOCOL = "aap-inference-backend-probe-subject/v1"
BACKEND_PROBE_NATIVE_FUNCTIONS = (
    "aap_inference_backend_probe_v1.backend_probe_policy()",
    "aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)",
)
_MODALITY_ORDER = ("image", "video", "lidar")
_NATIVE_POLICY_KEYS = frozenset(
    {
        "schema",
        "probe_protocol",
        "subject_protocol",
        "origin_transaction",
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


class BackendProbeInvalidRequest(ValueError):
    """The local request or remote wire data is malformed or out of bounds."""

    def __init__(self) -> None:
        super().__init__("Invalid Inference backend probe contract")


class BackendProbeUnavailable(Exception):
    """The selected Inference probe provider is unavailable or malformed."""

    def __init__(self) -> None:
        super().__init__("Inference backend probe unavailable")


class BackendProbeLegacyFailure(Exception):
    """The probe provider could not materialize the legacy backend facts."""

    def __init__(self) -> None:
        super().__init__("Legacy backend materialization failed")


class _ProbeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _BackendProbeLimits(_ProbeModel):
    video_tracker_window_size_frames: StrictInt
    video_tracker_sam3_window_size_frames: StrictInt

    @model_validator(mode="after")
    def positive_limits(self) -> _BackendProbeLimits:
        if (
            self.video_tracker_window_size_frames <= 0
            or self.video_tracker_sam3_window_size_frames <= 0
        ):
            raise ValueError("Capability limits must be positive integers")
        return self


class BackendProbeRequest(_ProbeModel):
    schema_: Literal["aap-inference-backend-probe/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    registry_id: uuid.UUID
    probe_subject: StrictStr = Field(repr=False)
    timeout_seconds: int | float
    limits: _BackendProbeLimits

    @model_validator(mode="after")
    def validate_probe_subject_and_timeout(self) -> BackendProbeRequest:
        subject = self.probe_subject
        prefix = "v1."
        if type(subject) is not str or not subject.startswith(prefix):
            raise ValueError("Invalid opaque probe subject")
        encoded = subject[len(prefix) :]
        if not encoded or not encoded.isascii():
            raise ValueError("Invalid opaque probe subject")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("Invalid opaque probe subject") from exc
        if not decoded or base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError("Invalid opaque probe subject")

        timeout = self.timeout_seconds
        if type(timeout) not in (int, float):
            raise ValueError("Probe timeout must be a positive finite number")
        try:
            finite_timeout = math.isfinite(float(timeout))
        except (OverflowError, ValueError):
            finite_timeout = False
        if not finite_timeout or timeout <= 0:
            raise ValueError("Probe timeout must be a positive finite number")
        return self


class BackendProbeSuccess(_ProbeModel):
    schema_: Literal["aap-inference-backend-probe/v1"] = Field(alias="schema")
    ok: Literal[True]
    request_id: uuid.UUID
    registry_id: uuid.UUID
    subject_sha256: StrictStr
    modalities: list[Literal["image", "video", "lidar"]] | None

    @model_validator(mode="after")
    def canonical_modalities(self) -> BackendProbeSuccess:
        digest = self.subject_sha256
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("Invalid probe subject digest")
        modalities = self.modalities
        if modalities is not None:
            canonical = [value for value in _MODALITY_ORDER if value in modalities]
            if modalities != canonical:
                raise ValueError("Probe modalities must be unique and canonical")
        return self


class BackendProbeFailure(_ProbeModel):
    schema_: Literal["aap-inference-backend-probe/v1"] = Field(alias="schema")
    ok: Literal[False]
    request_id: uuid.UUID
    registry_id: uuid.UUID
    subject_sha256: StrictStr
    error: Literal["legacy_materialization_failure"]

    @model_validator(mode="after")
    def valid_subject_digest(self) -> BackendProbeFailure:
        digest = self.subject_sha256
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("Invalid probe subject digest")
        return self


BackendProbeReply: TypeAlias = Annotated[
    BackendProbeSuccess | BackendProbeFailure,
    Field(discriminator="ok"),
]

_REQUEST_ADAPTER = TypeAdapter(BackendProbeRequest)
_REPLY_ADAPTER = TypeAdapter(BackendProbeReply)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def backend_probe_policy_ready(value: str | bytes | dict) -> bool:
    """Check the exact fixed Native policy, returning false on every malformed value."""
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
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        return False

    if type(parsed) is not dict or set(parsed) != _NATIVE_POLICY_KEYS:
        return False
    functions = parsed["functions"]
    return bool(
        type(parsed["schema"]) is str
        and parsed["schema"] == BACKEND_PROBE_NATIVE_POLICY_SCHEMA
        and type(parsed["probe_protocol"]) is str
        and parsed["probe_protocol"] == BACKEND_PROBE_PROTOCOL
        and type(parsed["subject_protocol"]) is str
        and parsed["subject_protocol"] == BACKEND_PROBE_SUBJECT_PROTOCOL
        and type(parsed["origin_transaction"]) is str
        and parsed["origin_transaction"] == "in_progress"
        and type(functions) is list
        and all(type(name) is str for name in functions)
        and functions == list(BACKEND_PROBE_NATIVE_FUNCTIONS)
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
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Invalid JSON value")


def _json_input(value: object, *, max_bytes: int) -> bytes:
    if isinstance(value, bytes):
        encoded = value
    elif type(value) is str:
        encoded = value.encode("utf-8")
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
            raise BackendProbeInvalidRequest() from None
    if len(encoded) > max_bytes:
        raise BackendProbeInvalidRequest()
    try:
        parsed = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except _JSON_PARSE_ERRORS:
        raise BackendProbeInvalidRequest() from None
    if type(parsed) is not dict:
        raise BackendProbeInvalidRequest()
    return encoded


def _decode_request(value: object) -> BackendProbeRequest:
    encoded = _json_input(value, max_bytes=BACKEND_PROBE_REQUEST_MAX_BYTES)
    try:
        return _REQUEST_ADAPTER.validate_json(encoded)
    except _JSON_PARSE_ERRORS:
        raise BackendProbeInvalidRequest() from None


def decode_backend_probe_request(data: object) -> BackendProbeRequest:
    """Decode a typed, mapping, or JSON request without trusting model instances."""
    return _decode_request(data)


def _subject_sha256(probe_subject: str) -> str:
    return hashlib.sha256(probe_subject.encode("ascii")).hexdigest()


def make_backend_probe_request(
    registry_id: uuid.UUID,
    probe_subject: str,
    *,
    timeout: int | float,
    limits: CapabilityLimits,
    request_id: uuid.UUID | None = None,
) -> BackendProbeRequest:
    """Build the closed wire request from the captured credential-owning subject."""
    if type(registry_id) is not uuid.UUID or (
        request_id is not None and type(request_id) is not uuid.UUID
    ):
        raise BackendProbeInvalidRequest()
    if type(limits) is not CapabilityLimits:
        raise BackendProbeInvalidRequest()
    try:
        request = _REQUEST_ADAPTER.validate_python(
            {
                "schema": BACKEND_PROBE_PROTOCOL,
                "request_id": request_id or uuid.uuid4(),
                "registry_id": registry_id,
                "probe_subject": probe_subject,
                "timeout_seconds": timeout,
                "limits": {
                    "video_tracker_window_size_frames": limits.video_tracker_window_size_frames,
                    "video_tracker_sam3_window_size_frames": limits.video_tracker_sam3_window_size_frames,
                },
            }
        )
    except _JSON_PARSE_ERRORS:
        raise BackendProbeInvalidRequest() from None
    backend_probe_request_bytes(request)
    return request


def backend_probe_request_bytes(request: object) -> bytes:
    """Serialize a validated request using deterministic, finite ASCII JSON."""
    typed = _decode_request(request)
    encoded = json.dumps(
        typed.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded) > BACKEND_PROBE_REQUEST_MAX_BYTES:
        raise BackendProbeInvalidRequest()
    return encoded


def decode_backend_probe_reply(
    request: BackendProbeRequest | object, data: object
) -> BackendProbeReply:
    """Decode a reply and bind it to every identity in the original request."""
    typed_request = _decode_request(request)
    try:
        encoded = _json_input(data, max_bytes=BACKEND_PROBE_REPLY_MAX_BYTES)
    except BackendProbeInvalidRequest:
        raise BackendProbeUnavailable() from None
    try:
        reply = _REPLY_ADAPTER.validate_json(encoded)
    except _JSON_PARSE_ERRORS:
        raise BackendProbeUnavailable() from None
    if (
        reply.request_id != typed_request.request_id
        or reply.registry_id != typed_request.registry_id
        or reply.subject_sha256 != _subject_sha256(typed_request.probe_subject)
    ):
        raise BackendProbeUnavailable()
    return reply


def backend_probe_reply_bytes(reply: BackendProbeReply) -> bytes:
    """Serialize a closed reply, enforcing the wire-size budget."""
    if type(reply) not in (BackendProbeSuccess, BackendProbeFailure):
        raise BackendProbeInvalidRequest()
    try:
        typed = _REPLY_ADAPTER.validate_json(
            _json_input(reply, max_bytes=BACKEND_PROBE_REPLY_MAX_BYTES)
        )
        encoded = json.dumps(
            typed.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except _JSON_PARSE_ERRORS:
        raise BackendProbeInvalidRequest() from None
    if len(encoded) > BACKEND_PROBE_REPLY_MAX_BYTES:
        raise BackendProbeInvalidRequest()
    return encoded


def reply_modalities(
    request: BackendProbeRequest | object,
    reply: BackendProbeReply | object,
) -> list[str] | None:
    """Validate reply binding; preserve legacy fail-open nulls and failures."""
    typed = decode_backend_probe_reply(request, reply)
    if isinstance(typed, BackendProbeFailure):
        raise BackendProbeLegacyFailure()
    return list(typed.modalities) if typed.modalities is not None else None
