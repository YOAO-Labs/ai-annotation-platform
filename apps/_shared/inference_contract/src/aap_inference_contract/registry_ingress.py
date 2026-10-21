"""Closed request/result contracts for Registry environment synchronization."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .capabilities import CapabilityLimits

REGISTRY_ENV_SYNC_PROTOCOL = "aap-inference-registry-env-sync/v1"
REGISTRY_ENV_SYNC_STATUS_PROTOCOL = "aap-inference-registry-env-sync-status/v1"
REGISTRY_ENV_SYNC_RESULT_PROTOCOL = "aap-inference-registry-env-sync-result/v1"
REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES = 24 * 1024 * 1024
REGISTRY_ENV_SYNC_REPLY_MAX_BYTES = 24 * 1024 * 1024
REGISTRY_ENV_SYNC_NATIVE_POLICY_SCHEMA = "aap-inference-registry-ingress-native/v1"
REGISTRY_INGRESS_DATA_PROTOCOL = "aap-inference-registry-ingress/v1"
REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS = (
    "aap_inference_registry_ingress_v1.registry_ingress_policy()",
    "aap_inference_registry_ingress_v1.registry_by_url(text)",
    "aap_inference_registry_ingress_v1.registry_candidates(text)",
    "aap_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)",
    "aap_inference_registry_ingress_v1.singleton_pool(uuid)",
    "aap_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text)",
    "aap_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)",
    "aap_inference_registry_ingress_v1.disconnect_env_registry(uuid,text)",
    "aap_inference_registry_ingress_v1.tag_seed_registry(uuid,text)",
    "aap_inference_registry_ingress_v1.sync_result(uuid,text)",
    "aap_inference_registry_ingress_v1.record_sync_result(uuid,text,integer)",
)

_POLICY_KEYS = frozenset(
    {"schema", "data_protocol", "transaction", "functions", "ready"}
)
_JSON_ERRORS = (
    UnicodeError,
    ValueError,
    TypeError,
    RecursionError,
    OverflowError,
    ValidationError,
)


class RegistryEnvSyncUnavailable(Exception):
    """The selected Registry environment synchronization owner is unavailable."""

    def __init__(self) -> None:
        super().__init__("Inference Registry environment sync unavailable")


class _RegistryEnvSyncModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _RegistryEnvSyncLimits(_RegistryEnvSyncModel):
    video_tracker_window_size_frames: StrictInt
    video_tracker_sam3_window_size_frames: StrictInt

    @model_validator(mode="after")
    def positive_values(self) -> _RegistryEnvSyncLimits:
        if (
            self.video_tracker_window_size_frames <= 0
            or self.video_tracker_sam3_window_size_frames <= 0
        ):
            raise ValueError("Capability limits must be positive integers")
        return self


class RegistryEnvSyncRequest(_RegistryEnvSyncModel):
    schema_: Literal["aap-inference-registry-env-sync/v1"] = Field(alias="schema")
    request_id: uuid.UUID
    urls: list[Annotated[StrictStr, Field(min_length=1)]] = Field(repr=False)
    timeout_seconds: StrictInt | StrictFloat
    capability_limits: _RegistryEnvSyncLimits

    @model_validator(mode="after")
    def finite_timeout(self) -> RegistryEnvSyncRequest:
        timeout = self.timeout_seconds
        if type(timeout) not in (int, float):
            raise ValueError("Timeout must be a finite number")
        try:
            finite = math.isfinite(float(timeout))
        except (OverflowError, TypeError, ValueError):
            finite = False
        if not finite:
            raise ValueError("Timeout must be a finite number")
        return self


class RegistryEnvSyncResult(_RegistryEnvSyncModel):
    schema_: Literal["aap-inference-registry-env-sync-result/v1"] = Field(
        alias="schema"
    )
    request_id: uuid.UUID
    command_sha256: StrictStr
    state: Literal["caller_pending", "committed", "unknown"]
    observed_urls: StrictInt | None

    @model_validator(mode="after")
    def valid_result(self) -> RegistryEnvSyncResult:
        digest = self.command_sha256
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("Invalid command fingerprint")
        count = self.observed_urls
        if self.state == "unknown":
            if count is not None:
                raise ValueError("Unknown sync result cannot contain a count")
        elif type(count) is not int or count < 0:
            raise ValueError("Known sync results require a nonnegative count")
        return self


class RegistryEnvSyncStatusRequest(_RegistryEnvSyncModel):
    schema_: Literal["aap-inference-registry-env-sync-status/v1"] = Field(
        alias="schema"
    )
    request_id: uuid.UUID
    command_sha256: StrictStr

    @model_validator(mode="after")
    def valid_fingerprint(self) -> RegistryEnvSyncStatusRequest:
        if len(self.command_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.command_sha256
        ):
            raise ValueError("Invalid command fingerprint")
        return self


_REQUEST_ADAPTER = TypeAdapter(RegistryEnvSyncRequest)
_RESULT_ADAPTER = TypeAdapter(RegistryEnvSyncResult)
_STATUS_REQUEST_ADAPTER = TypeAdapter(RegistryEnvSyncStatusRequest)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


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


def _input_json(
    value: object,
    *,
    max_bytes: int,
    canonical_uuid_fields: tuple[str, ...] = (),
) -> tuple[bytes, dict[str, object]]:
    if isinstance(value, bytes):
        encoded = value
    elif type(value) is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise RegistryEnvSyncUnavailable() from None
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
            raise RegistryEnvSyncUnavailable() from None
    if len(encoded) > max_bytes:
        raise RegistryEnvSyncUnavailable()
    try:
        parsed = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except _JSON_ERRORS:
        raise RegistryEnvSyncUnavailable() from None
    if type(parsed) is not dict:
        raise RegistryEnvSyncUnavailable()
    for field in canonical_uuid_fields:
        raw = parsed.get(field)
        if type(raw) is not str:
            raise RegistryEnvSyncUnavailable()
        try:
            canonical = str(uuid.UUID(raw))
        except (ValueError, AttributeError):
            raise RegistryEnvSyncUnavailable() from None
        if raw != canonical:
            raise RegistryEnvSyncUnavailable()
    return encoded, parsed


def _decode_request(value: object) -> RegistryEnvSyncRequest:
    encoded, _ = _input_json(
        value,
        max_bytes=REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES,
        canonical_uuid_fields=("request_id",),
    )
    try:
        return _REQUEST_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RegistryEnvSyncUnavailable() from None


def decode_registry_env_sync_request(data: object) -> RegistryEnvSyncRequest:
    """Decode and revalidate a request model, mapping, or strict JSON payload."""
    return _decode_request(data)


def make_registry_env_sync_request(
    urls: list[str],
    *,
    timeout_seconds: int | float,
    capability_limits: CapabilityLimits | dict,
    request_id: uuid.UUID | None = None,
) -> RegistryEnvSyncRequest:
    """Build the exact closed command accepted by the Registry ingress owner."""
    if type(urls) is not list or (
        request_id is not None and type(request_id) is not uuid.UUID
    ):
        raise RegistryEnvSyncUnavailable()
    if type(capability_limits) is CapabilityLimits:
        limits: object = {
            "video_tracker_window_size_frames": capability_limits.video_tracker_window_size_frames,
            "video_tracker_sam3_window_size_frames": capability_limits.video_tracker_sam3_window_size_frames,
        }
    elif type(capability_limits) is dict:
        limits = capability_limits
    else:
        raise RegistryEnvSyncUnavailable()
    try:
        request = _REQUEST_ADAPTER.validate_python(
            {
                "schema": REGISTRY_ENV_SYNC_PROTOCOL,
                "request_id": request_id or uuid.uuid4(),
                "urls": urls,
                "timeout_seconds": timeout_seconds,
                "capability_limits": limits,
            }
        )
    except _JSON_ERRORS:
        raise RegistryEnvSyncUnavailable() from None
    registry_env_sync_request_bytes(request)
    return request


def registry_env_sync_request_bytes(request: object) -> bytes:
    """Encode a validated environment sync request as bounded UTF-8 JSON."""
    typed = _decode_request(request)
    encoded = json.dumps(
        typed.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES:
        raise RegistryEnvSyncUnavailable()
    return encoded


def registry_env_sync_command_sha256(
    request: RegistryEnvSyncRequest | object,
) -> str:
    """Fingerprint canonical UTF-8 command JSON with request_id omitted."""
    typed = _decode_request(request)
    command = typed.model_dump(mode="json", by_alias=True)
    del command["request_id"]
    canonical = json.dumps(
        command,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _decode_result(value: object) -> RegistryEnvSyncResult:
    encoded, _ = _input_json(
        value,
        max_bytes=REGISTRY_ENV_SYNC_REPLY_MAX_BYTES,
        canonical_uuid_fields=("request_id",),
    )
    try:
        return _RESULT_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RegistryEnvSyncUnavailable() from None


def registry_env_sync_result_bytes(result: object) -> bytes:
    """Encode a validated result/status payload within the shared response cap."""
    typed = _decode_result(result)
    encoded = json.dumps(
        typed.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > REGISTRY_ENV_SYNC_REPLY_MAX_BYTES:
        raise RegistryEnvSyncUnavailable()
    return encoded


def decode_registry_env_sync_result(
    request: RegistryEnvSyncRequest | object,
    data: object,
) -> RegistryEnvSyncResult:
    """Decode and bind a command result to its command ID and fingerprint."""
    typed_request = _decode_request(request)
    result = _decode_result(data)
    if (
        result.request_id != typed_request.request_id
        or result.command_sha256 != registry_env_sync_command_sha256(typed_request)
    ):
        raise RegistryEnvSyncUnavailable()
    return result


def registry_env_sync_status_request(
    request: RegistryEnvSyncRequest | object,
) -> RegistryEnvSyncStatusRequest:
    """Build a status query bound to the original command identity and digest."""
    typed = _decode_request(request)
    return RegistryEnvSyncStatusRequest(
        schema=REGISTRY_ENV_SYNC_STATUS_PROTOCOL,
        request_id=typed.request_id,
        command_sha256=registry_env_sync_command_sha256(typed),
    )


def _decode_status_request(value: object) -> RegistryEnvSyncStatusRequest:
    encoded, _ = _input_json(
        value,
        max_bytes=REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES,
        canonical_uuid_fields=("request_id",),
    )
    try:
        return _STATUS_REQUEST_ADAPTER.validate_json(encoded)
    except _JSON_ERRORS:
        raise RegistryEnvSyncUnavailable() from None


def decode_registry_env_sync_status_request(
    data: object,
) -> RegistryEnvSyncStatusRequest:
    """Decode and revalidate a strict status request."""
    return _decode_status_request(data)


def registry_env_sync_status_request_bytes(request: object) -> bytes:
    """Encode a strict status request as bounded UTF-8 JSON."""
    typed = _decode_status_request(request)
    encoded = json.dumps(
        typed.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES:
        raise RegistryEnvSyncUnavailable()
    return encoded


def decode_registry_env_sync_status_result(
    request: RegistryEnvSyncStatusRequest | object,
    data: object,
) -> RegistryEnvSyncResult:
    """Decode and bind a status result to its requested command fingerprint."""
    typed_request = _decode_status_request(request)
    result = _decode_result(data)
    if (
        result.request_id != typed_request.request_id
        or result.command_sha256 != typed_request.command_sha256
    ):
        raise RegistryEnvSyncUnavailable()
    return result


def registry_env_sync_policy_ready(value: str | bytes | dict) -> bool:
    """Check the exact Native Registry ingress policy, returning false on errors."""
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
    except _JSON_ERRORS:
        return False
    if type(parsed) is not dict or set(parsed) != _POLICY_KEYS:
        return False
    functions = parsed["functions"]
    return bool(
        type(parsed["schema"]) is str
        and parsed["schema"] == REGISTRY_ENV_SYNC_NATIVE_POLICY_SCHEMA
        and type(parsed["data_protocol"]) is str
        and parsed["data_protocol"] == REGISTRY_INGRESS_DATA_PROTOCOL
        and type(parsed["transaction"]) is str
        and parsed["transaction"] == "caller_owned"
        and type(functions) is list
        and all(type(name) is str for name in functions)
        and functions == list(REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS)
        and type(parsed["ready"]) is bool
        and parsed["ready"] is True
    )


__all__ = [
    "REGISTRY_INGRESS_DATA_PROTOCOL",
    "REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS",
    "REGISTRY_ENV_SYNC_NATIVE_POLICY_SCHEMA",
    "REGISTRY_ENV_SYNC_PROTOCOL",
    "REGISTRY_ENV_SYNC_REPLY_MAX_BYTES",
    "REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES",
    "REGISTRY_ENV_SYNC_RESULT_PROTOCOL",
    "REGISTRY_ENV_SYNC_STATUS_PROTOCOL",
    "RegistryEnvSyncRequest",
    "RegistryEnvSyncResult",
    "RegistryEnvSyncStatusRequest",
    "RegistryEnvSyncUnavailable",
    "decode_registry_env_sync_request",
    "decode_registry_env_sync_result",
    "decode_registry_env_sync_status_request",
    "decode_registry_env_sync_status_result",
    "make_registry_env_sync_request",
    "registry_env_sync_command_sha256",
    "registry_env_sync_policy_ready",
    "registry_env_sync_request_bytes",
    "registry_env_sync_result_bytes",
    "registry_env_sync_status_request",
    "registry_env_sync_status_request_bytes",
]
