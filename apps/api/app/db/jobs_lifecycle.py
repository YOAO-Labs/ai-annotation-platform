"""Keep borrowed ORM flushes on the Jobs owner's fixed write capabilities."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, attributes, object_session
from sqlalchemy.orm.exc import StaleDataError

_TRACE = "aap_jobs_lifecycle_commands"
_TABLES = set()


def _snapshot(value):
    if isinstance(value, dict):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot(item) for item in value]
    return value


def _column_snapshot(name, value):
    if name in {"started_at", "completed_at", "created_at", "updated_at"}:
        if isinstance(value, datetime):
            return value.isoformat()
    elif name in {"id", "user_id", "project_id", "retained_project_id"}:
        if value is not None:
            return str(value)
    return _snapshot(value)


def encode_jobs_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class _ObservedRow:
    __slots__ = ("_row", "_command")

    def __init__(self, row, command):
        object.__setattr__(self, "_row", row)
        object.__setattr__(self, "_command", command)

    def __getattr__(self, name):
        value = getattr(self._row, name)
        if name not in self._command["reads"]:
            self._command["reads"][name] = _column_snapshot(name, value)
        return value

    def __setattr__(self, name, value):
        setattr(self._row, name, value)
        self._command["values"][name] = _column_snapshot(name, value)
        self._command["written_fields"].append(name)


@contextlib.contextmanager
def observe_job_changes(row, operation: str, arguments: Mapping):
    command = {
        "operation": operation,
        "reads": {},
        "values": {},
        "written_fields": [],
        "arguments": arguments,
    }
    try:
        yield _ObservedRow(row, command)
    finally:
        # Retain the successful prefix even if the original operation raises.
        if command["written_fields"]:
            command["arguments"] = _snapshot(arguments)
            values = command["values"]
            if operation == "update_progress":
                command["arguments"]["pct"] = values.get("progress_pct")
                if "payload" not in values:
                    command["arguments"]["extra_payload"] = None
            elif operation == "mark_cancelled":
                if command["arguments"]["progress_pct"] is not None:
                    command["arguments"]["progress_pct"] = values.get("progress_pct")
                if "result" not in values:
                    command["arguments"]["result"] = None
            elif operation == "mark_failed":
                command["arguments"]["error"] = values.get("error_message")
                if "result" not in values:
                    command["arguments"]["result"] = None
            allowed = command["arguments"].get("allowed_statuses")
            if isinstance(allowed, (set, frozenset)):
                command["arguments"]["allowed_statuses"] = list(allowed)
            inspect(row).info.setdefault(_TRACE, []).append(command)


def lifecycle_flush_request(target, changes):
    commands = inspect(target).info.get(_TRACE, [])
    last_values = {}
    for command in commands:
        last_values.update(command["values"])
    projection, legacy = {}, {}
    for name, value in changes.items():
        value = _column_snapshot(name, value)
        if name == "updated_at":
            continue
        if name in {"payload", "result"}:
            projection[name] = value
            continue
        if name in last_values and encode_jobs_json(value) == encode_jobs_json(
            last_values[name]
        ):
            continue
        legacy[name] = value
    return {
        "schema": "aap-jobs-lifecycle/v1",
        "commands": commands,
        "projection_changes": projection,
        "changed_fields": list(changes),
        "updated_at_override_present": "updated_at" in changes,
        "updated_at_override": _column_snapshot(
            "updated_at", changes.get("updated_at")
        ),
        "legacy_changes": legacy,
    }


def _apply_job_update(mapper, connection, target):
    if connection.dialect.name != "postgresql":
        inspect(target).info.pop(_TRACE, None)
        return
    state = inspect(target)
    changes = {
        prop.key: state.dict[prop.key]
        for prop in mapper.column_attrs
        if state.attrs[prop.key].history.has_changes()
    }
    if not changes:
        state.info.pop(_TRACE, None)
        return
    result = connection.execute(
        text(
            "SELECT applied, updated_at FROM aap_jobs_v1.apply_lifecycle("
            "CAST(:job_id AS pg_catalog.uuid), CAST(:request AS pg_catalog.jsonb))"
        ),
        {
            "job_id": state.identity[0],
            "request": encode_jobs_json(lifecycle_flush_request(target, changes)),
        },
    ).one()
    if not result.applied:
        raise StaleDataError("Jobs lifecycle row no longer exists")
    for name, value in changes.items():
        attributes.set_committed_value(target, name, value)
    if "updated_at" not in changes:
        object_session(target).expire(target, ["updated_at"])
    state.info.pop(_TRACE, None)


def _retarget_job_insert(connection, cursor, statement, parameters, context, many):
    command = getattr(getattr(context, "compiled", None), "statement", None)
    table = getattr(command, "table", None)
    if (
        connection.dialect.name == "postgresql"
        and getattr(command, "is_insert", False)
        and table is not None
        and table._deannotate() in _TABLES
    ):
        prefix = "INSERT INTO async_jobs "
        if not statement.startswith(prefix):
            raise RuntimeError("unexpected compiled Jobs INSERT target")
        statement = statement.replace(
            prefix,
            "INSERT INTO aap_jobs_v1.async_job_creation AS async_jobs ",
            1,
        )
    return statement, parameters


def _clear_insert_trace(mapper, connection, target):
    inspect(target).info.pop(_TRACE, None)


def _clear_rolled_back_traces(session, *_):
    states = list(session.identity_map.all_states())
    states.extend(inspect(row) for row in session.new)
    for state in states:
        state.info.pop(_TRACE, None)


def install_jobs_owner_mapping(model):
    """Register once for every engine using this existing mapped table."""
    if model.__table__ in _TABLES:
        return
    _TABLES.add(model.__table__)
    event.listen(model, "before_update", _apply_job_update)
    event.listen(model, "after_insert", _clear_insert_trace)
    if not event.contains(Engine, "before_cursor_execute", _retarget_job_insert):
        event.listen(Engine, "before_cursor_execute", _retarget_job_insert, retval=True)
        event.listen(Session, "after_rollback", _clear_rolled_back_traces)
        event.listen(Session, "after_soft_rollback", _clear_rolled_back_traces)
