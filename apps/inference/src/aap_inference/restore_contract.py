"""Strict wire values for the offline coordinated video restore operator."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

RESTORE_PROTOCOL = "aap-video-restore/v1"
RESTORE_DATA_PROTOCOL = "aap-video-restore-data/v1"
RESTORE_REQUEST_MAX_BYTES = 65_536
RESTORE_REPLY_MAX_BYTES = 65_536
RESTORE_NATIVE_SCHEMA = "aap_inference_routing_v1"
RESTORE_NATIVE_ROLE = "video_restore_operator_client"
RESTORE_NATIVE_FUNCTIONS = ("aap_inference_routing_v1.video_restore_step(text,text)",)
RESTORE_ACTIONS = frozenset(
    {"policy", "inspect", "prepare", "status", "observe", "record_bound", "finish"}
)
RESTORE_REASONS = frozenset(
    {
        "invalid_command",
        "invalid_request",
        "unsupported_isolation",
        "execution_busy",
        "restore_conflict",
        "restore_busy",
        "restore_missing",
        "source_mismatch",
        "realm_mismatch",
        "authority_unavailable",
        "route_snapshot_invalid",
        "registry_unavailable",
        "backend_reply_invalid",
        "transport_changed",
        "retirement_incomplete",
        "storage_unavailable",
    }
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_COMMAND_FIELDS = frozenset(
    {
        "schema",
        "restore_id",
        "backup_sha256",
        "fence_sha256",
        "source_jobs_realm",
        "target_jobs_realm",
        "source_routing_realm",
        "target_routing_realm",
        "source_jobs_secret_sha256",
        "source_routing_secret_sha256",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "restore_id",
        "phase",
        "command_sha256",
        "backup_sha256",
        "fence_sha256",
        "source_jobs_realm",
        "target_jobs_realm",
        "source_routing_realm",
        "target_routing_realm",
        "started_at",
        "completed_at",
        "counts",
    }
)
_COUNT_FIELDS = frozenset(
    {
        "executions_invalidated",
        "route_intents_rewrapped",
        "registries_rebound",
        "scopes_retired",
        "attempts_reconciled",
    }
)
_CONTEXT_FIELDS = frozenset(
    {
        "control_id",
        "registry_id",
        "phase",
        "new_epoch",
        "challenge",
        "retired_epoch",
        "backend_incarnation",
        "endpoint",
        "signing_key",
    }
)
_ENDPOINT_FIELDS = frozenset({"url", "auth_method", "auth_token"})
_IDENTITY_FIELDS = frozenset(
    {
        "source_jobs_realm",
        "target_jobs_realm",
        "source_routing_realm",
        "target_routing_realm",
        "source_jobs_secret_sha256",
        "source_routing_secret_sha256",
    }
)
_POLICY_FIELDS = frozenset({"schema", "status", "ready", "functions"})
_NATIVE_RESULT_FIELDS = frozenset({"schema", "status", "restore", "contexts"})


class RestoreUnavailable(Exception):
    """The restore owner or its response could not be safely verified."""

    def __init__(self) -> None:
        super().__init__("Video restore owner unavailable")


class RestoreRejected(Exception):
    """The Native owner rejected a command with a closed reason code."""

    def __init__(self, reason: str) -> None:
        if type(reason) is not str or reason not in RESTORE_REASONS:
            raise RestoreUnavailable()
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class RestoreCommand:
    """The complete operator command; secret fingerprints are not secrets."""

    restore_id: str
    backup_sha256: str
    fence_sha256: str
    source_jobs_realm: str
    target_jobs_realm: str
    source_routing_realm: str
    target_routing_realm: str
    source_jobs_secret_sha256: str
    source_routing_secret_sha256: str
    schema: str = RESTORE_PROTOCOL

    def __post_init__(self) -> None:
        _validate_command_values(self.as_dict())

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "restore_id": self.restore_id,
            "backup_sha256": self.backup_sha256,
            "fence_sha256": self.fence_sha256,
            "source_jobs_realm": self.source_jobs_realm,
            "target_jobs_realm": self.target_jobs_realm,
            "source_routing_realm": self.source_routing_realm,
            "target_routing_realm": self.target_routing_realm,
            "source_jobs_secret_sha256": self.source_jobs_secret_sha256,
            "source_routing_secret_sha256": self.source_routing_secret_sha256,
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Secret-free public result returned by the coordinator."""

    state: Literal["pending", "completed", "rejected", "unknown"]
    record: dict[str, Any] | None = None
    reason: str | None = None


