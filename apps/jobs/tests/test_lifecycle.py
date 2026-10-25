from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from aap_jobs.lifecycle import JobsLifecycleManager

FIXED_TIME = datetime(2026, 10, 24, 12, 30, tzinfo=timezone.utc)


class _RowProxy:
    def __init__(self, row, reads, values, events):
        object.__setattr__(self, "_row", row)
        object.__setattr__(self, "_reads", reads)
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "_events", events)

    def __getattr__(self, name):
        value = getattr(self._row, name)
        self._reads.setdefault(name, value)
        self._events.append(("read", name))
        return value

    def __setattr__(self, name, value):
        setattr(self._row, name, value)
        self._values[name] = value
        self._events.append(("write", name, value))


class _TraceData:
    def __init__(self, *rows):
        self.rows = {row.id: row for row in rows}
        self.commands = []
        self.events = []
        self.create_arguments = []

    @contextmanager
    def changes(self, row, operation, arguments: Mapping[str, object]):
        reads = {}
        values = {}
        events = []
        self.events.append(("changes_start", operation))
        try:
            yield _RowProxy(row, reads, values, events)
        finally:
            command = {
                "operation": operation,
                "reads": reads,
                "values": values.copy(),
                "written_fields": list(values),
                "arguments": dict(arguments),
                "events": events,
            }
            self.commands.append(command)
            self.events.append(("changes_end", operation))

    async def create_job(
        self,
        *,
        kind,
        user_id,
        project_id,
        payload,
        celery_task_id,
        status,
    ):
        self.create_arguments.append(
            {
                "kind": kind,
                "user_id": user_id,
                "project_id": project_id,
                "payload": payload,
                "celery_task_id": celery_task_id,
                "status": status,
            }
        )
        row = SimpleNamespace(
            id=uuid.uuid4(),
            kind=kind,
            user_id=user_id,
            project_id=project_id,
            payload=payload,
            celery_task_id=celery_task_id,
            status=status,
            progress_pct=0,
            result={},
            error_message=None,
            started_at=None,
            completed_at=None,
        )
        self.rows[row.id] = row
        self.events.append(("create", row.id))
        await self.flush()
        return row

    async def get_job(self, job_id):
        self.events.append(("get", job_id))
        return self.rows.get(job_id)

    async def flush(self):
        self.events.append(("flush",))


def _row(*, status="pending", payload=None, result=None, celery_task_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind="future_job_kind",
        user_id=None,
        project_id=None,
        payload={} if payload is None else payload,
        result={} if result is None else result,
        status=status,
        progress_pct=0,
        error_message=None,
        celery_task_id=celery_task_id,
        started_at=None,
        completed_at=None,
    )


def _manager(data, times=None):
    timestamps = iter(times or [FIXED_TIME])
    return JobsLifecycleManager(data, clock=lambda: next(timestamps))


def test_mark_running_records_only_original_reads_and_writes_in_order():
    row = _row()
    data = _TraceData(row)
    manager = _manager(data)

    manager.mark_running(row, celery_task_id="execution-1")

    command = data.commands[0]
    assert command["operation"] == "mark_running"
    assert command["reads"] == {
        "status": "pending",
        "celery_task_id": None,
    }
    assert command["written_fields"] == [
        "status",
        "started_at",
        "celery_task_id",
    ]
    assert row.status == "running"
    assert row.started_at == FIXED_TIME
    assert row.celery_task_id == "execution-1"


def test_mark_running_guards_cached_status_and_fills_execution_id_only_when_empty():
    row = _row(status="running", celery_task_id="existing")
    data = _TraceData(row)
    manager = _manager(data)

    manager.mark_running(row, celery_task_id="new")

    command = data.commands[0]
    assert command["reads"] == {"status": "running", "celery_task_id": "existing"}
    assert command["written_fields"] == []
    assert row.celery_task_id == "existing"


def test_progress_clamps_and_preserves_native_projection_values():
    row = _row(payload={"before": 1})
    data = _TraceData(row)
    manager = _manager(data)
    native_result = [False, None, "result"]

    manager.update_progress(
        row,
        120,
        extra_payload={"after": 2},
        result=native_result,
    )

    command = data.commands[0]
    assert command["written_fields"] == ["result", "progress_pct", "payload"]
    assert command["reads"] == {"payload": {"before": 1}}
    assert row.result is native_result
    assert row.progress_pct == 100
    assert row.payload == {"before": 1, "after": 2}


def test_progress_keeps_partial_result_write_when_integer_conversion_fails():
    row = _row()
    data = _TraceData(row)
    manager = _manager(data)
    native_result = {"partial": None}

    with pytest.raises(ValueError):
        manager.update_progress(row, "not-an-integer", result=native_result)

    command = data.commands[0]
    assert command["written_fields"] == ["result"]
    assert command["values"] == {"result": native_result}
    assert row.result is native_result
    assert row.progress_pct == 0


