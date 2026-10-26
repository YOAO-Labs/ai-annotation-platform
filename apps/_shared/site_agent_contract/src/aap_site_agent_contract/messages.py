"""Canonical JSON and closed wire schemas; no clocks, credentials or I/O."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_BYTES = 64 * 1024 * 1024
MAX_COMMAND_LIFETIME_SECONDS = 120
MAX_TIMEOUT_SECONDS = 60

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ROOT_HANDLE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.:@-]{1,128}\Z")


class ProtocolError(ValueError):
    """A stable, payload-free failure code safe to return across the wire."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json_value(value: Any) -> None:
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ProtocolError("invalid_json")
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProtocolError("invalid_json")
            _json_value(key)
            _json_value(item)
        return
    raise ProtocolError("invalid_json")


def canonical_json(value: Any) -> bytes:
    """Encode native JSON values with sorted keys, ASCII escapes and no NaN.

    This is the protocol's Python JSON representation, not RFC 8785. It does not
    normalize Unicode or conflate distinct JSON numeric representations.
    """
    try:
        _json_value(value)
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid_json") from None


def digest(value: Any) -> str:
    """Hash every field of a value; callers validate messages before hashing."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ProtocolError("invalid_json")


def parse_json(data: bytes, max_bytes: int = MAX_MESSAGE_BYTES) -> Any:
    """Decode bounded UTF-8 JSON, rejecting duplicate keys at every depth."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ProtocolError("invalid_message_limit")
    if type(data) is not bytes:
        raise ProtocolError("invalid_json")
    if len(data) > max_bytes:
        raise ProtocolError("message_too_large")
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
        _json_value(value)
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid_json") from None


def _mapping(value: Any, code: str) -> dict:
    if type(value) is not dict:
        raise ProtocolError(code)
    return value


def _fields(value: dict, required: set[str], optional: set[str], code: str) -> None:
    if not required <= value.keys() or value.keys() - required - optional:
        raise ProtocolError(code)


def _copy(value: Any, code: str) -> dict:
    _mapping(value, code)
    return parse_json(canonical_json(value))