def _canonical_uuid(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str:
        raise RestoreUnavailable()
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise RestoreUnavailable() from None
    if str(parsed) != value:
        raise RestoreUnavailable()
    return value


def _realm_name(value: str) -> str:
    if not value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise RestoreUnavailable()
    return value


def _validate_oid(value: str) -> None:
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise RestoreUnavailable()
    parsed = int(value)
    if str(parsed) != value or parsed <= 0 or parsed > (1 << 32) - 1:
        raise RestoreUnavailable()


def _validate_jobs_realm(value: object) -> str:
    if type(value) is not str:
        raise RestoreUnavailable()
    try:
        oid, rest = value.split(":", 1)
        name, system_identifier = rest.rsplit(":", 1)
    except ValueError:
        raise RestoreUnavailable()
    _validate_oid(oid)
    _realm_name(name)
    if (
        not system_identifier.isascii()
        or not system_identifier.isdigit()
        or len(system_identifier) > 20
        or str(int(system_identifier)) != system_identifier
        or int(system_identifier) <= 0
        or int(system_identifier) > (1 << 64) - 1
    ):
        raise RestoreUnavailable()
    return value


def _validate_routing_realm(value: object) -> str:
    if type(value) is not str:
        raise RestoreUnavailable()
    parts = value.split(":", 1)
    if len(parts) != 2:
        raise RestoreUnavailable()
    _validate_oid(parts[0])
    _realm_name(parts[1])
    return value


def _validate_command_values(value: Mapping[str, object]) -> dict[str, str]:
    if set(value) != _COMMAND_FIELDS:
        raise RestoreUnavailable()
    for field in _COMMAND_FIELDS:
        if type(value[field]) is not str:
            raise RestoreUnavailable()
    if value["schema"] != RESTORE_PROTOCOL:
        raise RestoreUnavailable()
    _canonical_uuid(value["restore_id"])
    for field in (
        "backup_sha256",
        "fence_sha256",
        "source_jobs_secret_sha256",
        "source_routing_secret_sha256",
    ):
        if _SHA256_RE.fullmatch(value[field]) is None:
            raise RestoreUnavailable()
    for field in ("source_jobs_realm", "target_jobs_realm"):
        _validate_jobs_realm(value[field])
    for field in ("source_routing_realm", "target_routing_realm"):
        _validate_routing_realm(value[field])
    return {key: value[key] for key in _COMMAND_FIELDS}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _decode_object(value: object, *, max_bytes: int) -> dict[str, Any]:
    try:
        if type(value) is bytes:
            raw = value
        elif type(value) is str:
            raw = value.encode("utf-8", errors="strict")
        elif isinstance(value, Mapping):
            raw = json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
        else:
            raise RestoreUnavailable()
        if len(raw) > max_bytes:
            raise RestoreUnavailable()
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except RestoreUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise RestoreUnavailable() from None
    if type(parsed) is not dict:
        raise RestoreUnavailable()
    return parsed


def decode_restore_command(value: object) -> RestoreCommand:
    if isinstance(value, RestoreCommand):
        _validate_command_values(value.as_dict())
        return value
    raw = _decode_object(value, max_bytes=RESTORE_REQUEST_MAX_BYTES)
    if set(raw) != _COMMAND_FIELDS:
        raise RestoreUnavailable()
    try:
        return RestoreCommand(
            schema=raw["schema"],
            restore_id=raw["restore_id"],
            backup_sha256=raw["backup_sha256"],
            fence_sha256=raw["fence_sha256"],
            source_jobs_realm=raw["source_jobs_realm"],
            target_jobs_realm=raw["target_jobs_realm"],
            source_routing_realm=raw["source_routing_realm"],
            target_routing_realm=raw["target_routing_realm"],
            source_jobs_secret_sha256=raw["source_jobs_secret_sha256"],
            source_routing_secret_sha256=raw["source_routing_secret_sha256"],
        )
    except RestoreUnavailable:
        raise
    except (TypeError, ValueError):
        raise RestoreUnavailable() from None


def canonical_command_json(command: RestoreCommand | Mapping[str, object]) -> str:
    parsed = decode_restore_command(command)
    try:
        return json.dumps(
            parsed.as_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise RestoreUnavailable() from None


def command_sha256(command: RestoreCommand | Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_command_json(command).encode("utf-8")).hexdigest()


def _validate_timestamp(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        raise RestoreUnavailable()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise RestoreUnavailable() from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise RestoreUnavailable()
    return value


def decode_restore_record(
    value: object, *, command: RestoreCommand | None = None
) -> dict[str, Any]:
    record = _decode_object(value, max_bytes=RESTORE_REPLY_MAX_BYTES)
    if set(record) != _RECORD_FIELDS:
        raise RestoreUnavailable()
    if type(record["phase"]) is not str or record["phase"] not in {
        "prepared",
        "completed",
    }:
        raise RestoreUnavailable()
    _canonical_uuid(record["restore_id"])
    for field in (
        "command_sha256",
        "backup_sha256",
        "fence_sha256",
    ):
        if (
            type(record[field]) is not str
            or _SHA256_RE.fullmatch(record[field]) is None
        ):
            raise RestoreUnavailable()
    _validate_jobs_realm(record["source_jobs_realm"])
    _validate_jobs_realm(record["target_jobs_realm"])
    _validate_routing_realm(record["source_routing_realm"])
    _validate_routing_realm(record["target_routing_realm"])
    _validate_timestamp(record["started_at"])
    _validate_timestamp(record["completed_at"], nullable=True)
    counts = record["counts"]
    if record["phase"] == "prepared":
        if record["completed_at"] is not None or counts is not None:
            raise RestoreUnavailable()
    else:
        if (
            record["completed_at"] is None
            or not isinstance(counts, Mapping)
            or set(counts) != _COUNT_FIELDS
        ):
            raise RestoreUnavailable()
        for count in counts.values():
            if type(count) is not int or not 0 <= count <= (1 << 63) - 1:
                raise RestoreUnavailable()
        record["counts"] = dict(counts)
    if command is not None:
        expected = command.as_dict()
        if any(
            record[record_field] != expected[command_field]
            for record_field, command_field in (
                ("restore_id", "restore_id"),
                ("backup_sha256", "backup_sha256"),
                ("fence_sha256", "fence_sha256"),
                ("source_jobs_realm", "source_jobs_realm"),
                ("target_jobs_realm", "target_jobs_realm"),
                ("source_routing_realm", "source_routing_realm"),
                ("target_routing_realm", "target_routing_realm"),
            )
        ) or record["command_sha256"] != command_sha256(command):
            raise RestoreUnavailable()
    return record


def decode_restore_context(value: object) -> dict[str, Any]:
    context = _decode_object(value, max_bytes=RESTORE_REPLY_MAX_BYTES)
    if set(context) != _CONTEXT_FIELDS:
        raise RestoreUnavailable()
    for field in ("control_id", "registry_id", "new_epoch", "backend_incarnation"):
        _canonical_uuid(context[field])
    _canonical_uuid(context["retired_epoch"], nullable=True)
    if type(context["phase"]) is not str or context["phase"] not in {
        "prepared",
        "bound",
    }:
        raise RestoreUnavailable()
    if (
        type(context["challenge"]) is not str
        or _SHA256_RE.fullmatch(context["challenge"]) is None
    ):
        raise RestoreUnavailable()
    key = context["signing_key"]
    if type(key) is not str or _BASE64URL_RE.fullmatch(key) is None:
        raise RestoreUnavailable()
    import base64

    try:
        raw_key = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    except (ValueError, base64.binascii.Error):
        raise RestoreUnavailable() from None
    if (
        len(raw_key) != 32
        or base64.urlsafe_b64encode(raw_key).rstrip(b"=").decode("ascii") != key
    ):
        raise RestoreUnavailable()
    endpoint = context["endpoint"]
    if not isinstance(endpoint, Mapping) or set(endpoint) != _ENDPOINT_FIELDS:
        raise RestoreUnavailable()
    url, auth_method, auth_token = (
        endpoint["url"],
        endpoint["auth_method"],
        endpoint["auth_token"],
    )
    if (
        type(url) is not str
        or not url
        or any(character.isspace() or ord(character) < 0x20 for character in url)
        or type(auth_method) is not str
        or auth_method != "token"
        or type(auth_token) is not str
        or not auth_token
        or not auth_token.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in auth_token)
    ):
        raise RestoreUnavailable()
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise RestoreUnavailable() from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RestoreUnavailable()
    context["endpoint"] = dict(endpoint)
    return context


def decode_native_envelope(value: object, *, action: str) -> dict[str, Any]:
    if action not in RESTORE_ACTIONS:
        raise RestoreUnavailable()
    envelope = _decode_object(value, max_bytes=RESTORE_REPLY_MAX_BYTES)
    if (
        envelope.get("schema") != RESTORE_DATA_PROTOCOL
        or type(envelope.get("status")) is not str
        or envelope["status"] not in {"ok", "rejected"}
    ):
        raise RestoreUnavailable()
    if envelope["status"] == "rejected":
        if set(envelope) != {"schema", "status", "reason"}:
            raise RestoreUnavailable()
        reason = envelope["reason"]
        if type(reason) is not str or reason not in RESTORE_REASONS:
            raise RestoreUnavailable()
        return envelope
    if action == "policy":
        if set(envelope) != _POLICY_FIELDS or envelope["ready"] is not True:
            raise RestoreUnavailable()
        functions = envelope["functions"]
        if type(functions) is not list or functions != list(RESTORE_NATIVE_FUNCTIONS):
            raise RestoreUnavailable()
        return envelope
    if action == "inspect":
        if set(envelope) != {"schema", "status", "identity"}:
            raise RestoreUnavailable()
        identity = envelope["identity"]
        if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_FIELDS:
            raise RestoreUnavailable()
        for field in ("source_jobs_realm", "target_jobs_realm"):
            _validate_jobs_realm(identity[field])
        for field in ("source_routing_realm", "target_routing_realm"):
            _validate_routing_realm(identity[field])
        for field in ("source_jobs_secret_sha256", "source_routing_secret_sha256"):
            if (
                type(identity[field]) is not str
                or _SHA256_RE.fullmatch(identity[field]) is None
            ):
                raise RestoreUnavailable()
        envelope["identity"] = dict(identity)
        return envelope
    if set(envelope) != _NATIVE_RESULT_FIELDS:
        raise RestoreUnavailable()
    record = decode_restore_record(envelope["restore"])
    contexts = envelope["contexts"]
    if type(contexts) is not list:
        raise RestoreUnavailable()
    decoded_contexts = [decode_restore_context(context) for context in contexts]
    if record["phase"] == "completed" and decoded_contexts:
        raise RestoreUnavailable()
    registry_ids = [context["registry_id"] for context in decoded_contexts]
    if len(set(registry_ids)) != len(registry_ids):
        raise RestoreUnavailable()
    envelope["restore"] = record
    envelope["contexts"] = decoded_contexts
    return envelope


def video_restore_schema() -> dict[str, Any]:
    """Return the checked-in JSON Schema for public and private restore values."""
    string = {"type": "string"}
    uuid = {
        "type": "string",
        "format": "uuid",
        "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    }
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    jobs_realm = {
        "type": "string",
        "pattern": "^[1-9][0-9]*:[^\\u0000-\\u001f\\u007f]+:[1-9][0-9]*$",
    }
    routing_realm = {
        "type": "string",
        "pattern": "^[1-9][0-9]*:[^\\u0000-\\u001f\\u007f]+$",
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RESTORE_PROTOCOL,
        "$defs": {
            "command": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_COMMAND_FIELDS),
                "properties": {
                    "schema": {"const": RESTORE_PROTOCOL},
                    "restore_id": uuid,
                    "backup_sha256": sha,
                    "fence_sha256": sha,
                    "source_jobs_realm": jobs_realm,
                    "target_jobs_realm": jobs_realm,
                    "source_routing_realm": routing_realm,
                    "target_routing_realm": routing_realm,
                    "source_jobs_secret_sha256": sha,
                    "source_routing_secret_sha256": sha,
                },
            },
            "counts": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_COUNT_FIELDS),
                "properties": {
                    field: {"type": "integer", "minimum": 0, "maximum": (1 << 63) - 1}
                    for field in sorted(_COUNT_FIELDS)
                },
            },
            "record": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_RECORD_FIELDS),
                "properties": {
                    "restore_id": uuid,
                    "phase": {"enum": ["prepared", "completed"]},
                    "command_sha256": sha,
                    "backup_sha256": sha,
                    "fence_sha256": sha,
                    "source_jobs_realm": jobs_realm,
                    "target_jobs_realm": jobs_realm,
                    "source_routing_realm": routing_realm,
                    "target_routing_realm": routing_realm,
                    "started_at": {
                        "type": "string",
                        "format": "date-time",
                        "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$",
                    },
                    "completed_at": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "string",
                                "format": "date-time",
                                "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$",
                            },
                        ]
                    },
                    "counts": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/counts"}]},
                },
            },
            "private_context": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_CONTEXT_FIELDS),
                "properties": {
                    "control_id": uuid,
                    "registry_id": uuid,
                    "phase": {"enum": ["prepared", "bound"]},
                    "new_epoch": uuid,
                    "challenge": sha,
                    "retired_epoch": {"anyOf": [{"type": "null"}, uuid]},
                    "backend_incarnation": uuid,
                    "endpoint": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": sorted(_ENDPOINT_FIELDS),
                        "properties": {
                            "url": {"type": "string", "format": "uri"},
                            "auth_method": {"const": "token"},
                            "auth_token": string,
                        },
                    },
                    "signing_key": {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"},
                },
            },
            "backend_control_reply": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "schema",
                    "command_id",
                    "registry_id",
                    "authority_epoch",
                    "backend_incarnation",
                    "challenge",
                    "phase",
                    "active_requests",
                ],
                "properties": {
                    "schema": {"const": "aap-backend-route-control-reply/v1"},
                    "command_id": uuid,
                    "registry_id": uuid,
                    "authority_epoch": uuid,
                    "backend_incarnation": uuid,
                    "challenge": sha,
                    "phase": {"enum": ["draining", "drained", "bound"]},
                    "active_requests": {"type": "integer", "minimum": 0},
                },
            },
            "native_step": {
                "oneOf": [
                    {"$ref": "#/$defs/native_policy_step"},
                    {"$ref": "#/$defs/native_inspect_step"},
                    {"$ref": "#/$defs/native_prepare_step"},
                    {"$ref": "#/$defs/native_status_step"},
                    {"$ref": "#/$defs/native_observe_step"},
                    {"$ref": "#/$defs/native_record_bound_step"},
                    {"$ref": "#/$defs/native_finish_step"},
                ],
            },
            "native_policy_step": {
                "$ref": "#/$defs/native_step_shape",
                "properties": {
                    "action": {"const": "policy"},
                    "arguments": {"type": "object", "maxProperties": 0},
                },
            },
            "native_inspect_step": {
                "$ref": "#/$defs/native_step_shape",
                "properties": {
                    "action": {"const": "inspect"},
                    "arguments": {"type": "object", "maxProperties": 0},
                },
            },
            "native_prepare_step": {
                "$ref": "#/$defs/native_step_shape",
                "properties": {
                    "action": {"const": "prepare"},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["command"],
                        "properties": {"command": {"$ref": "#/$defs/command"}},
                    },
                },
            },
            "native_status_step": {
                "$ref": "#/$defs/native_step_shape",
                "properties": {
                    "action": {"const": "status"},
                    "arguments": {"$ref": "#/$defs/restore_id_arguments"},
                },
            },
            "native_observe_step": {
                "$ref": "#/$defs/native_step_shape",
                "properties": {
                    "action": {"const": "observe"},
                    "arguments": {"$ref": "#/$defs/reply_arguments"},
                },
            },
            "native_record_bound_step": {
                "$ref": "#/$defs/native_step_shape",
                "properties": {
                    "action": {"const": "record_bound"},
                    "arguments": {"$ref": "#/$defs/reply_arguments"},
                },
            },
            "native_finish_step": {
                "$ref": "#/$defs/native_step_shape",
                "properties": {
                    "action": {"const": "finish"},
                    "arguments": {"$ref": "#/$defs/restore_id_arguments"},
                },
            },
            "native_step_shape": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "arguments"],
                "properties": {
                    "action": {"enum": sorted(RESTORE_ACTIONS)},
                    "arguments": {"type": "object"},
                },
            },
            "restore_id_arguments": {
                "type": "object",
                "additionalProperties": False,
                "required": ["restore_id"],
                "properties": {"restore_id": uuid},
            },
            "reply_arguments": {
                "type": "object",
                "additionalProperties": False,
                "required": ["restore_id", "registry_id", "reply"],
                "properties": {
                    "restore_id": uuid,
                    "registry_id": uuid,
                    "reply": {"$ref": "#/$defs/backend_control_reply"},
                },
            },
            "native_success": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "status", "restore", "contexts"],
                "properties": {
                    "schema": {"const": RESTORE_DATA_PROTOCOL},
                    "status": {"const": "ok"},
                    "restore": {"$ref": "#/$defs/record"},
                    "contexts": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/private_context"},
                    },
                },
            },
            "inspect_success": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "status", "identity"],
                "properties": {
                    "schema": {"const": RESTORE_DATA_PROTOCOL},
                    "status": {"const": "ok"},
                    "identity": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": sorted(_IDENTITY_FIELDS),
                        "properties": {
                            "source_jobs_realm": jobs_realm,
                            "target_jobs_realm": jobs_realm,
                            "source_routing_realm": routing_realm,
                            "target_routing_realm": routing_realm,
                            "source_jobs_secret_sha256": sha,
                            "source_routing_secret_sha256": sha,
                        },
                    },
                },
            },
            "native_rejected": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "status", "reason"],
                "properties": {
                    "schema": {"const": RESTORE_DATA_PROTOCOL},
                    "status": {"const": "rejected"},
                    "reason": {"enum": sorted(RESTORE_REASONS)},
                },
            },
        },
        "oneOf": [
            {"$ref": "#/$defs/command"},
            {"$ref": "#/$defs/record"},
            {"$ref": "#/$defs/private_context"},
            {"$ref": "#/$defs/native_step"},
            {"$ref": "#/$defs/native_success"},
            {"$ref": "#/$defs/inspect_success"},
            {"$ref": "#/$defs/native_rejected"},
        ],
    }


__all__ = [
    "RESTORE_ACTIONS",
    "RESTORE_DATA_PROTOCOL",
    "RESTORE_NATIVE_FUNCTIONS",
    "RESTORE_NATIVE_ROLE",
    "RESTORE_NATIVE_SCHEMA",
    "RESTORE_PROTOCOL",
    "RESTORE_REASONS",
    "RestoreCommand",
    "RestoreRejected",
    "RestoreResult",
    "RestoreUnavailable",
    "canonical_command_json",
    "command_sha256",
    "decode_native_envelope",
    "decode_restore_command",
    "decode_restore_context",
    "decode_restore_record",
    "video_restore_schema",
]
