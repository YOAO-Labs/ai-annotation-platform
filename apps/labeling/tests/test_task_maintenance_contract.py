import json
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from aap_labeling.task_maintenance_contract import (
    TASK_MAINTENANCE_PROTOCOL,
    BatchCounterPatch,
    LockSourceLessTasks,
    ReadSourceLessBatchCounters,
    ReadSourceLessTaskCandidates,
    TaskMaintenanceUnavailable,
    WriteSourceLessBatchCounters,
    decode_task_maintenance_reply,
    task_maintenance_schema,
)


def _reply(request, data):
    return {
        "schema": TASK_MAINTENANCE_PROTOCOL,
        "ok": True,
        "request": json.loads(request.model_dump_json(by_alias=True)),
        "data": data,
    }


def test_task_maintenance_schema_matches_packaged_and_frozen_artifacts():
    schema = task_maintenance_schema()
    assert schema["$id"] == TASK_MAINTENANCE_PROTOCOL
    assert schema["x-postgresql-functions"] == [
        "aap_labeling_v1.task_maintenance_command(jsonb)"
    ]
    assert (
        json.loads(
            files("aap_labeling")
            .joinpath("task-maintenance.schema.json")
            .read_text(encoding="utf-8")
        )
        == schema
    )

    repository = Path(__file__).resolve().parents[3]
    frozen_schema = json.loads(
        (
            repository
            / "apps/api/alembic/sql/0355_labeling_task_maintenance.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert frozen_schema == schema
    assert files("aap_labeling").joinpath(
        "sql/0355_labeling_task_maintenance.sql"
    ).read_text(encoding="utf-8") == (
        repository / "apps/api/alembic/sql/0355_labeling_task_maintenance.sql"
    ).read_text(encoding="utf-8")


def test_source_less_candidates_are_sorted_and_scoped_to_the_request():
    project_id = uuid4()
    task_id = uuid4()
    request = ReadSourceLessTaskCandidates(
        schema=TASK_MAINTENANCE_PROTOCOL,
        operation="read_source_less_task_candidates",
        project_id=project_id,
    )
    data = {
        "operation": "read_source_less_task_candidates",
        "project_id": str(project_id),
        "task_ids": [str(task_id)],
    }
    assert decode_task_maintenance_reply(request, _reply(request, data)).task_ids == [
        task_id
    ]
    data["project_id"] = str(uuid4())
    with pytest.raises(TaskMaintenanceUnavailable):
        decode_task_maintenance_reply(request, _reply(request, data))


def test_task_lock_reply_may_only_return_a_sorted_subset_of_candidates():
    project_id = uuid4()
    task_ids = sorted([uuid4(), uuid4()], key=str)
    request = LockSourceLessTasks(
        schema=TASK_MAINTENANCE_PROTOCOL,
        operation="lock_source_less_tasks",
        project_id=project_id,
        task_ids=task_ids,
    )
    data = {
        "operation": "lock_source_less_tasks",
        "project_id": str(project_id),
        "task_ids": [str(task_ids[0])],
    }
    assert decode_task_maintenance_reply(request, _reply(request, data)).task_ids == [
        task_ids[0]
    ]
    data["task_ids"] = [str(uuid4())]
    with pytest.raises(TaskMaintenanceUnavailable):
        decode_task_maintenance_reply(request, _reply(request, data))


def test_batch_counter_read_matches_every_requested_batch():
    project_id = uuid4()
    batch_ids = sorted([uuid4(), uuid4()], key=str)
    request = ReadSourceLessBatchCounters(
        schema=TASK_MAINTENANCE_PROTOCOL,
        operation="read_source_less_batch_counters",
        project_id=project_id,
        batch_ids=batch_ids,
    )
    data = {
        "operation": "read_source_less_batch_counters",
        "project_id": str(project_id),
        "batches": [
            {
                "batch_id": str(batch_id),
                "total_tasks": index,
                "completed_tasks": 0,
                "review_tasks": 0,
            }
            for index, batch_id in enumerate(batch_ids)
        ],
    }
    decoded = decode_task_maintenance_reply(request, _reply(request, data))
    assert [row.batch_id for row in decoded.batches] == batch_ids
    data["batches"].pop()
    with pytest.raises(TaskMaintenanceUnavailable):
        decode_task_maintenance_reply(request, _reply(request, data))


def test_batch_counter_write_preserves_explicit_timestamp_and_row_scope():
    project_id = uuid4()
    batch_id = uuid4()
    timestamp = datetime(2026, 10, 25, tzinfo=timezone.utc)
    request = WriteSourceLessBatchCounters(
        schema=TASK_MAINTENANCE_PROTOCOL,
        operation="write_source_less_batch_counters",
        project_id=project_id,
        batches=[
            BatchCounterPatch(
                batch_id=batch_id,
                total_tasks_set=True,
                total_tasks=3,
                completed_tasks_set=False,
                review_tasks_set=False,
                updated_at_set=True,
                updated_at=timestamp,
            )
        ],
    )
    data = {
        "operation": "write_source_less_batch_counters",
        "project_id": str(project_id),
        "batches": [
            {
                "batch_id": str(batch_id),
                "project_id": str(project_id),
                "total_tasks": 3,
                "completed_tasks": 1,
                "review_tasks": 1,
                "updated_at": timestamp.isoformat(),
            }
        ],
    }
    assert decode_task_maintenance_reply(request, _reply(request, data)).batches[
        0
    ].batch_id == UUID(str(batch_id))
    data["batches"][0]["project_id"] = str(uuid4())
    with pytest.raises(TaskMaintenanceUnavailable):
        decode_task_maintenance_reply(request, _reply(request, data))