def test_terminal_guards_and_completion_write_order_match_cached_row_policy():
    guarded = _row(status="cancelled")
    data = _TraceData(guarded)
    manager = _manager(data)

    manager.mark_complete(guarded)

    assert data.commands[0]["reads"] == {"status": "cancelled"}
    assert data.commands[0]["written_fields"] == []
    assert data.events == [
        ("changes_start", "mark_complete"),
        ("changes_end", "mark_complete"),
    ]

    completed = _row(status="running")
    data = _TraceData(completed)
    manager = _manager(data)
    manager.mark_complete(completed, result=False)

    assert data.commands[0]["written_fields"] == [
        "status",
        "progress_pct",
        "completed_at",
        "result",
    ]
    assert completed.status == "completed"
    assert completed.progress_pct == 100
    assert completed.completed_at == FIXED_TIME
    assert completed.result is False


def test_failure_clips_error_and_cancel_returns_the_original_row_identity():
    failed = _row(status="running")
    data = _TraceData(failed)
    manager = _manager(data)
    manager.mark_failed(failed, error="e" * 4003, result=None)

    assert data.commands[0]["written_fields"] == [
        "status",
        "completed_at",
        "error_message",
    ]
    assert len(failed.error_message) == 4000

    cancelled = _row(status="completed")
    data = _TraceData(cancelled)
    manager = _manager(data)
    returned = manager.mark_cancelled(cancelled)
    assert returned is cancelled
    assert data.commands[0]["written_fields"] == []

    returned = manager.mark_cancelled(
        cancelled,
        allowed_statuses=None,
        progress_pct=-5,
        result={"reason": "forced"},
    )
    assert returned is cancelled
    assert cancelled.status == "cancelled"
    assert cancelled.progress_pct == 0
    assert cancelled.result == {"reason": "forced"}


def test_cancel_keeps_status_and_timestamp_prefix_when_progress_conversion_fails():
    row = _row(status="running")
    data = _TraceData(row)
    manager = _manager(data)

    with pytest.raises(ValueError):
        manager.mark_cancelled(
            row,
            allowed_statuses=None,
            progress_pct="bad-progress",
            result={"must_not_be_written": True},
        )

    assert data.commands[0]["written_fields"] == ["status", "completed_at"]
    assert row.status == "cancelled"
    assert row.completed_at == FIXED_TIME
    assert row.result == {}


def test_request_cancel_merges_payload_and_uses_isoformat_timestamp():
    original_payload = {"existing": [1, None]}
    row = _row(status="running", payload=original_payload)
    data = _TraceData(row)
    manager = _manager(data)

    manager.request_cancel(row)

    assert row.payload == {
        "existing": [1, None],
        "cancel_requested": True,
        "cancel_requested_at": "2026-10-24T12:30:00+00:00",
    }
    assert data.commands[0]["reads"] == {
        "status": "running",
        "payload": original_payload,
    }
    assert data.commands[0]["written_fields"] == ["payload"]


@pytest.mark.asyncio
async def test_create_normalizes_falsey_payload_and_keeps_add_flush_in_data_port():
    data = _TraceData()
    manager = _manager(data)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()

    row = await manager.create_job(
        kind="open-kind",
        user_id=user_id,
        project_id=project_id,
        payload=[],
        celery_task_id="execution-id",
    )

    assert data.create_arguments == [
        {
            "kind": "open-kind",
            "user_id": user_id,
            "project_id": project_id,
            "payload": {},
            "celery_task_id": "execution-id",
            "status": "pending",
        }
    ]
    assert data.events == [("create", row.id), ("flush",)]


@pytest.mark.asyncio
async def test_track_job_keeps_cache_lookups_and_terminal_flush_order():
    data = _TraceData()
    manager = _manager(data, [FIXED_TIME, FIXED_TIME])

    async with manager.track_job(kind="short_job", payload={"input": 1}) as row:
        assert row.status == "running"
        data.events.append(("body",))

    assert [event[0] for event in data.events] == [
        "create",
        "flush",
        "get",
        "changes_start",
        "changes_end",
        "flush",
        "body",
        "get",
        "changes_start",
        "changes_end",
        "flush",
    ]
    assert row.status == "completed"
    assert row.completed_at == FIXED_TIME


@pytest.mark.asyncio
async def test_track_job_marks_failure_and_reraises_original_exception():
    data = _TraceData()
    manager = _manager(data, [FIXED_TIME, FIXED_TIME])

    class TracedError(RuntimeError):
        def __str__(self):
            data.events.append(("str_error",))
            return "failed body"

    with pytest.raises(TracedError):
        async with manager.track_job(kind="short_job") as row:
            assert row.status == "running"
            raise TracedError()

    assert row.status == "failed"
    assert row.error_message == "failed body"
    assert data.commands[-1]["operation"] == "mark_failed"
    assert data.events[-5:] == [
        ("str_error",),
        ("get", row.id),
        ("changes_start", "mark_failed"),
        ("changes_end", "mark_failed"),
        ("flush",),
    ]
    assert data.events[-1] == ("flush",)