def _int(value: Any, code: str, minimum: int | None = None, maximum=None) -> int:
    if (
        type(value) is not int
        or (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
    ):
        raise ProtocolError(code)
    return value


def _match(value: Any, pattern: re.Pattern, code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ProtocolError(code)
    return value


def _uuid(value: Any, code: str) -> str:
    if type(value) is not str:
        raise ProtocolError(code)
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ProtocolError(code) from None


def _version(value: Any) -> int:
    if type(value) is not int or value != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol_version")
    return value


def _path(value: Any, *, nonempty=False, filename=False) -> str:
    if (
        type(value) is not str
        or len(value) > 1000
        or len(value.split("/")) > 32
        or (nonempty and not value)
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or (value and any(part in {"", ".", ".."} for part in value.split("/")))
        or (filename and "/" in value)
    ):
        raise ProtocolError("invalid_path")
    return value


def _pair(value: Any, code: str) -> list[int]:
    if type(value) is not list or len(value) != 2:
        raise ProtocolError(code)
    return [_int(value[0], code, 1), _int(value[1], code)]


def _anchor(value: Any) -> dict | None:
    if value is None:
        return None
    code = "invalid_anchor"
    _mapping(value, code)
    _fields(value, {"path_digest", "root", "base"}, set(), code)
    return {
        "path_digest": _match(value["path_digest"], _SHA256, code),
        "root": _pair(value["root"], code),
        "base": _pair(value["base"], code),
    }


def _observation(value: Any) -> dict | None:
    if value is None:
        return None
    code = "invalid_observation"
    _mapping(value, code)
    _fields(
        value,
        set(),
        {"identity", "size", "mode", "mtime_ns", "ctime_ns", "source_identity"},
        code,
    )
    result = dict(value)
    for field in ("identity", "source_identity"):
        if field in value:
            result[field] = _pair(value[field], code)
    for field in ("mtime_ns", "ctime_ns"):
        if field in value:
            result[field] = _int(value[field], code)
    if "mode" in value:
        result["mode"] = _int(value["mode"], code, 0)
    if "size" in value:
        result["size"] = _int(value["size"], code, 0, MAX_CONTENT_BYTES)
    return result


def _fence(value: Any) -> dict:
    code = "invalid_fence"
    _mapping(value, code)
    uuids = {"location_id", "source_id", "run_id", "job_id", "user_id", "asset_id"}
    revisions = {"location_revision", "maintenance_revision", "generation"}
    required = {"location_id", "location_revision", "maintenance_revision", "purpose"}
    _fields(value, required, uuids | revisions | {"task_id", "lease_token"}, code)
    result = dict(value)
    result["purpose"] = _match(value["purpose"], _LABEL, code)
    for field in uuids & value.keys():
        result[field] = _uuid(value[field], code)
    for field in revisions & value.keys():
        result[field] = _int(value[field], code, 1)
    for field in {"task_id", "lease_token"} & value.keys():
        result[field] = _match(value[field], _TOKEN, code)
    return result


def _arguments(value: Any, operation: str) -> dict:
    code = "invalid_arguments"
    _mapping(value, code)
    common = {"relative_path", "expected_anchor"}
    optional = {
        "probe": {"sample_file"},
        "browse": {"after", "snapshot"},
        "scan": {"expected_identity"},
        "read": {
            "source_path",
            "expected",
            "expected_source_identity",
            "expected_sha256",
            "expected_size",
        },
    }[operation]
    required = {"path", "limit"} if operation == "browse" else set()
    if operation in {"scan", "read"}:
        required.add("path")
    _fields(value, required, common | optional, code)
    result = {
        "relative_path": _path(value.get("relative_path", "")),
        "expected_anchor": _anchor(value.get("expected_anchor")),
    }
    if operation != "probe":
        result["path"] = _path(value["path"], nonempty=operation == "read")
    if operation == "probe":
        sample = value.get("sample_file")
        result["sample_file"] = (
            _path(sample, nonempty=True) if sample is not None else None
        )
    elif operation == "browse":
        result["limit"] = _int(value["limit"], code, 1, 200)
        after, snapshot = value.get("after"), value.get("snapshot")
        result["after"] = (
            _path(after, nonempty=True, filename=True) if after is not None else None
        )
        result["snapshot"] = (
            _match(snapshot, _SHA256, code) if snapshot is not None else None
        )
    elif operation == "scan":
        identity = value.get("expected_identity")
        result["expected_identity"] = (
            _pair(identity, code) if identity is not None else None
        )
    else:
        if "source_path" in value:
            result["source_path"] = _path(value["source_path"])
        result["expected"] = _observation(value.get("expected"))
        identity, sha, size = (
            value.get("expected_source_identity"),
            value.get("expected_sha256"),
            value.get("expected_size"),
        )
        result["expected_source_identity"] = (
            _pair(identity, code) if identity is not None else None
        )
        result["expected_sha256"] = (
            _match(sha, _SHA256, code) if sha is not None else None
        )
        result["expected_size"] = (
            _int(size, code, 0, MAX_CONTENT_BYTES) if size is not None else None
        )
    return result


def validate_command(value: dict) -> dict:
    """Validate and detach a command, adding only documented argument defaults."""
    code = "invalid_command"
    value = _copy(value, code)
    _fields(
        value,
        {
            "protocol_version",
            "command_id",
            "agent_id",
            "credential_revision",
            "root_handle",
            "root_revision",
            "operation",
            "arguments",
            "issued_at",
            "expires_at",
            "max_bytes",
            "timeout_seconds",
            "fence",
        },
        {"bandwidth_bytes_per_second"},
        code,
    )
    result = dict(value)
    result["protocol_version"] = _version(value["protocol_version"])
    for field in ("command_id", "agent_id"):
        result[field] = _uuid(value[field], code)
    for field in ("credential_revision", "root_revision"):
        result[field] = _int(value[field], code, 1)
    result["root_handle"] = _match(value["root_handle"], _ROOT_HANDLE, code)
    operation = value["operation"]
    if type(operation) is not str or operation not in {
        "probe",
        "browse",
        "scan",
        "read",
    }:
        raise ProtocolError(code)
    issued = _int(value["issued_at"], code, 0)
    expires = _int(value["expires_at"], code, 0)
    if not 0 < expires - issued <= MAX_COMMAND_LIFETIME_SECONDS:
        raise ProtocolError(code)
    result["max_bytes"] = _int(value["max_bytes"], code, 1, MAX_CONTENT_BYTES)
    if "bandwidth_bytes_per_second" in value:
        result["bandwidth_bytes_per_second"] = _int(
            value["bandwidth_bytes_per_second"], code, 1024, 1024**3
        )
    result["timeout_seconds"] = _int(
        value["timeout_seconds"], code, 1, MAX_TIMEOUT_SECONDS
    )
    result["fence"] = _fence(value["fence"])
    result["arguments"] = _arguments(value["arguments"], operation)
    if len(canonical_json(result)) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large")
    return result


def validate_result(value: dict) -> dict:
    """Validate a bounded opaque operation result and physical-close evidence."""
    code = "invalid_result"
    value = _copy(value, code)
    _fields(
        value,
        {
            "protocol_version",
            "command_id",
            "command_digest",
            "status",
            "result",
            "error_code",
            "closed",
        },
        set(),
        code,
    )
    value["protocol_version"] = _version(value["protocol_version"])
    value["command_id"] = _uuid(value["command_id"], code)
    _match(value["command_digest"], _SHA256, code)
    _mapping(value["result"], code)
    if type(value["closed"]) is not bool:
        raise ProtocolError(code)
    if value["status"] == "completed":
        if value["error_code"] is not None or not value["closed"]:
            raise ProtocolError(code)
    elif value["status"] == "failed":
        _match(value["error_code"], _LABEL, code)
        if value["result"]:
            raise ProtocolError(code)
    else:
        raise ProtocolError(code)
    return value


def validate_ack(value: dict) -> dict:
    """Validate receipt/application acknowledgements without advancing state."""
    code = "invalid_ack"
    value = _copy(value, code)
    _fields(
        value,
        {
            "protocol_version",
            "command_id",
            "command_digest",
            "result_digest",
            "state",
            "code",
        },
        set(),
        code,
    )
    value["protocol_version"] = _version(value["protocol_version"])
    value["command_id"] = _uuid(value["command_id"], code)
    for field in ("command_digest", "result_digest"):
        _match(value[field], _SHA256, code)
    if value["state"] in ("received", "applied"):
        if value["code"] is not None:
            raise ProtocolError(code)
    elif value["state"] == "rejected":
        _match(value["code"], _LABEL, code)
    else:
        raise ProtocolError(code)
    return value
