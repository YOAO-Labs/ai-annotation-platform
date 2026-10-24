"""Native PostgreSQL checks for the borrowed Jobs lifecycle adapter."""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import event, func, inspect as sa_inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.jobs_lifecycle import _TRACE
from app.db.models.async_job import AsyncJob
from app.services import async_job as async_job_svc


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned strict Jobs lifecycle LOGIN",
)
async def test_independent_node_consumes_lifecycle_capabilities(
    test_engine, record_property
):
    _jobs_lifecycle_url(test_engine)
    node = shutil.which("node")
    assert node is not None
    root = Path(__file__).resolve().parents[3]
    result = await asyncio.to_thread(
        subprocess.run,
        [
            node,
            str(root / "apps/jobs/conformance/node/lifecycle_acceptance.mjs"),
            "--live",
        ],
        cwd=root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["status"] == "passed"
    assert receipt["functionCapabilities"] == 8
    assert receipt["rowColumns"] == 15
    record_property("jobs_lifecycle_node_consumer", json.dumps(receipt))


def _job(**overrides) -> AsyncJob:
    values = {
        "id": uuid.uuid4(),
        "kind": "audit_archive",
        "project_id": None,
        "retained_project_id": None,
        "user_id": None,
        "status": "pending",
        "progress_pct": 0,
        "payload": {},
        "result": {},
    }
    values.update(overrides)
    return AsyncJob(**values)


def _sqlstate(error: BaseException) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if value:
            return value
        current = getattr(current, "orig", None) or current.__cause__
    return None


def _jobs_lifecycle_url(test_engine):
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_JOBS_LIFECYCLE_URL"])
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.database and url.database.endswith("_test")
    assert url.username == "jobs_lifecycle_client"
    return url.set(drivername="postgresql+asyncpg")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned database and strict Jobs lifecycle LOGIN",
)
async def test_strict_login_creates_scoped_job_and_rejects_private_legacy_writes(
    test_engine,
):
    from tests.factory import create_project, create_user

    url = _jobs_lifecycle_url(test_engine)
    strict_engine = create_async_engine(url, hide_parameters=True)
    strict_sessions = async_sessionmaker(strict_engine, expire_on_commit=False)
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with owner_sessions() as setup:
            suffix = uuid.uuid4().hex
            actor = await create_user(
                setup,
                "project_admin",
                f"jobs-lifecycle-{suffix}@test.local",
                "Jobs lifecycle actor",
            )
            project = await create_project(setup, owner_id=actor.id)
            await setup.commit()
            actor_id, project_id = actor.id, project.id

        async with strict_sessions() as strict:
            identity = (
                await strict.execute(
                    text(
                        "SELECT session_user::text, current_user::text, "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'public.async_jobs', 'SELECT'), "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'public.async_jobs', 'UPDATE')"
                    )
                )
            ).one()
            assert identity[0] == identity[1] == "jobs_lifecycle_client"
            assert identity[2:] == (False, False)

            created = await async_job_svc.create_job(
                strict,
                kind="audit_archive",
                user_id=actor_id,
                project_id=project_id,
            )
            created_id = created.id
            visible = await async_job_svc.get_job(
                strict, created_id, populate_existing=True
            )
            assert visible is created
            assert visible.status == "pending"
            assert visible.project_id == project_id
            assert visible.retained_project_id is None
            await async_job_svc.update_progress(strict, created_id, 7)
            await strict.flush()
            assert "updated_at" in sa_inspect(visible).expired_attributes
            await strict.refresh(visible, attribute_names=["updated_at"])
            assert visible.updated_at is not None
            assert "updated_at" not in sa_inspect(visible).expired_attributes
            await strict.commit()

        async with owner_sessions() as owner:
            job = await async_job_svc.get_job(owner, created_id, populate_existing=True)
            await async_job_svc.mark_running(owner, created_id)
            job.status = "completed"
            await owner.flush()
            assert job.status == "completed"
            assert job.started_at is not None
            await owner.commit()

        async with owner_sessions() as owner:
            persisted = await async_job_svc.get_job(
                owner, created_id, populate_existing=True
            )
            original_updated_at = persisted.updated_at
            assert persisted.status == "completed"

        async with strict_sessions() as strict:
            job = await async_job_svc.get_job(
                strict, created_id, populate_existing=True
            )
            job.status = "failed"
            with pytest.raises(DBAPIError) as denied_status:
                await strict.flush()
            assert _sqlstate(denied_status.value) == "42501"
            await strict.rollback()

        async with strict_sessions() as strict:
            job = await async_job_svc.get_job(
                strict, created_id, populate_existing=True
            )
            job.updated_at = datetime(2001, 1, 1, tzinfo=timezone.utc)
            with pytest.raises(DBAPIError) as denied_timestamp:
                await strict.flush()
            assert _sqlstate(denied_timestamp.value) == "42501"
            await strict.rollback()

        async with owner_sessions() as owner:
            persisted = await async_job_svc.get_job(
                owner, created_id, populate_existing=True
            )
            assert persisted.status == "completed"
            assert persisted.updated_at == original_updated_at
    finally:
        await strict_engine.dispose()


