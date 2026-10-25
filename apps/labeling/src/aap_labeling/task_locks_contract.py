"""Closed, language-neutral requests for Labeling task edit locks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

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
)

from aap_preannotation_results.contract import CanonicalUUID, UtcDateTime

TASK_LOCKS_PROTOCOL = "aap-labeling-task-locks/v1"
TASK_LOCKS_DATA_SCHEMA = "aap-labeling-task-locks-data/v1"
TASK_LOCKS_FUNCTION = "aap_labeling_v1.task_edit_lock_command(jsonb)"
TASK_LOCK_SCOPE_PREFIX = "aap:task-edit-lock:"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-task-locks/v1"] = Field(alias="schema")
    action: StrictStr


class _ScopeCommand(_Command):
    task_id: CanonicalUUID


class TaskLockScopeCommand(_ScopeCommand):
    action: Literal["lock_scope"]


class TaskLockTryFinalScopeCommand(_ScopeCommand):
    action: Literal["try_final_scope"]


class TaskLockAcquireCommand(_ScopeCommand):
    action: Literal["acquire"]
    user_id: CanonicalUUID
    ttl: StrictInt | None
    force_takeover: StrictBool


class TaskLockReleaseCommand(_ScopeCommand):
    action: Literal["release"]
    user_id: CanonicalUUID


class TaskLockHeartbeatCommand(_ScopeCommand):
    action: Literal["heartbeat"]
    user_id: CanonicalUUID
    ttl: StrictInt | None


class TaskLockAssertWriteCommand(_ScopeCommand):
    action: Literal["assert_write_allowed"]
    user_id: CanonicalUUID


class TaskLockReadCommand(_ScopeCommand):
    action: Literal["is_locked", "active_lock", "active_locks"]


class TaskLockCleanupCommand(_Command):
    action: Literal["cleanup_expired"]
    task_id: CanonicalUUID | None


class TaskLockBatchFinalCommand(_Command):
    action: Literal["batch_final_write_available"]
    batch_id: CanonicalUUID
    actor_id: CanonicalUUID
    allow_own: StrictBool
    allowed_statuses: tuple[StrictStr, ...]

    @field_validator("allowed_statuses", mode="before")
    @classmethod
    def json_array_to_status_tuple(cls, value):
        if type(value) not in (list, tuple) or any(
            type(item) is not str for item in value
        ):
            raise ValueError("allowed_statuses must be an array of strings")
        return tuple(value)


TaskLocksCommand: TypeAlias = Annotated[
    TaskLockScopeCommand
    | TaskLockTryFinalScopeCommand
    | TaskLockAcquireCommand
    | TaskLockReleaseCommand
    | TaskLockHeartbeatCommand
    | TaskLockAssertWriteCommand
    | TaskLockReadCommand
    | TaskLockCleanupCommand
    | TaskLockBatchFinalCommand,
    Field(discriminator="action"),
]

_COMMAND_ADAPTER = TypeAdapter(TaskLocksCommand)
_UUID_ADAPTER = TypeAdapter(CanonicalUUID)


class TaskLockRecord(_ClosedModel):
    id: CanonicalUUID
    task_id: CanonicalUUID
    user_id: CanonicalUUID
    expire_at: UtcDateTime
    unique_id: CanonicalUUID
    created_at: UtcDateTime


class TaskLocksInvalidRequest(ValueError):
    """A task-lock command does not match the closed wire contract."""


class TaskLocksUnavailable(RuntimeError):
    """The Labeling task-lock capability returned an invalid reply."""


def task_locks_command(action: str, **values: Any) -> TaskLocksCommand:
    try:
        return _COMMAND_ADAPTER.validate_python(
            {"schema": TASK_LOCKS_PROTOCOL, "action": action, **values}
        )
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise TaskLocksInvalidRequest() from None


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def _json_object(payload: object) -> dict[str, object]:
    if isinstance(payload, (bytes, bytearray, memoryview, str)):
        raw = (
            bytes(payload) if not isinstance(payload, str) else payload.encode("utf-8")
        )
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    else:
        value = _json_ready(payload)
    if type(value) is not dict:
        raise ValueError("Expected a JSON object")
    return value


def decode_task_locks_command(value: object) -> TaskLocksCommand:
    try:
        if isinstance(value, (bytes, bytearray, memoryview, str)):
            raw = bytes(value) if not isinstance(value, str) else value.encode("utf-8")
            payload = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            return _COMMAND_ADAPTER.validate_python(payload)
        return _COMMAND_ADAPTER.validate_python(_json_ready(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TaskLocksInvalidRequest() from None


def task_locks_command_payload(value: object) -> dict[str, object]:
    command = decode_task_locks_command(value)
    return command.model_dump(mode="json", by_alias=True)


def _decode_lock(value: object) -> TaskLockRecord | None:
    if value is None:
        return None
    try:
        return TaskLockRecord.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise TaskLocksUnavailable() from None


def _decode_ids(value: object) -> list[CanonicalUUID]:
    if type(value) is not list:
        raise TaskLocksUnavailable()
    try:
        return [_UUID_ADAPTER.validate_python(item) for item in value]
    except (ValidationError, ValueError, TypeError):
        raise TaskLocksUnavailable() from None


def decode_task_locks_reply(command: object, payload: object) -> dict[str, object]:
    request = decode_task_locks_command(command)
    try:
        reply = _json_object(payload)
        if set(reply) != {"schema", "action", "ok", "data"}:
            raise ValueError("Invalid reply fields")
        if (
            reply["schema"] != TASK_LOCKS_DATA_SCHEMA
            or reply["action"] != request.action
            or reply["ok"] is not True
            or type(reply["data"]) is not dict
        ):
            raise ValueError("Invalid reply envelope")
        data = reply["data"]
        expected_fields = {
            "lock_scope": {"locked"},
            "try_final_scope": {"available"},
            "acquire": {"considered_ids", "deleted_ids", "lock"},
            "release": {"released", "lock"},
            "heartbeat": {"renewed", "lock"},
            "assert_write_allowed": {"deleted_ids", "locks", "owner_id"},
            "is_locked": {"deleted_ids", "lock", "locked", "user_id"},
            "active_lock": {"deleted_ids", "lock"},
            "active_locks": {"deleted_ids", "locks"},
            "cleanup_expired": {"deleted_count", "deleted_ids"},
            "batch_final_write_available": {"available"},
        }[request.action]
        if set(data) != expected_fields:
            raise ValueError("Invalid data fields")

        if request.action == "lock_scope":
            if data["locked"] is not True:
                raise ValueError("Scope lock was not acquired")
            return {"locked": True}
        if request.action in {
            "try_final_scope",
            "batch_final_write_available",
        }:
            if type(data["available"]) is not bool:
                raise ValueError("Invalid lock availability")
            return {"available": data["available"]}
        if request.action == "acquire":
            return {
                "lock": _check_lock_identity(
                    _decode_lock(data["lock"]),
                    task_id=request.task_id,
                    user_id=request.user_id,
                ),
                "considered_ids": _decode_ids(data["considered_ids"]),
                "deleted_ids": _decode_ids(data["deleted_ids"]),
            }
        if request.action == "release":
            if type(data["released"]) is not bool:
                raise ValueError("Invalid release result")
            lock = _check_lock_identity(
                _decode_lock(data["lock"]),
                task_id=request.task_id,
                user_id=request.user_id,
            )
            if data["released"] != (lock is not None):
                raise ValueError("Release result and lock disagree")
            return {"released": data["released"], "lock": lock}
        if request.action == "heartbeat":
            if type(data["renewed"]) is not bool:
                raise ValueError("Invalid heartbeat result")
            lock = _check_lock_identity(
                _decode_lock(data["lock"]),
                task_id=request.task_id,
                user_id=request.user_id,
            )
            if data["renewed"] != (lock is not None):
                raise ValueError("Heartbeat result and lock disagree")
            return {"renewed": data["renewed"], "lock": lock}
        if request.action == "assert_write_allowed":
            owner_id = data["owner_id"]
            owner = (
                None if owner_id is None else _UUID_ADAPTER.validate_python(owner_id)
            )
            locks = _check_lock_list_identity(
                _decode_locks(data["locks"]), request.task_id
            )
            if owner is not None:
                if owner == request.user_id or not any(
                    lock.user_id == owner for lock in locks
                ):
                    raise TaskLocksUnavailable()
            elif any(lock.user_id != request.user_id for lock in locks):
                raise TaskLocksUnavailable()
            return {
                "owner_id": owner,
                "deleted_ids": _decode_ids(data["deleted_ids"]),
                "locks": locks,
            }
        if request.action == "is_locked":
            locked = data["locked"]
            user_id = data["user_id"]
            if type(locked) is not bool:
                raise ValueError("Invalid lock state")
            owner = None if user_id is None else _UUID_ADAPTER.validate_python(user_id)
            if locked != (owner is not None):
                raise ValueError("Lock state and owner disagree")
            lock = _check_lock_identity(
                _decode_lock(data["lock"]), task_id=request.task_id
            )
            if locked != (lock is not None) or (
                lock is not None and lock.user_id != owner
            ):
                raise ValueError("Lock state and row disagree")
            return {
                "locked": locked,
                "user_id": owner,
                "lock": lock,
                "deleted_ids": _decode_ids(data["deleted_ids"]),
            }
        if request.action == "active_lock":
            return {
                "lock": _check_lock_identity(
                    _decode_lock(data["lock"]), task_id=request.task_id
                ),
                "deleted_ids": _decode_ids(data["deleted_ids"]),
            }
        if request.action == "active_locks":
            return {
                "locks": _check_lock_list_identity(
                    _decode_locks(data["locks"]), request.task_id
                ),
                "deleted_ids": _decode_ids(data["deleted_ids"]),
            }
        if request.action == "cleanup_expired":
            deleted_count = data["deleted_count"]
            if type(deleted_count) is not int or deleted_count < 0:
                raise ValueError("Invalid deletion count")
            deleted_ids = _decode_ids(data["deleted_ids"])
            if deleted_count != len(deleted_ids):
                raise ValueError("Deletion count and deleted IDs disagree")
            return {"deleted_count": deleted_count, "deleted_ids": deleted_ids}
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TaskLocksUnavailable() from None
    raise TaskLocksUnavailable()


def _decode_locks(value: object) -> list[TaskLockRecord]:
    if type(value) is not list:
        raise TaskLocksUnavailable()
    try:
        locks = [TaskLockRecord.model_validate(item) for item in value]
    except (ValidationError, ValueError, TypeError):
        raise TaskLocksUnavailable() from None
    return locks


def _check_lock_identity(
    lock: TaskLockRecord | None,
    *,
    task_id: CanonicalUUID,
    user_id: CanonicalUUID | None = None,
) -> TaskLockRecord | None:
    if lock is not None and (
        lock.task_id != task_id or (user_id is not None and lock.user_id != user_id)
    ):
        raise TaskLocksUnavailable()
    return lock


def _check_lock_list_identity(
    locks: list[TaskLockRecord], task_id: CanonicalUUID
) -> list[TaskLockRecord]:
    if any(lock.task_id != task_id for lock in locks):
        raise TaskLocksUnavailable()
    return locks


def task_locks_schema() -> dict[str, Any]:
    schema = _COMMAND_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = TASK_LOCKS_PROTOCOL
    schema["title"] = "Labeling task edit-lock commands"
    schema["x-postgresql-functions"] = {
        TASK_LOCKS_FUNCTION: {
            "argument_types": ["jsonb"],
            "returns": "jsonb",
            "owner": "labeling",
            "security_definer": True,
            "fixed_targets": [
                "public.task_locks",
                "public.tasks",
                "aap_identity_v1.lock_accounts_share(uuid[],boolean)",
            ],
            "caller_transaction_required": True,
            "same_transaction_advisory_lock": True,
            "task_scope_key": TASK_LOCK_SCOPE_PREFIX + "<task UUID>",
            "table_access": {
                "public.task_locks": {
                    "select": [
                        "id",
                        "task_id",
                        "user_id",
                        "expire_at",
                        "unique_id",
                        "created_at",
                    ],
                    "insert": ["id", "task_id", "user_id", "expire_at", "unique_id"],
                    "update": ["expire_at"],
                    "delete": True,
                }
            },
            "actions": sorted(
                {
                    "acquire",
                    "active_lock",
                    "active_locks",
                    "assert_write_allowed",
                    "batch_final_write_available",
                    "cleanup_expired",
                    "heartbeat",
                    "is_locked",
                    "lock_scope",
                    "release",
                    "try_final_scope",
                }
            ),
        }
    }
    record = {"$ref": "#/$defs/TaskLockRecord"}
    nullable_record = {"anyOf": [record, {"type": "null"}]}
    identifiers = {"type": "array", "items": {"type": "string", "format": "uuid"}}

    def data_schema(properties):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(properties),
            "properties": properties,
        }

    replies = {
        "lock_scope": {"locked": {"const": True}},
        "try_final_scope": {"available": {"type": "boolean"}},
        "acquire": {
            "lock": nullable_record,
            "considered_ids": identifiers,
            "deleted_ids": identifiers,
        },
        "release": {
            "released": {"type": "boolean"},
            "lock": nullable_record,
        },
        "heartbeat": {
            "renewed": {"type": "boolean"},
            "lock": nullable_record,
        },
        "assert_write_allowed": {
            "owner_id": {
                "anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]
            },
            "deleted_ids": identifiers,
            "locks": {"type": "array", "items": record},
        },
        "is_locked": {
            "locked": {"type": "boolean"},
            "user_id": {
                "anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]
            },
            "lock": nullable_record,
            "deleted_ids": identifiers,
        },
        "active_lock": {"lock": nullable_record, "deleted_ids": identifiers},
        "active_locks": {
            "locks": {"type": "array", "items": record},
            "deleted_ids": identifiers,
        },
        "cleanup_expired": {
            "deleted_count": {"type": "integer", "minimum": 0},
            "deleted_ids": identifiers,
        },
        "batch_final_write_available": {"available": {"type": "boolean"}},
    }
    schema.setdefault("$defs", {})["TaskLockRecord"] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "task_id",
            "user_id",
            "expire_at",
            "unique_id",
            "created_at",
        ],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "task_id": {"type": "string", "format": "uuid"},
            "user_id": {"type": "string", "format": "uuid"},
            "expire_at": {"type": "string", "format": "date-time"},
            "unique_id": {"type": "string", "format": "uuid"},
            "created_at": {"type": "string", "format": "date-time"},
        },
    }
    schema["x-native-data-reply-schema"] = {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema", "action", "ok", "data"],
                "properties": {
                    "schema": {"const": TASK_LOCKS_DATA_SCHEMA},
                    "action": {"const": action},
                    "ok": {"const": True},
                    "data": data_schema(properties),
                },
            }
            for action, properties in replies.items()
        ]
    }
    return schema


__all__ = [
    "TASK_LOCKS_DATA_SCHEMA",
    "TASK_LOCKS_FUNCTION",
    "TASK_LOCKS_PROTOCOL",
    "TASK_LOCK_SCOPE_PREFIX",
    "TaskLockRecord",
    "TaskLocksCommand",
    "TaskLocksInvalidRequest",
    "TaskLocksUnavailable",
    "decode_task_locks_command",
    "decode_task_locks_reply",
    "task_locks_command",
    "task_locks_command_payload",
    "task_locks_schema",
]
