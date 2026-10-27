"""Dataset unlink uses exact-generation Jobs cancellation and fixed Audit records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.exc import DBAPIError

from app.db.models.async_job import AsyncJob
from tests.factory import create_project, create_user

_CANCEL_DATASET_TASK_CREATION = text(
    "SELECT aap_jobs_v1.cancel_dataset_task_creation("
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:association_id AS uuid))"
)
_VALID_CONTEXT = text(
    "SELECT aap_audit_v1.valid_request_context(CAST(:context AS jsonb))"
)
_SQL_NULL_CONTEXT = text("SELECT aap_audit_v1.valid_request_context(NULL::jsonb)")
_RECORD_DATASET_UNLINK = text(
    "SELECT aap_audit_v1.record_dataset_unlink("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:deleted_tasks AS bigint), CAST(:deleted_annotations AS bigint), "
    "CAST(:batch_ids AS uuid[]), CAST(:context AS jsonb))"
).bindparams(bindparam("batch_ids", type_=ARRAY(UUID(as_uuid=True))))
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


def _creation_job(
    *,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID | None,
    payload_project_id: uuid.UUID,
    association_id: uuid.UUID,
    status: str = "pending",
    kind: str = "create_tasks",
) -> AsyncJob:
    job_id = uuid.uuid4()
    return AsyncJob(
        id=job_id,
        kind=kind,
        project_id=project_id,
        user_id=actor_id,
        status=status,
        progress_pct=37,
        payload=_creation_payload(
            job_id,
            actor_id,
            dataset_id,
            payload_project_id,
            association_id,
        ),
        result={"retained": str(job_id)},
    )


@pytest.mark.asyncio
async def test_dataset_task_creation_cancellation_matches_authoritative_generation(
    db_session, super_admin
):
    actor, _ = super_admin
    other_actor = await create_user(
        db_session,
        "employee",
        f"unlink-jobs-other-{uuid.uuid4()}@test.local",
        "Other actor",
    )
    project = await create_project(db_session, owner_id=actor.id)
    other_project = await create_project(db_session, owner_id=actor.id)
    dataset_id = uuid.uuid4()
    association_id = uuid.uuid4()
    other_association_id = uuid.uuid4()

    pending = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
        status="pending",
    )
    running = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
        status="running",
    )
    different_association = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=other_association_id,
    )
    different_dataset = _creation_job(
        actor_id=actor.id,
        dataset_id=uuid.uuid4(),
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
    )
    forged_payload_scope = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=other_project.id,
        association_id=association_id,
    )
    other_authoritative_scope = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=other_project.id,
        payload_project_id=other_project.id,
        association_id=association_id,
    )
    mismatched_actor = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
    )
    mismatched_actor.user_id = other_actor.id
    malformed = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
    )
    malformed.payload["unexpected"] = "not part of frozen v1"
    completed = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
        status="completed",
    )
    failed = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
        status="failed",
    )
    cancelled = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
        status="cancelled",
    )
    terminal_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    completed.completed_at = terminal_at
    failed.completed_at = terminal_at
    cancelled.completed_at = terminal_at
    wrong_kind = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
        kind="dataset_import",
    )
    unresolved = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=None,
        payload_project_id=project.id,
        association_id=association_id,
    )
    detached = _creation_job(
        actor_id=actor.id,
        dataset_id=dataset_id,
        project_id=project.id,
        payload_project_id=project.id,
        association_id=association_id,
    )
    db_session.add_all(
        [
            pending,
            running,
            different_association,
            different_dataset,
            forged_payload_scope,
            other_authoritative_scope,
            mismatched_actor,
            malformed,
            completed,
            failed,
            cancelled,
            wrong_kind,
            unresolved,
            detached,
        ]
    )
    await db_session.flush()
    await db_session.execute(
        text("UPDATE public.async_jobs SET project_id = NULL WHERE id = :job_id"),
        {"job_id": detached.id},
    )
    await db_session.refresh(detached)
    assert detached.retained_project_id == project.id

    count = await db_session.scalar(
        _CANCEL_DATASET_TASK_CREATION,
        {
            "dataset_id": str(dataset_id),
            "project_id": str(project.id),
            "association_id": str(association_id),
        },
    )
    assert count == 2

    for job in (pending, running):
        await db_session.refresh(job)
        assert job.status == "cancelled"
        assert job.progress_pct == 37
        assert job.result == {"retained": str(job.id)}
        assert job.completed_at is not None

    terminal_ids = {completed.id, failed.id, cancelled.id}
    for job, original_status in (
        (different_association, "pending"),
        (different_dataset, "pending"),
        (forged_payload_scope, "pending"),
        (other_authoritative_scope, "pending"),
        (mismatched_actor, "pending"),
        (malformed, "pending"),
        (completed, "completed"),
        (failed, "failed"),
        (cancelled, "cancelled"),
        (wrong_kind, "pending"),
        (unresolved, "pending"),
        (detached, "pending"),
    ):
        await db_session.refresh(job)
        assert job.status == original_status
        assert job.progress_pct == 37
        assert job.result == {"retained": str(job.id)}
        if job.id in terminal_ids:
            assert job.completed_at == terminal_at


@pytest.mark.asyncio
async def test_dataset_unlink_audit_context_and_fixed_metadata(db_session, super_admin):
    actor, _ = super_admin
    dataset_id = uuid.uuid4()
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    batch_ids = [uuid.uuid4(), uuid.uuid4()]
    context = {
        "method": "DELETE",
        "path": f"/api/v1/datasets/{dataset_id}/link/{project_id}",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }

    assert await db_session.scalar(_SQL_NULL_CONTEXT) is True
    for valid in (
        {},
        context,
        {"method": None, "path": None, "ip": None, "request_id": None},
    ):
        assert (
            await db_session.scalar(_VALID_CONTEXT, {"context": json.dumps(valid)})
            is True
        )
    invalid_contexts = (
        None,  # JSON null, distinct from SQL NULL.
        [],
        {"other": "value"},
        {"method": 7},
        {"method": "D" * 9},
        {"path": "p" * 257},
        {"request_id": "r" * 37},
        {"ip": "i" * 65},
        {"ip": "not an address"},
    )
    for invalid in invalid_contexts:
        assert (
            await db_session.scalar(_VALID_CONTEXT, {"context": json.dumps(invalid)})
            is False
        )

    audit_id = await db_session.scalar(
        _RECORD_DATASET_UNLINK,
        {
            "operation_id": str(operation_id),
            "actor_id": str(actor.id),
            "dataset_id": str(dataset_id),
            "project_id": str(project_id),
            "deleted_tasks": 12,
            "deleted_annotations": 34,
            "batch_ids": batch_ids,
            "context": json.dumps(context),
        },
    )
    row = (await db_session.execute(_READ_AUDIT, {"audit_id": audit_id})).one()
    assert row.actor_id == actor.id
    assert row.actor_email == actor.email
    assert row.actor_role == "super_admin"
    assert (row.action, row.target_type, row.target_id) == (
        "dataset.unlink",
        "dataset",
        str(dataset_id),
    )
    assert (row.method, row.path, row.status_code, row.request_id) == (
        "DELETE",
        context["path"],
        200,
        context["request_id"],
    )
    assert row.ip.startswith("127.0.0.1")
    assert row.detail_json == {
        "operation_id": str(operation_id),
        "project_id": str(project_id),
        "deleted_tasks": 12,
        "deleted_annotations": 34,
        "deleted_batches": 2,
        "deleted_batch_ids": [str(item_id) for item_id in batch_ids],
        "soft": False,
    }


@pytest.mark.asyncio
async def test_dataset_unlink_audit_rejects_invalid_metadata_and_actor(
    db_session, super_admin
):
    actor, _ = super_admin
    valid = {
        "operation_id": str(uuid.uuid4()),
        "actor_id": str(actor.id),
        "dataset_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "deleted_tasks": 0,
        "deleted_annotations": 0,
        "batch_ids": [],
        "context": json.dumps({"method": "DELETE"}),
    }
    invalid_requests = (
        {**valid, "deleted_tasks": -1},
        {**valid, "deleted_tasks": None},
        {**valid, "deleted_annotations": -1},
        {**valid, "batch_ids": None},
        {**valid, "batch_ids": [uuid.uuid4(), None]},
        {**valid, "context": json.dumps({"untrusted": True})},
        {**valid, "actor_id": str(uuid.uuid4())},
    )
    for request in invalid_requests:
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.scalar(_RECORD_DATASET_UNLINK, request)


def test_dataset_unlink_jobs_audit_artifact_and_migration_are_closed():
    api_root = Path(__file__).resolve().parents[1]
    sql_path = api_root / "alembic/sql/0204_dataset_unlink_jobs_audit.sql"
    schema_path = api_root / "alembic/sql/0204_dataset_unlink_jobs_audit.schema.json"
    migration_path = api_root / "alembic/versions/0204_dataset_unlink_jobs_audit.py"
    sql = sql_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    migration = migration_path.read_text(encoding="utf-8")

    assert schema["$id"] == "urn:aap:contract:dataset-unlink-jobs-audit:v1"
    assert schema["x-postgresql-dependencies"] == [
        "aap_jobs_v1._task_creation_job_payload_valid(uuid, jsonb)",
        "aap_identity_v1.lock_audit_actor(uuid)",
    ]
    assert schema["x-audit-log-entry"] == {
        "action": "dataset.unlink",
        "target_type": "dataset",
        "status_code": 200,
        "detail": {"$ref": "#/$defs/datasetUnlinkAuditDetail"},
    }
    assert set(schema["x-postgresql-functions"]) == {
        "aap_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid)",
        "aap_audit_v1.valid_request_context(jsonb)",
        "aap_audit_v1.record_dataset_unlink(uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb)",
    }
    assert sql.count("SET search_path = pg_catalog, pg_temp") == 3
    assert sql.count("FROM PUBLIC") == 3
    assert "'dataset.unlink', 'dataset', p_dataset_id::text" in sql
    assert "p_deleted_batch_ids" in sql
    assert "array_ndims(p_deleted_batch_ids) IS DISTINCT FROM 1" in sql
    assert "p_event" not in sql and "p_action" not in sql

    assert migration.count(" RESTRICT") == 3
