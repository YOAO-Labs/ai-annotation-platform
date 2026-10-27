"""Jobs and Audit support contracts for Dataset/DatasetItem deletion."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.models.async_job import AsyncJob
from tests.factory import create_project

_GUARD_DATASET_SOURCE_DELETION = text(
    "SELECT aap_jobs_v1.guard_dataset_source_deletion(CAST(:dataset_id AS uuid))"
)
_TASK_CREATION_EXECUTION_ACTIVE = text(
    "SELECT aap_jobs_v1.task_creation_execution_active("
    "CAST(:operation_id AS uuid), CAST(:job_id AS uuid))"
)
_RECORD_SOURCE_DELETION = text(
    "SELECT aap_audit_v1.record_dataset_source_deletion("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:item_id AS uuid), "
    "CAST(:deleted_items AS bigint), CAST(:deleted_scenes AS bigint), "
    "CAST(:deleted_associations AS bigint), CAST(:discarded_uploads AS bigint), "
    "CAST(:cancelled_jobs AS bigint), CAST(:queued_storage AS bigint), "
    "CAST(:context AS jsonb))"
)
_READ_AUDIT = text(
    "SELECT actor_id, actor_email, actor_role, action, target_type, target_id, "
    "method, path, status_code, ip::text AS ip, request_id, detail_json "
    "FROM public.audit_logs WHERE id = :audit_id"
)


def _creation_payload(
    job_id: uuid.UUID,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    association_id: uuid.UUID,
) -> dict[str, str]:
    return {
        "schema": "aap-create-tasks/v1",
        "operation_id": str(job_id),
        "actor_id": str(actor_id),
        "dataset_id": str(dataset_id),
        "project_id": str(project_id),
        "association_id": str(association_id),
        "mode": "link",
    }


async def _insert_creation_job(
    db_session,
    *,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    status: str = "pending",
    cancel_requested: bool = False,
    payload_project_id: uuid.UUID | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    payload: dict[str, object] = _creation_payload(
        job_id,
        actor_id,
        dataset_id,
        payload_project_id if payload_project_id is not None else project_id,
        uuid.uuid4(),
    )
    if cancel_requested:
        payload["cancel_requested"] = True
        payload["cancel_requested_at"] = "2026-10-18T00:00:00+00:00"
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="create_tasks",
            project_id=project_id,
            user_id=actor_id,
            status=status,
            payload=payload,
            result={"retained": str(job_id)},
        )
    )
    await db_session.flush()
    return job_id


@pytest.mark.asyncio
async def test_dataset_import_guard_fails_closed_without_mutating_jobs(
    db_session, super_admin
):
    actor, _ = super_admin
    target_dataset_id = uuid.uuid4()
    other_dataset_id = uuid.uuid4()
    target_job = AsyncJob(
        kind="dataset_import",
        user_id=actor.id,
        status="pending",
        payload={"dataset_id": str(target_dataset_id)},
    )
    other_job = AsyncJob(
        kind="dataset_import",
        user_id=actor.id,
        status="running",
        payload={"dataset_id": str(other_dataset_id)},
    )
    malformed_jobs = [
        AsyncJob(
            kind="dataset_import",
            user_id=actor.id,
            status="running",
            payload={},
        ),
        AsyncJob(
            kind="dataset_import",
            user_id=actor.id,
            status="pending",
            payload={"dataset_id": "not-a-uuid"},
        ),
        AsyncJob(
            kind="dataset_import",
            user_id=actor.id,
            status="running",
            payload={"dataset_id": 123},
        ),
    ]
    db_session.add_all([target_job, other_job, *malformed_jobs])
    await db_session.flush()

    result = await db_session.scalar(
        _GUARD_DATASET_SOURCE_DELETION,
        {"dataset_id": str(target_dataset_id)},
    )
    assert result == {
        "schema": "aap-source-retention/v1",
        "ok": False,
        "error": {"code": "source_busy"},
    }
    await db_session.refresh(target_job)
    assert target_job.status == "pending"
    assert target_job.payload == {"dataset_id": str(target_dataset_id)}
    assert target_job.result == {}

    # A well-formed import for another Dataset does not block this one.
    await db_session.delete(target_job)
    for job in malformed_jobs:
        await db_session.delete(job)
    await db_session.flush()
    assert await db_session.scalar(
        _GUARD_DATASET_SOURCE_DELETION,
        {"dataset_id": str(target_dataset_id)},
    ) == {"schema": "aap-source-retention/v1", "ok": True, "data": {}}

    await db_session.refresh(other_job)
    assert other_job.status == "running"
    assert other_job.payload == {"dataset_id": str(other_dataset_id)}
    assert other_job.result == {}


@pytest.mark.asyncio
async def test_dataset_import_guard_ignores_terminal_imports(db_session, super_admin):
    actor, _ = super_admin
    dataset_id = uuid.uuid4()
    db_session.add(
        AsyncJob(
            kind="dataset_import",
            user_id=actor.id,
            status="completed",
            payload={"dataset_id": str(dataset_id)},
        )
    )
    await db_session.flush()

    assert await db_session.scalar(
        _GUARD_DATASET_SOURCE_DELETION, {"dataset_id": str(dataset_id)}
    ) == {"schema": "aap-source-retention/v1", "ok": True, "data": {}}


@pytest.mark.asyncio
async def test_task_creation_execution_active_is_fail_closed_and_scope_checked(
    db_session, super_admin
):
    actor, _ = super_admin
    operation_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    project = await create_project(db_session, owner_id=actor.id)
    project_id = project.id

    # A synchronous accepted operation has no AsyncJob to prove completion.
    assert (
        await db_session.scalar(
            _TASK_CREATION_EXECUTION_ACTIVE,
            {"operation_id": str(operation_id), "job_id": None},
        )
        is True
    )
    assert (
        await db_session.scalar(
            _TASK_CREATION_EXECUTION_ACTIVE,
            {"operation_id": str(operation_id), "job_id": str(uuid.uuid4())},
        )
        is True
    )

    pending_job_id = await _insert_creation_job(
        db_session,
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project_id,
    )
    assert (
        await db_session.scalar(
            _TASK_CREATION_EXECUTION_ACTIVE,
            {"operation_id": str(pending_job_id), "job_id": str(pending_job_id)},
        )
        is True
    )
    assert (
        await db_session.scalar(
            _TASK_CREATION_EXECUTION_ACTIVE,
            {
                "operation_id": str(uuid.uuid4()),
                "job_id": str(pending_job_id),
            },
        )
        is True
    )

    running_job_id = await _insert_creation_job(
        db_session,
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project_id,
        status="running",
    )
    assert (
        await db_session.scalar(
            _TASK_CREATION_EXECUTION_ACTIVE,
            {"operation_id": str(running_job_id), "job_id": str(running_job_id)},
        )
        is True
    )

    cancelled_request_id = await _insert_creation_job(
        db_session,
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project_id,
        status="running",
        cancel_requested=True,
    )
    assert (
        await db_session.scalar(
            _TASK_CREATION_EXECUTION_ACTIVE,
            {
                "operation_id": str(cancelled_request_id),
                "job_id": str(cancelled_request_id),
            },
        )
        is False
    )

    for terminal_status in ("completed", "failed", "cancelled"):
        terminal_job_id = await _insert_creation_job(
            db_session,
            actor_id=actor.id,
            dataset_id=dataset_id,
            project_id=project_id,
            status=terminal_status,
        )
        assert (
            await db_session.scalar(
                _TASK_CREATION_EXECUTION_ACTIVE,
                {
                    "operation_id": str(terminal_job_id),
                    "job_id": str(terminal_job_id),
                },
            )
            is False
        )

    inconsistent_job_id = await _insert_creation_job(
        db_session,
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project_id,
        payload_project_id=uuid.uuid4(),
    )
    assert (
        await db_session.scalar(
            _TASK_CREATION_EXECUTION_ACTIVE,
            {
                "operation_id": str(inconsistent_job_id),
                "job_id": str(inconsistent_job_id),
            },
        )
        is True
    )


@pytest.mark.asyncio
async def test_dataset_source_audit_uses_fixed_targets_and_metadata(
    db_session, super_admin
):
    actor, _ = super_admin
    dataset_id = uuid.uuid4()
    item_id = uuid.uuid4()
    request_id = uuid.uuid4()
    context = {
        "method": "DELETE",
        "path": f"/api/v1/datasets/{dataset_id}",
        "ip": "127.0.0.1",
        "request_id": str(request_id),
    }

    requests = (
        {
            "operation_id": uuid.uuid4(),
            "actor_id": actor.id,
            "dataset_id": dataset_id,
            "item_id": None,
            "deleted_items": 3,
            "deleted_scenes": 1,
            "deleted_associations": 2,
            "discarded_uploads": 1,
            "cancelled_jobs": 4,
            "queued_storage": 3,
            "context": json.dumps(context),
            "action": "dataset.delete",
            "target_type": "dataset",
            "target_id": str(dataset_id),
        },
        {
            "operation_id": uuid.uuid4(),
            "actor_id": actor.id,
            "dataset_id": dataset_id,
            "item_id": item_id,
            "deleted_items": 1,
            "deleted_scenes": 0,
            "deleted_associations": 0,
            "discarded_uploads": 0,
            "cancelled_jobs": 0,
            "queued_storage": 1,
            "context": json.dumps(
                {**context, "path": f"/api/v1/datasets/{dataset_id}/items/{item_id}"}
            ),
            "action": "dataset.item_delete",
            "target_type": "dataset_item",
            "target_id": str(item_id),
        },
    )

    for request in requests:
        audit_id = await db_session.scalar(
            _RECORD_SOURCE_DELETION,
            {
                key: request[key]
                for key in request
                if key not in {"action", "target_type", "target_id"}
            },
        )
        row = (await db_session.execute(_READ_AUDIT, {"audit_id": audit_id})).one()
        assert row.actor_id == actor.id
        assert row.actor_email == actor.email
        assert row.actor_role == "super_admin"
        assert (row.action, row.target_type, row.target_id) == (
            request["action"],
            request["target_type"],
            request["target_id"],
        )
        assert row.status_code == 204
        assert row.ip.startswith("127.0.0.1")
        assert row.detail_json == {
            "operation_id": str(request["operation_id"]),
            "dataset_id": str(dataset_id),
            "item_id": str(item_id) if request["item_id"] else None,
            "deleted_items": request["deleted_items"],
            "deleted_scenes": request["deleted_scenes"],
            "deleted_associations": request["deleted_associations"],
            "discarded_uploads": request["discarded_uploads"],
            "cancelled_jobs": request["cancelled_jobs"],
            "queued_storage": request["queued_storage"],
        }


@pytest.mark.asyncio
async def test_dataset_source_audit_rejects_invalid_counts_actor_and_context(
    db_session, super_admin
):
    actor, _ = super_admin
    request = {
        "operation_id": str(uuid.uuid4()),
        "actor_id": str(actor.id),
        "dataset_id": str(uuid.uuid4()),
        "item_id": None,
        "deleted_items": 0,
        "deleted_scenes": 0,
        "deleted_associations": 0,
        "discarded_uploads": 0,
        "cancelled_jobs": 0,
        "queued_storage": 0,
        "context": json.dumps({"method": "DELETE"}),
    }
    invalid_requests = (
        {**request, "deleted_items": -1},
        {**request, "deleted_scenes": None},
        {**request, "deleted_associations": -1},
        {**request, "discarded_uploads": -1},
        {**request, "cancelled_jobs": -1},
        {**request, "queued_storage": -1},
        {**request, "context": "null"},
        {**request, "context": json.dumps({"ip": "not-an-ip"})},
        {**request, "actor_id": str(uuid.uuid4())},
    )

    for invalid_request in invalid_requests:
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.scalar(_RECORD_SOURCE_DELETION, invalid_request)


def test_dataset_source_support_artifact_and_migration_are_closed():
    api_root = Path(__file__).resolve().parents[1]
    sql_path = api_root / "alembic/sql/0208_dataset_source_support.sql"
    schema_path = api_root / "alembic/sql/0208_dataset_source_support.schema.json"
    migration_path = api_root / "alembic/versions/0208_dataset_source_support.py"
    sql = sql_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    migration = migration_path.read_text(encoding="utf-8")

    assert schema["$id"] == "urn:aap:dataset-source-support:v1"
    assert set(schema["x-postgresql-functions"]) == {
        "aap_jobs_v1.guard_dataset_source_deletion(uuid)",
        "aap_jobs_v1.task_creation_execution_active(uuid, uuid)",
        "aap_audit_v1.record_dataset_source_deletion(uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb)",
    }
    assert schema["x-postgresql-dependencies"] == [
        "aap_jobs_v1._task_creation_job_payload_valid(uuid, jsonb)",
        "aap_audit_v1.valid_request_context(jsonb)",
        "aap_identity_v1.lock_audit_actor(uuid)",
    ]
    assert sql.count("SET search_path = pg_catalog, pg_temp") == 3
    assert sql.count("FROM PUBLIC") == 3
    assert "UPDATE public.async_jobs" not in sql
    assert "DELETE FROM public.async_jobs" not in sql
    assert "p_action" not in sql and "p_event" not in sql
    assert "'dataset.delete'" in sql and "'dataset.item_delete'" in sql
    assert "status_code, ip, request_id, detail_json" in sql

    assert migration.count(" RESTRICT") == 3
