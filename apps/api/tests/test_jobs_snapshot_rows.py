"""Jobs creation and owner-bound execution snapshot rows."""

from __future__ import annotations

from contextlib import contextmanager
import re
import uuid

import pytest
from sqlalchemy import event, inspect as sa_inspect

from app.db.models.async_job import AsyncJob
from app.services import async_job as jobs
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
from app.services.jobs_lifecycle_data import (
    JobsLifecyclePostgresData,
    job_lock_statement,
)
from app.services.prediction_execution import create_snapshot
from tests.factory import create_project, create_user


@contextmanager
def _capture_sql(session):
    statements: list[str] = []
    engine = session.bind.sync_engine

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _assert_no_private_jobs_read(statements: list[str]) -> None:
    assert not any(
        re.search(r"\bfrom\s+(?:public\.)?async_jobs\b", statement.lower())
        for statement in statements
    )


async def _project_world(db):
    user = await create_user(
        db,
        "project_admin",
        f"jobs-snapshot-{uuid.uuid4().hex}@test.local",
        "Jobs Snapshot",
    )
    project = await create_project(db, owner_id=user.id)
    snapshot = await create_snapshot(
        db,
        project.id,
        {
            "stages": [
                {"stage": 0, "parent_stage": None, "ml_backend_id": str(uuid.uuid4())}
            ],
            "prompt": None,
        },
    )
    return user, project, snapshot


async def test_generic_creation_uses_closed_view_without_snapshot_column(db_session):
    user, project, _snapshot = await _project_world(db_session)

    with _capture_sql(db_session) as statements:
        evaluated = await jobs.create_job(
            db_session,
            kind="sample_evaluation",
            user_id=user.id,
            project_id=project.id,
            payload={"evaluation_id": str(uuid.uuid4())},
        )
        ordinary = await jobs.create_job(
            db_session,
            kind="export",
            user_id=user.id,
            payload={},
        )

    inserts = [
        statement
        for statement in statements
        if statement.lstrip().lower().startswith("insert into")
    ]
    assert len(inserts) == 2
    for statement in inserts:
        lowered = statement.lower()
        assert lowered.startswith("insert into aap_jobs_v1.async_job_creation ")
        for absent in (
            "execution_snapshot_id",
            "last_progress_at",
            "progress_observation",
        ):
            assert absent not in lowered

    assert evaluated.kind == "sample_evaluation"
    assert evaluated.status == "pending"
    assert ordinary.kind == "export"
    assert await jobs.execution_snapshot_id(db_session, evaluated.id) is None
    assert await jobs.execution_snapshot_id(db_session, ordinary.id) is None


async def test_owner_binding_materializes_on_read_lock_and_idempotent_rows(db_session):
    user, project, snapshot = await _project_world(db_session)
    key = uuid.uuid4().hex
    job = await jobs.create_job(
        db_session,
        kind="batch_predict",
        user_id=user.id,
        project_id=project.id,
        payload={"data_manager_idempotency_key": key},
    )
    await jobs.bind_execution_snapshot(
        db_session, job, actor_id=user.id, snapshot_id=snapshot.id
    )
    await db_session.flush()

    with _capture_sql(db_session) as statements:
        read = await jobs.get_job(db_session, job.id, populate_existing=True)
    assert read.execution_snapshot_id == snapshot.id
    assert "execution_snapshot_id" not in sa_inspect(read).unloaded
    assert "last_progress_at" not in sa_inspect(read).unloaded
    assert "progress_observation" not in sa_inspect(read).unloaded
    lowered = [statement.lower() for statement in statements]
    assert any("aap_jobs_v1.read_job(" in statement for statement in lowered)
    assert any(
        "aap_jobs_v1.read_job_execution_snapshot(" in statement for statement in lowered
    )
    assert any("aap_jobs_v1.read_job_progress(" in statement for statement in lowered)
    _assert_no_private_jobs_read(statements)

    locked = await db_session.scalar(
        job_lock_statement(job.id, mode="share_nowait", populate_existing=True)
    )
    assert locked.execution_snapshot_id == snapshot.id
    assert "execution_snapshot_id" not in sa_inspect(locked).unloaded
    assert "last_progress_at" not in sa_inspect(locked).unloaded

    found = await JobsDomainQueryPostgresData(db_session).find_action_idempotent_job(
        kind="batch_predict",
        project_id=project.id,
        actor_id=user.id,
        key=key,
    )
    assert found is not None
    assert found.execution_snapshot_id == snapshot.id
    assert "execution_snapshot_id" not in sa_inspect(found).unloaded


async def test_expired_refresh_reads_snapshot_through_the_jobs_owner(db_session):
    user, project, snapshot = await _project_world(db_session)
    job = await jobs.create_job(
        db_session,
        kind="batch_predict",
        user_id=user.id,
        project_id=project.id,
    )
    await jobs.bind_execution_snapshot(
        db_session, job, actor_id=user.id, snapshot_id=snapshot.id
    )
    await db_session.flush()

    db_session.expire(job)
    await db_session.refresh(job)

    assert job.execution_snapshot_id == snapshot.id
    assert job.last_progress_at is None
    assert job.progress_observation is None
    assert "execution_snapshot_id" not in sa_inspect(job).unloaded
    assert "last_progress_at" not in sa_inspect(job).unloaded
    assert "progress_observation" not in sa_inspect(job).unloaded


async def test_generic_insert_and_update_cannot_write_the_snapshot_binding(db_session):
    user, project, snapshot = await _project_world(db_session)

    with pytest.raises(ValueError, match="bind_prediction_execution_snapshot"):
        await JobsLifecyclePostgresData(db_session).create_job(
            kind="export", execution_snapshot_id=snapshot.id
        )

    insert_savepoint = await db_session.begin_nested()
    pending = AsyncJob(kind="export", execution_snapshot_id=snapshot.id)
    db_session.add(pending)
    with pytest.raises(ValueError, match="bind_prediction_execution_snapshot"):
        await db_session.flush()
    await insert_savepoint.rollback()

    job = await jobs.create_job(
        db_session,
        kind="export",
        user_id=user.id,
        project_id=project.id,
    )
    await db_session.flush()
    # A savepoint rollback expires the row it dirtied, and touching an expired
    # attribute outside greenlet_spawn cannot refresh it. The invariant is about
    # the stored binding, so keep the identity as a plain UUID.
    job_id = job.id

    update_savepoint = await db_session.begin_nested()
    loaded = await jobs.get_job(db_session, job_id, populate_existing=True)
    loaded.execution_snapshot_id = snapshot.id
    with pytest.raises(ValueError, match="bind_prediction_execution_snapshot"):
        await db_session.flush()
    await update_savepoint.rollback()

    assert await jobs.execution_snapshot_id(db_session, job_id) is None


async def test_owner_binding_rejects_an_unrelated_actor(db_session):
    user, project, snapshot = await _project_world(db_session)
    other = await create_user(
        db_session,
        "project_admin",
        f"jobs-snapshot-other-{uuid.uuid4().hex}@test.local",
        "Other Jobs Snapshot",
    )
    job = await jobs.create_job(
        db_session,
        kind="batch_predict",
        user_id=user.id,
        project_id=project.id,
    )

    with pytest.raises(ValueError, match="binding rejected"):
        await jobs.bind_execution_snapshot(
            db_session, job, actor_id=other.id, snapshot_id=snapshot.id
        )

    assert await jobs.execution_snapshot_id(db_session, job.id) is None