@pytest.mark.asyncio
async def test_lifecycle_roundtrips_native_jsonb_result_values(db_session):
    job = await async_job_svc.create_job(db_session, kind="audit_archive")
    large_integer = 2**70
    values = [
        None,
        False,
        [None, False, large_integer],
        {"nested": {"count": large_integer, "enabled": False}},
    ]

    for expected in values:
        await async_job_svc.replace_result(db_session, job.id, result=expected)
        await db_session.flush()
        actual = await async_job_svc.get_job(db_session, job.id, populate_existing=True)
        assert actual.result == expected
        if expected is False:
            assert actual.result is False
        if isinstance(expected, list):
            assert type(actual.result[2]) is int
            assert actual.result[2] == large_integer
        if isinstance(expected, dict):
            assert type(actual.result["nested"]["count"]) is int
            assert actual.result["nested"]["count"] == large_integer


@pytest.mark.asyncio
async def test_lifecycle_batches_commands_per_row_and_keeps_partial_flush_order(
    db_session,
):
    rows = sorted((_job(), _job()), key=lambda row: row.id.int)
    first, second = rows
    db_session.add_all(rows)
    await db_session.flush()

    unflushed = _job()
    db_session.add(unflushed)
    calls = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "aap_jobs_v1.apply_lifecycle(" not in statement.lower():
            return
        if isinstance(parameters, Mapping):
            job_id, request = parameters["job_id"], parameters["request"]
        else:
            job_id, request = tuple(parameters)
        calls.append((uuid.UUID(str(job_id)), json.loads(request)))

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await async_job_svc.update_progress(
            db_session,
            first.id,
            41,
            extra_payload={"phase": "first"},
            result={"stored": True},
        )
        await async_job_svc.update_payload(db_session, first.id, {"later": 2})
        await async_job_svc.update_progress(db_session, second.id, 77)

        await db_session.flush([first, second])
        assert [job_id for job_id, _ in calls] == [first.id, second.id]
        first_request, second_request = [request for _, request in calls]
        assert [command["operation"] for command in first_request["commands"]] == [
            "update_progress",
            "update_payload",
        ]
        assert {"progress_pct", "payload", "result"} <= set(
            first_request["changed_fields"]
        )
        assert "status" not in first_request["changed_fields"]
        assert second_request["changed_fields"] == ["progress_pct"]
        assert sa_inspect(unflushed).pending

        with db_session.no_autoflush:
            count = await db_session.scalar(
                select(func.count())
                .select_from(AsyncJob)
                .where(AsyncJob.id == unflushed.id)
                .execution_options(autoflush=False)
            )
        assert count == 0

        await db_session.flush()
        assert len(calls) == 2
        first_row = await async_job_svc.get_job(
            db_session, first.id, populate_existing=True
        )
        second_row = await async_job_svc.get_job(
            db_session, second.id, populate_existing=True
        )
        assert first_row.payload == {"phase": "first", "later": 2}
        assert first_row.result == {"stored": True}
        assert second_row.progress_pct == 77
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_lifecycle_default_timestamp_expires_but_explicit_override_is_kept(
    db_session,
):
    job = await async_job_svc.create_job(db_session, kind="audit_archive")
    await db_session.refresh(job, attribute_names=["updated_at"])

    await async_job_svc.update_progress(db_session, job.id, 10)
    await db_session.flush()
    assert "updated_at" in sa_inspect(job).expired_attributes
    await db_session.refresh(job, attribute_names=["updated_at"])
    assert job.updated_at is not None

    explicit = datetime(2001, 2, 3, 4, 5, tzinfo=timezone.utc)
    job.updated_at = explicit
    await async_job_svc.update_progress(db_session, job.id, 11)
    await db_session.flush()
    assert "updated_at" not in sa_inspect(job).expired_attributes
    assert job.updated_at == explicit


