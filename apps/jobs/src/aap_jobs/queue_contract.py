"""Closed HTTP contract for the private Jobs Celery queue owner."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

JOBS_QUEUE_PROTOCOL = "aap-jobs-queue/v1"
JOBS_QUEUE_PATH = "/v1/jobs/queue"
MAX_EXECUTION_ID_LENGTH = 64


class JobsQueueContractError(ValueError):
    """The wire payload is outside the frozen queue contract."""


class JobsQueueUnavailable(RuntimeError):
    """The broker owner did not confirm dispatch or revoke acceptance."""


@dataclass(frozen=True, slots=True)
class QueueDispatch:
    task_name: str
    execution_id: str
    args: list[Any]
    kwargs: dict[str, Any]
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QueueRevoke:
    execution_id: str


@dataclass(frozen=True, slots=True)
class QueueReply:
    accepted: bool
    execution_id: str


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JobsQueueContractError()
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise JobsQueueContractError()
    return parsed


def _invalid_constant(_value: str) -> Any:
    raise JobsQueueContractError()


def _execution_id(value: Any) -> str:
    if type(value) is not str or not value or len(value) > MAX_EXECUTION_ID_LENGTH:
        raise JobsQueueContractError()
    return value


def _native_json(value: Any) -> None:
    """Validate JSON-native values without recursive model serialization."""
    pending = [value]
    seen_containers: set[int] = set()
    while pending:
        item = pending.pop()
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise JobsQueueContractError()
            continue
        if type(item) is list:
            identity = id(item)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend(item)
            continue
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise JobsQueueContractError()
            identity = id(item)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend(item.values())
            continue
        raise JobsQueueContractError()


def _dispatch(value: dict[str, Any]) -> QueueDispatch:
    if set(value) != {
        "schema",
        "operation",
        "task_name",
        "execution_id",
        "args",
        "kwargs",
        "options",
    }:
        raise JobsQueueContractError()
    task_name = value["task_name"]
    args = value["args"]
    kwargs = value["kwargs"]
    options = value["options"]
    if (
        type(task_name) is not str
        or not task_name
        or type(args) is not list
        or type(kwargs) is not dict
        or type(options) is not dict
        or options
    ):
        raise JobsQueueContractError()
    _native_json(args)
    _native_json(kwargs)
    return QueueDispatch(
        task_name=task_name,
        execution_id=_execution_id(value["execution_id"]),
        args=args,
        kwargs=kwargs,
        options=options,
    )


def decode_queue_request(body: bytes) -> QueueDispatch | QueueRevoke:
    try:
        value = json.loads(
            body,
            parse_float=_finite_float,
            parse_constant=_invalid_constant,
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise JobsQueueContractError() from exc
    if (
        type(value) is not dict
        or value.get("schema") != JOBS_QUEUE_PROTOCOL
        or type(value.get("operation")) is not str
    ):
        raise JobsQueueContractError()
    if value["operation"] == "dispatch":
        return _dispatch(value)
    if value["operation"] == "revoke":
        if set(value) != {"schema", "operation", "execution_id"}:
            raise JobsQueueContractError()
        return QueueRevoke(_execution_id(value["execution_id"]))
    raise JobsQueueContractError()


def encode_dispatch_request(
    task_name: str,
    execution_id: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> bytes:
    value = {
        "schema": JOBS_QUEUE_PROTOCOL,
        "operation": "dispatch",
        "task_name": task_name,
        "execution_id": execution_id,
        "args": args,
        "kwargs": {} if kwargs is None else kwargs,
        "options": {} if options is None else options,
    }
    _dispatch(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise JobsQueueContractError() from exc


def encode_revoke_request(execution_id: str) -> bytes:
    value = {
        "schema": JOBS_QUEUE_PROTOCOL,
        "operation": "revoke",
        "execution_id": _execution_id(execution_id),
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def encode_queue_reply(*, accepted: bool, execution_id: str) -> bytes:
    if type(accepted) is not bool:
        raise JobsQueueContractError()
    value = {
        "schema": JOBS_QUEUE_PROTOCOL,
        "accepted": accepted,
        "execution_id": _execution_id(execution_id),
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def decode_queue_reply(body: bytes) -> QueueReply:
    try:
        value = json.loads(body, object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise JobsQueueContractError() from exc
    if (
        type(value) is not dict
        or set(value) != {"schema", "accepted", "execution_id"}
        or value.get("schema") != JOBS_QUEUE_PROTOCOL
        or type(value.get("accepted")) is not bool
    ):
        raise JobsQueueContractError()
    return QueueReply(
        accepted=value["accepted"],
        execution_id=_execution_id(value["execution_id"]),
    )
