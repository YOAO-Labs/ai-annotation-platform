"""Native PostgreSQL acceptance for fixed Jobs fixture outcome updates."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import delete, inspect as sa_inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.services import async_job as jobs

ROOT = Path(__file__).resolve().parents[3]
_ROLE = "identity_api_consumer"
_FUNCTION = "aap_jobs_v1.set_filter_fixture_job_outcomes"
_CALL = (
    "SELECT aap_jobs_v1.set_filter_fixture_job_outcomes("
    "CAST(:completed AS pg_catalog.uuid), CAST(:failed AS pg_catalog.uuid))"
)
_SCHEMA = ROOT / "apps/jobs/src/aap_jobs/fixture_filter.schema.json"
_SQL = ROOT / "apps/jobs/src/aap_jobs/sql/0331_jobs_filter_fixtures.sql"
_API_SCHEMA = ROOT / "apps/api/alembic/sql/0331_jobs_filter_fixtures.schema.json"
_API_SQL = ROOT / "apps/api/alembic/sql/0331_jobs_filter_fixtures.sql"
_NODE = ROOT / "apps/jobs/conformance/node/filter_fixture_acceptance.mjs"
_OLD_TIMESTAMP = datetime(2001, 1, 1, tzinfo=timezone.utc)
_FAILED_RESULT = {
    "keep": "failed result",
    "large_integer": 9007199254740993,
    "json_null": None,
    "json_false": False,
}

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned module-data PostgreSQL fixture",
    ),
]


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


def _new_pair() -> tuple[AsyncJob, AsyncJob]:
    low, high = sorted((uuid.uuid4(), uuid.uuid4()), key=lambda value: value.int)
    return (
        AsyncJob(
            id=high,
            kind="batch_predict",
            status="pending",
            progress_pct=0,
            payload={"fixture": "completed", "preserve": True},
            result={},
            created_at=_OLD_TIMESTAMP,
        ),
        AsyncJob(
            id=low,
            kind="prediction_retry",
            status="pending",
            progress_pct=0,
            payload={"fixture": "failed", "preserve": True},
            result={},
            created_at=_OLD_TIMESTAMP,
        ),
    )


async def _read_job(session, job_id):
    job = await jobs.get_job(session, job_id, populate_existing=True)
    assert job is not None
    return job


def _assert_initial(job: AsyncJob, expected_result: dict) -> None:
    assert job.status == "pending"
    assert job.progress_pct == 0
    assert job.result == expected_result
    assert job.error_message is None
    assert job.started_at is None and job.completed_at is None
    assert job.updated_at == _OLD_TIMESTAMP


def _assert_outcomes(completed: AsyncJob, failed: AsyncJob) -> None:
    assert completed.status == "completed"
    assert completed.progress_pct == 100
    assert completed.result == {"success_count": 2}
    assert completed.error_message is None
    assert completed.started_at is None and completed.completed_at is None
    assert completed.payload == {"fixture": "completed", "preserve": True}
    assert completed.updated_at > _OLD_TIMESTAMP

    assert failed.status == "failed"
    assert failed.progress_pct == 40
    assert failed.error_message == "fixture failure"
    assert failed.result == _FAILED_RESULT
    assert failed.started_at is None and failed.completed_at is None
    assert failed.payload == {"fixture": "failed", "preserve": True}
    assert failed.updated_at > _OLD_TIMESTAMP


async def _assert_contract(test_engine, runtime) -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    assert json.loads(_API_SCHEMA.read_text(encoding="utf-8")) == schema
    sql = _SQL.read_text(encoding="utf-8")
    assert _API_SQL.read_text(encoding="utf-8") == sql
    assert schema["$id"] == "aap-jobs-filter-fixtures/v1"
    function = schema["x-postgresql-functions"][_FUNCTION]
    assert function == {
        "arguments": "uuid, uuid",
        "returns": "bigint",
        "volatility": "volatile",
        "parallel": "unsafe",
    }
    assert schema["$defs"]["FilterFixtureJobsCount"] == {
        "maximum": 2,
        "minimum": 2,
        "type": "integer",
    }
    assert schema["x-transactions"]["ordering"].startswith("ascending UUID")
    assert "ORDER BY item.id" in sql

    async with test_engine.connect() as owner:
        catalog = (
            await owner.execute(
                text(
                    "SELECT pg_catalog.oidvectortypes(p.proargtypes), "
                    "pg_catalog.pg_get_function_result(p.oid),p.provolatile::text, "
                    "p.proparallel::text,p.prosecdef,p.proconfig, "
                    "pg_catalog.pg_get_userbyid(p.proowner), "
                    "pg_catalog.has_function_privilege(:role,p.oid,'EXECUTE'), "
                    "EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl, "
                    "pg_catalog.acldefault('f',p.proowner))) acl "
                    "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') "
                    "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n "
                    "ON n.oid=p.pronamespace WHERE n.nspname='aap_jobs_v1' "
                    "AND p.proname='set_filter_fixture_job_outcomes'"
                ),
                {"role": _ROLE},
            )
        ).one()
    assert tuple(catalog) == (
        "uuid, uuid",
        "bigint",
        "v",
        "u",
        True,
        ["search_path=pg_catalog, pg_temp", "TimeZone=UTC", "DateStyle=ISO, YMD"],
        "jobs_code",
        True,
        False,
    )

    async with runtime.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == _ROLE
        assert await connection.scalar(text("SELECT session_user")) == _ROLE
        privilege = (
            await connection.execute(
                text(
                    "SELECT pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT'), "
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','INSERT'), "
                    "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','INSERT'), "
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE'), "
                    "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','UPDATE'), "
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','DELETE'), "
                    "pg_catalog.has_table_privilege(current_user,'aap_jobs_v1.async_job_creation','SELECT'), "
                    "pg_catalog.has_table_privilege(current_user,'aap_jobs_v1.async_job_creation','INSERT'), "
                    "pg_catalog.has_function_privilege(current_user,:function,'EXECUTE')"
                ),
                {"function": f"{_FUNCTION}(uuid,uuid)"},
            )
        ).one()
        assert tuple(privilege) == (
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
        )


async def _call(session, completed_id, failed_id):
    return await session.scalar(
        text(_CALL), {"completed": completed_id, "failed": failed_id}
    )


async def _assert_failure(session, completed_id, failed_id, sqlstate):
    savepoint = await session.begin_nested()
    try:
        with pytest.raises(DBAPIError) as failure:
            await _call(session, completed_id, failed_id)
        assert _sqlstate(failure.value) == sqlstate
    finally:
        await savepoint.rollback()


async def _assert_private_update_denied(runtime, statement, job_id):
    async with runtime.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(DBAPIError) as denied:
                await connection.execute(text(statement), {"id": job_id})
            assert _sqlstate(denied.value) == "42501"
        finally:
            await transaction.rollback()


async def _run_node_acceptance(test_engine, completed_id, failed_id):
    node = shutil.which("node")
    assert node is not None
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"]).set(
        drivername="postgresql"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "JOBS_FILTER_FIXTURE_DATABASE_URL": url.render_as_string(
                hide_password=False
            ),
            "JOBS_FILTER_FIXTURE_EXPECTED_ROLE": _ROLE,
            "JOBS_FILTER_FIXTURE_EXPECTED_DATABASE": test_engine.url.database,
            "JOBS_FILTER_FIXTURE_EXPECTED_HOST": test_engine.url.host,
            "JOBS_FILTER_FIXTURE_EXPECTED_PORT": str(test_engine.url.port or 5432),
            "JOBS_FILTER_FIXTURE_COMPLETED_JOB_ID": str(completed_id),
            "JOBS_FILTER_FIXTURE_FAILED_JOB_ID": str(failed_id),
        }
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [node, str(_NODE), "--live"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["status"] == "passed"
    assert receipt["function"] == _FUNCTION
    assert receipt["nativeBigint"] == "2"
    return receipt


async def test_filter_fixture_updates_through_real_login_and_node_contract(
    test_engine, record_property
):
    from tests.test_jobs_creation_runtime_native import _lan_runtime

    completed, failed = _new_pair()
    lifecycle_id = None
    runtime = await _lan_runtime(test_engine)
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    api_sessions = async_sessionmaker(runtime, expire_on_commit=False)
    ids = [completed.id, failed.id]
    try:
        await _assert_contract(test_engine, runtime)
        async with owner_sessions() as setup:
            setup.add_all([completed, failed])
            await setup.flush()
            completed.updated_at = _OLD_TIMESTAMP
            failed.result = _FAILED_RESULT
            failed.updated_at = _OLD_TIMESTAMP
            await setup.commit()

        async with api_sessions() as api:
            cached_completed = await _read_job(api, completed.id)
            cached_failed = await _read_job(api, failed.id)
            _assert_initial(cached_completed, {})
            _assert_initial(cached_failed, _FAILED_RESULT)

            await _assert_failure(api, None, failed.id, "22023")
            await _assert_failure(api, completed.id, None, "22023")
            await _assert_failure(api, completed.id, completed.id, "22023")
            missing_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
            await _assert_failure(api, completed.id, missing_id, "P0002")
            _assert_initial(await _read_job(api, completed.id), {})
            _assert_initial(await _read_job(api, failed.id), _FAILED_RESULT)

            count = await _call(api, completed.id, failed.id)
            assert type(count) is int and count == 2
            assert cached_completed.status == "pending"
            refreshed_completed = await _read_job(api, completed.id)
            refreshed_failed = await _read_job(api, failed.id)
            assert refreshed_completed is cached_completed
            assert refreshed_failed is cached_failed
            _assert_outcomes(refreshed_completed, refreshed_failed)
            await api.rollback()
            _assert_initial(await _read_job(api, completed.id), {})
            _assert_initial(await _read_job(api, failed.id), _FAILED_RESULT)
            await api.rollback()

            savepoint = await api.begin_nested()
            count = await _call(api, completed.id, failed.id)
            assert type(count) is int and count == 2
            _assert_outcomes(
                await _read_job(api, completed.id), await _read_job(api, failed.id)
            )
            await savepoint.rollback()
            _assert_initial(await _read_job(api, completed.id), {})
            _assert_initial(await _read_job(api, failed.id), _FAILED_RESULT)
            await api.rollback()

        receipt = await _run_node_acceptance(test_engine, completed.id, failed.id)
        record_property("jobs_filter_fixture_node", json.dumps(receipt))

        async with api_sessions() as api:
            final_completed = await _read_job(api, completed.id)
            final_failed = await _read_job(api, failed.id)
            _assert_outcomes(final_completed, final_failed)

            await _assert_private_update_denied(
                runtime,
                "UPDATE public.async_jobs SET status='running', updated_at=pg_catalog.now() WHERE id=:id",
                completed.id,
            )
            await _assert_private_update_denied(
                runtime,
                "UPDATE public.async_jobs SET progress_pct=9 WHERE id=:id",
                completed.id,
            )

            for field, value in (("status", "failed"), ("updated_at", _OLD_TIMESTAMP)):
                legacy = await _read_job(api, completed.id)
                with pytest.raises(DBAPIError) as denied:
                    async with api.begin_nested():
                        setattr(legacy, field, value)
                        await api.flush()
                assert _sqlstate(denied.value) == "42501"
                _assert_outcomes(
                    await _read_job(api, completed.id), await _read_job(api, failed.id)
                )

            ordinary_job = await jobs.create_job(api, kind="audit_archive")
            lifecycle_id = ordinary_job.id
            await jobs.mark_running(
                api, ordinary_job.id, celery_task_id="ordinary-update-probe"
            )
            await jobs.update_progress(
                api,
                ordinary_job.id,
                9,
                result={"value": 1},
                extra_payload={"probe": True},
            )
            await api.flush()
            assert ordinary_job.progress_pct == 9
            assert "updated_at" in sa_inspect(ordinary_job).expired_attributes
            persisted = await _read_job(api, ordinary_job.id)
            assert persisted is ordinary_job
            assert persisted.status == "running" and persisted.progress_pct == 9
            assert (
                persisted.result == {"value": 1} and persisted.payload["probe"] is True
            )
            assert persisted.started_at is not None
            await jobs.mark_complete(api, ordinary_job.id, result={"done": True})
            await api.flush()
            assert (
                ordinary_job.status == "completed"
                and ordinary_job.completed_at is not None
            )
            await api.commit()
    finally:
        await runtime.dispose()
        async with owner_sessions() as cleanup:
            cleanup_ids = ids + ([lifecycle_id] if lifecycle_id is not None else [])
            await cleanup.execute(delete(AsyncJob).where(AsyncJob.id.in_(cleanup_ids)))
            await cleanup.commit()


async def test_filter_fixture_preserves_original_mapper_update_masks_and_order(
    test_engine,
):
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(test_engine.url)
    completed, failed = _new_pair()
    completed_id, failed_id = completed.id, failed.id
    suffix = uuid.uuid4().hex[:12]
    probe = "jobs_fixture_mask_" + suffix
    columns = (
        "status",
        "progress_pct",
        "result",
        "error_message",
        "updated_at",
        "started_at",
        "completed_at",
    )
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                session.add_all([completed, failed])
                await session.flush()
                await session.execute(
                    text(
                        f"CREATE TEMP TABLE {probe} (seq bigint GENERATED ALWAYS AS IDENTITY, job_id uuid, field text) ON COMMIT DROP"
                    )
                )
                await session.execute(
                    text(f"""
                    CREATE FUNCTION public.{probe}() RETURNS trigger
                    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
                    AS $capture$ BEGIN
                        INSERT INTO pg_temp.{probe}(job_id,field) VALUES(NEW.id,TG_ARGV[0]);
                        RETURN NEW;
                    END $capture$
                """)
                )
                await session.execute(
                    text(f"REVOKE ALL ON FUNCTION public.{probe}() FROM PUBLIC")
                )
                for field in columns:
                    await session.execute(
                        text(
                            f"CREATE TRIGGER {probe}_{field} AFTER UPDATE OF {field} "
                            f"ON public.async_jobs FOR EACH ROW EXECUTE FUNCTION public.{probe}('{field}')"
                        )
                    )
                savepoint = await session.begin_nested()
                completed.status = "completed"
                completed.progress_pct = 100
                completed.result = {"success_count": 2}
                failed.status = "failed"
                failed.progress_pct = 40
                failed.error_message = "fixture failure"
                await session.flush()
                query = text(f"SELECT job_id,field FROM pg_temp.{probe} ORDER BY seq")
                baseline = (await session.execute(query)).all()
                await savepoint.rollback()
                await session.execute(text("SET LOCAL ROLE identity_api_consumer"))
                assert await session.scalar(text("SELECT current_user::text")) == _ROLE
                assert await _call(session, completed_id, failed_id) == 2
                await session.execute(text("RESET ROLE"))
                observed = (await session.execute(query)).all()
                assert observed == baseline
                assert [row[0] for row in observed] == [failed_id] * 4 + [
                    completed_id
                ] * 4
                by_job = {
                    job_id: {field for row_id, field in observed if row_id == job_id}
                    for job_id in (completed_id, failed_id)
                }
                assert by_job[completed_id] == {
                    "status",
                    "progress_pct",
                    "result",
                    "updated_at",
                }
                assert by_job[failed_id] == {
                    "status",
                    "progress_pct",
                    "error_message",
                    "updated_at",
                }
        finally:
            await transaction.rollback()