@pytest.mark.asyncio
async def test_dispatch_failure_preserves_claimed_state_cache_and_no_autoflush(
    db_session,
):
    claimed = await async_job_svc.create_job(db_session, kind="audit_archive")
    dispatch_error = await async_job_svc.create_job(db_session, kind="audit_archive")
    rollback_job = await async_job_svc.create_job(db_session, kind="audit_archive")
    legacy = await async_job_svc.create_job(db_session, kind="audit_archive")
    await db_session.scalar(
        update(AsyncJob)
        .where(AsyncJob.id == legacy.id, AsyncJob.status == "pending")
        .values(status="failed", error_message="x" * 4000)
        .returning(AsyncJob.id)
        .execution_options(synchronize_session="fetch")
    )

    await db_session.execute(
        update(AsyncJob)
        .where(AsyncJob.id == claimed.id)
        .values(status="running", result={"worker_claimed": True})
        .execution_options(synchronize_session=False)
    )
    assert claimed.status == "pending"
    assert (
        await async_job_svc.mark_dispatch_failed(
            db_session, claimed.id, error="ambiguous publish result"
        )
        is False
    )
    assert claimed.status == "pending"
    fresh_claimed = await async_job_svc.get_job(
        db_session, claimed.id, populate_existing=True
    )
    assert fresh_claimed.status == "running"
    assert fresh_claimed.result == {"worker_claimed": True}

    assert await async_job_svc.mark_dispatch_failed(
        db_session, dispatch_error.id, error="x" * 4001
    )
    assert dispatch_error.status == "failed"
    # Fetch synchronization leaves an unloaded nullable attribute unloaded.
    assert dispatch_error.error_message == legacy.error_message
    fresh_failure = await async_job_svc.get_job(
        db_session, dispatch_error.id, populate_existing=True
    )
    assert fresh_failure.error_message == "x" * 4000

    rollback_job_id = rollback_job.id
    savepoint = await db_session.begin_nested()
    assert await async_job_svc.mark_dispatch_failed(
        db_session, rollback_job_id, error="rolled back"
    )
    assert rollback_job.status == "failed"
    await savepoint.rollback()
    restored = await async_job_svc.get_job(
        db_session, rollback_job_id, populate_existing=True
    )
    assert restored.status == "pending"

    pending = _job()
    db_session.add(pending)
    with db_session.no_autoflush:
        assert not await async_job_svc.mark_dispatch_failed(
            db_session, pending.id, error="not yet inserted"
        )
        count = await db_session.scalar(
            select(func.count())
            .select_from(AsyncJob)
            .where(AsyncJob.id == pending.id)
            .execution_options(autoflush=False)
        )
    assert count == 0
    assert sa_inspect(pending).pending
    await db_session.flush()
    inserted = await async_job_svc.get_job(
        db_session, pending.id, populate_existing=True
    )
    assert inserted.status == "pending"


