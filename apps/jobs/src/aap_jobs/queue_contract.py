"""Closed HTTP contract for the private Jobs Celery queue owner."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

JOBS_QUEUE_PROTOCOL = "aap-jobs-queue/v2"
JOBS_QUEUE_PATH = "/v1/jobs/queue"
MAX_EXECUTION_ID_LENGTH = 64


class JobsQueueContractError(ValueError):
    """The wire payload is outside the frozen queue contract."""


class JobsQueueUnavailable(RuntimeError):
    """The broker owner did not confirm dispatch or revoke acceptance."""


@dataclass(frozen=True, slots=True)
class QueueTaskSpec:
    task_name: str
    execution_id: str
    args: list[Any]
    kwargs: dict[str, Any]
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QueueDispatch:
    task_name: str
    execution_id: str
    args: list[Any]
    kwargs: dict[str, Any]
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QueueCanvas:
    execution_id: str
    group_id: str
    tasks: list[QueueTaskSpec]
    callback: QueueTaskSpec


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


_TASK_SPEC_KEYS = {"task_name", "execution_id", "args", "kwargs", "options"}


def _task_spec(value: Any) -> QueueTaskSpec:
    if type(value) is not dict or set(value) != _TASK_SPEC_KEYS:
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
    ):
        raise JobsQueueContractError()
    _native_json(args)
    _native_json(kwargs)
    if any(type(key) is not str for key in options):
        raise JobsQueueContractError()
    if set(options) - {"queue", "soft_time_limit"}:
        raise JobsQueueContractError()
    if "queue" in options and (
        type(options["queue"]) is not str or not options["queue"]
    ):
        raise JobsQueueContractError()
    if "soft_time_limit" in options:
        limit = options["soft_time_limit"]
        if (
            type(limit) not in (int, float)
            or limit <= 0
            or (type(limit) is float and not math.isfinite(limit))
        ):
            raise JobsQueueContractError()
    _native_json(options)
    return QueueTaskSpec(
        task_name=task_name,
        execution_id=_execution_id(value["execution_id"]),
        args=args,
        kwargs=kwargs,
        options=options,
    )


def _dispatch(value: dict[str, Any]) -> QueueDispatch:
    if set(value) != {"schema", "operation", *_TASK_SPEC_KEYS}:
        raise JobsQueueContractError()
    spec = _task_spec(
        {
            "task_name": value["task_name"],
            "execution_id": value["execution_id"],
            "args": value["args"],
            "kwargs": value["kwargs"],
            "options": value["options"],
        }
    )
    return QueueDispatch(
        task_name=spec.task_name,
        execution_id=spec.execution_id,
        args=spec.args,
        kwargs=spec.kwargs,
        options=spec.options,
    )


def _canvas(value: dict[str, Any]) -> QueueCanvas:
    if set(value) != {
        "schema",
        "operation",
        "execution_id",
        "group_id",
        "tasks",
        "callback",
    }:
        raise JobsQueueContractError()
    execution_id = _execution_id(value["execution_id"])
    group_id = _execution_id(value["group_id"])
    raw_tasks = value["tasks"]
    if type(raw_tasks) is not list or not raw_tasks:
        raise JobsQueueContractError()
    callback = _task_spec(value["callback"])
    if callback.execution_id != execution_id:
        raise JobsQueueContractError()
    seen_ids = {group_id, execution_id}
    if len(seen_ids) != 2:
        raise JobsQueueContractError()
    tasks = []
    for raw_task in raw_tasks:
        task = _task_spec(raw_task)
        if task.execution_id in seen_ids:
            raise JobsQueueContractError()
        seen_ids.add(task.execution_id)
        tasks.append(task)
    return QueueCanvas(
        execution_id=execution_id,
        group_id=group_id,
        tasks=tasks,
        callback=callback,
    )


def _queue_request(value: Any) -> QueueDispatch | QueueCanvas | QueueRevoke:
    if (
        type(value) is not dict
        or value.get("schema") != JOBS_QUEUE_PROTOCOL
        or type(value.get("operation")) is not str
    ):
        raise JobsQueueContractError()
    if value["operation"] == "dispatch":
        return _dispatch(value)
    if value["operation"] == "canvas":
        return _canvas(value)
    if value["operation"] == "revoke":
        if set(value) != {"schema", "operation", "execution_id"}:
            raise JobsQueueContractError()
        return QueueRevoke(_execution_id(value["execution_id"]))
    raise JobsQueueContractError()


def decode_queue_request(
    body: bytes,
) -> QueueDispatch | QueueCanvas | QueueRevoke:
    try:
        value = json.loads(
            body,
            parse_float=_finite_float,
            parse_constant=_invalid_constant,
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise JobsQueueContractError() from exc
    return _queue_request(value)


def _encode_json(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise JobsQueueContractError() from exc


def encode_queue_request(value: dict[str, Any]) -> bytes:
    _queue_request(value)
    return _encode_json(value)


def encode_dispatch_request(
    task_name: str,
    execution_id: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> bytes:
    return encode_queue_request(
        {
            "schema": JOBS_QUEUE_PROTOCOL,
            "operation": "dispatch",
            "task_name": task_name,
            "execution_id": execution_id,
            "args": args,
            "kwargs": {} if kwargs is None else kwargs,
            "options": {} if options is None else options,
        }
    )


def encode_canvas_request(
    execution_id: str,
    group_id: str,
    tasks: list[dict[str, Any]],
    callback: dict[str, Any],
) -> bytes:
    return encode_queue_request(
        {
            "schema": JOBS_QUEUE_PROTOCOL,
            "operation": "canvas",
            "execution_id": execution_id,
            "group_id": group_id,
            "tasks": tasks,
            "callback": callback,
        }
    )


def encode_revoke_request(execution_id: str) -> bytes:
    return encode_queue_request(
        {
            "schema": JOBS_QUEUE_PROTOCOL,
            "operation": "revoke",
            "execution_id": execution_id,
        }
    )


def encode_queue_reply(*, accepted: bool, execution_id: str) -> bytes:
    if type(accepted) is not bool:
        raise JobsQueueContractError()
    value = {
        "schema": JOBS_QUEUE_PROTOCOL,
        "accepted": accepted,
        "execution_id": _execution_id(execution_id),
    }
    return _encode_json(value)


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


def load_queue_configuration() -> dict[str, Any]:
    """Load the shared Celery route/default/serializer configuration resource."""
    from importlib.resources import files

    try:
        value = json.loads(
            files("aap_jobs").joinpath("queue_routes.json").read_text(encoding="utf-8"),
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise JobsQueueContractError() from exc
    if (
        type(value) is not dict
        or set(value)
        != {
            "task_default_queue",
            "task_serializer",
            "result_serializer",
            "accept_content",
            "task_routes",
        }
        or value["task_default_queue"] != "default"
        or value["task_serializer"] != "json"
        or value["result_serializer"] != "json"
        or value["accept_content"] != ["json"]
        or type(value["task_routes"]) is not dict
    ):
        raise JobsQueueContractError()
    for task_name, route in value["task_routes"].items():
        if (
            type(task_name) is not str
            or not task_name
            or type(route) is not dict
            or set(route) != {"queue"}
            or type(route["queue"]) is not str
            or not route["queue"]
        ):
            raise JobsQueueContractError()
    return value