@pytest.mark.asyncio
async def test_partial_progress_error_and_rollback_clear_lifecycle_trace(
    db_session, test_engine
):
    job = await async_job_svc.create_job(db_session, kind="audit_archive")
    with pytest.raises(TypeError):
        await async_job_svc.update_progress(db_session, job.id, object())
    assert _TRACE not in sa_inspect(job).info

    with pytest.raises(ValueError):
        await async_job_svc.update_progress(
            db_session,
            job.id,
            "not-an-integer",
            result={"written_before_error": True},
        )
    trace = sa_inspect(job).info[_TRACE]
    assert trace[-1]["operation"] == "update_progress"
    assert trace[-1]["written_fields"] == ["result"]
    await db_session.flush()
    persisted = await async_job_svc.get_job(db_session, job.id, populate_existing=True)
    assert persisted.result == {"written_before_error": True}
    assert persisted.progress_pct == 0
    assert _TRACE not in sa_inspect(job).info

    with pytest.raises(TypeError):
        await async_job_svc.update_progress(
            db_session,
            job.id,
            object(),
            result={"prefix_survives": True},
            extra_payload=object(),
        )
    await db_session.flush()
    persisted = await async_job_svc.get_job(db_session, job.id, populate_existing=True)
    assert persisted.result == {"prefix_survives": True}
    await async_job_svc.update_progress(db_session, job.id, " １２ ")
    await db_session.flush()
    assert job.progress_pct == 12

    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner_sessions() as setup:
        rollback_job = await async_job_svc.create_job(setup, kind="audit_archive")
        rollback_id = rollback_job.id
        await setup.commit()

    rollback_job = await async_job_svc.get_job(
        db_session, rollback_id, populate_existing=True
    )
    await db_session.begin_nested()
    await async_job_svc.update_progress(db_session, rollback_id, 19)
    rollback_job.retained_project_id = "not-a-uuid"
    assert _TRACE in sa_inspect(rollback_job).info
    with pytest.raises(DBAPIError):
        await db_session.flush()
    assert _TRACE not in sa_inspect(rollback_job).info
    await db_session.rollback()

    restored = await async_job_svc.get_job(
        db_session, rollback_id, populate_existing=True
    )
    assert restored.retained_project_id is None
    assert restored.progress_pct == 0


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned strict Jobs lifecycle LOGIN",
)
async def test_expired_refresh_keeps_concurrent_timestamp_and_partial_dirty_cache(
    test_engine,
):
    strict_engine = create_async_engine(_jobs_lifecycle_url(test_engine))
    strict_sessions = async_sessionmaker(strict_engine, expire_on_commit=False)
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    statements = []
    job_id = uuid.uuid4()

    def capture(_connection, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    event.listen(strict_engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with owner_sessions() as setup:
            setup.add(_job(id=job_id))
            await setup.commit()
        async with strict_sessions() as reader:
            job = await async_job_svc.get_job(reader, job_id)
            assert job is not None and job.progress_pct == 0
            original_time = job.updated_at
            job.payload = {"local": "still dirty"}
            reader.expire(job, ["updated_at"])
            assert "updated_at" in sa_inspect(job).expired_attributes
            changed_time = datetime(2040, 1, 1, tzinfo=timezone.utc)
            async with owner_sessions() as change:
                await change.execute(
                    text(
                        "UPDATE public.async_jobs SET updated_at=:value, "
                        "progress_pct=71 WHERE id=:job_id"
                    ),
                    {"value": changed_time, "job_id": job_id},
                )
                await change.commit()
            with reader.no_autoflush:
                await reader.refresh(job, attribute_names=["updated_at"])
            assert job.updated_at == changed_time and job.updated_at != original_time
            assert job.progress_pct == 0
            assert job.payload == {"local": "still dirty"}
            assert sa_inspect(job).attrs.payload.history.has_changes()
            async with owner_sessions() as check:
                stored = await async_job_svc.get_job(check, job_id)
                assert stored is not None and stored.payload == {}
            await reader.rollback()
            assert sa_inspect(job).expired
            await reader.refresh(job)
            assert job.updated_at == changed_time and job.progress_pct == 71
            assert job.payload == {}
            async with owner_sessions() as delete_row:
                await delete_row.execute(
                    text("DELETE FROM public.async_jobs WHERE id=:job_id"),
                    {"job_id": job_id},
                )
                await delete_row.commit()
            reader.expire(job)
            with pytest.raises(InvalidRequestError):
                await reader.refresh(job)
            assert job in reader and sa_inspect(job).expired
            assert await async_job_svc.get_job(reader, job_id) is None
            assert job not in reader
        assert any("aap_jobs_v1.read_job(" in item for item in statements)
        assert not any(
            "from async_jobs" in item or "from public.async_jobs" in item
            for item in statements
        )
    finally:
        event.remove(strict_engine.sync_engine, "before_cursor_execute", capture)
        await strict_engine.dispose()
        async with owner_sessions() as cleanup:
            await cleanup.execute(
                text("DELETE FROM public.async_jobs WHERE id=:job_id"),
                {"job_id": job_id},
            )
            await cleanup.commit()
