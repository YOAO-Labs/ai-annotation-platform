"""Native PostgreSQL acceptance for fixed Jobs fixture outcome updates."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from tests.contract_schemas import load_contract_schema
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import delete, inspect as sa_inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob
from app.services import jobs_lifecycle_data as jobs

ROOT = Path(__file__).resolve().parents[3]
_LAN_GRANTS = ROOT / "infra/docker/lan-production-grants.sql"
_FUNCTION = "yoao_jobs_v1.set_filter_fixture_job_outcomes"
_CALL = (
    "SELECT yoao_jobs_v1.set_filter_fixture_job_outcomes("
    "CAST(:completed AS pg_catalog.uuid), CAST(:failed AS pg_catalog.uuid))"
)
_SCHEMA = ROOT / "apps/jobs/src/yoao_jobs/fixture_filter.schema.json"
_API_SCHEMA = "0331_jobs_filter_fixtures.schema.json"
_API_SQL = ROOT / "apps/api/alembic/sql/0331_jobs_filter_fixtures.sql"
_NODE = ROOT / "apps/jobs/conformance/node/filter_fixture_acceptance.mjs"
_OLD_TIMESTAMP = datetime(2001, 1, 1, tzinfo=timezone.utc)
_FAILED_RESULT: dict = {}

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
            kind="export",
            status="pending",
            progress_pct=0,
            payload={"fixture": "failed", "preserve": True},
            result={},
            created_at=_OLD_TIMESTAMP,
        ),
    )


async def _admit_presentation_proofs(
    session, run_id: uuid.UUID, completed_id: uuid.UUID, failed_id: uuid.UUID
) -> None:
    project_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO yoao_fixture_v1.fixture_runs (id, family, state) "
            "VALUES (:run_id, 'owned', 'open')"
        ),
        {"run_id": run_id},
    )
    await session.execute(
        text(
            "INSERT INTO yoao_fixture_v1.fixture_presentation_jobs "
            "(job_id, run_id, project_id, kind, state) VALUES "
            "(:completed_id, :run_id, :project_id, 'batch_predict', 'admitted'), "
            "(:failed_id, :run_id, :project_id, 'export', 'admitted')"
        ),
        {
            "completed_id": completed_id,
            "failed_id": failed_id,
            "run_id": run_id,
            "project_id": project_id,
        },
    )


async def _create_disposable_lan_runtime(test_engine):
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres
    from tests.test_workspace_consumer_profile_native import _grants

    _verify_postgres(test_engine.url)
    role = "jobs_update_lan_" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex
    async with test_engine.begin() as db:
        await db.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
            )
        )
    applied = await _grants(test_engine, _LAN_GRANTS, role)
    assert applied.returncode == 0, applied.stderr
    engine = create_async_engine(
        test_engine.url.set(username=role, password=password), hide_parameters=True
    )
    return engine, role


async def _drop_disposable_role(test_engine, role: str) -> None:
    async with test_engine.begin() as db:
        await db.execute(text(f"DROP OWNED BY {role}"))
        await db.execute(text(f"DROP ROLE {role}"))


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


async def _assert_contract(test_engine, runtime, runtime_role: str) -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    api_schema = json.loads(
        re.sub(
            r"\baap_(?=[a-z0-9_]*_v[0-9]+\b)",
            "yoao_",
            json.dumps(load_contract_schema(_API_SCHEMA)),
        )
    )
    assert api_schema == schema
    sql = _API_SQL.read_text(encoding="utf-8")
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
                    "ON n.oid=p.pronamespace WHERE n.nspname='yoao_jobs_v1' "
                    "AND p.proname='set_filter_fixture_job_outcomes'"
                ),
                {"role": runtime_role},
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
        False,
        False,
    )

    async with runtime.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == runtime_role
        assert await connection.scalar(text("SELECT session_user")) == runtime_role
        privilege = (
            await connection.execute(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','SELECT'), "
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','INSERT'), "
                    "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','INSERT'), "
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE'), "
                    "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','UPDATE'), "
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','DELETE'), "
                    "pg_catalog.has_table_privilege(current_user,'yoao_jobs_v1.async_job_creation','SELECT'), "
                    "pg_catalog.has_table_privilege(current_user,'yoao_jobs_v1.async_job_creation','INSERT'), "
                    "pg_catalog.has_function_privilege(current_user,:function,'EXECUTE')"
                ),
                {"function": f"{_FUNCTION}(uuid,uuid)"},
            )
        ).one()
        assert tuple(privilege) == (
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
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
    url = test_engine.url.set(drivername="postgresql")
    environment = os.environ.copy()
    environment.update(
        {
            "JOBS_FILTER_FIXTURE_DATABASE_URL": url.render_as_string(
                hide_password=False
            ),
            "JOBS_FILTER_FIXTURE_EXPECTED_ROLE": str(test_engine.url.username),
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
    completed, failed = _new_pair()
    run_id = uuid.uuid4()
    lifecycle_id = None
    runtime, runtime_role = await _create_disposable_lan_runtime(test_engine)
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    api_sessions = async_sessionmaker(runtime, expire_on_commit=False)
    ids = [completed.id, failed.id]
    try:
        await _assert_contract(test_engine, runtime, runtime_role)
        async with owner_sessions() as setup:
            setup.add_all([completed, failed])
            await setup.flush()
            completed.updated_at = _OLD_TIMESTAMP
            failed.result = _FAILED_RESULT
            failed.updated_at = _OLD_TIMESTAMP
            await setup.flush()
            await _admit_presentation_proofs(setup, run_id, completed.id, failed.id)
            await setup.commit()

        async with api_sessions() as api:
            await _assert_failure(api, completed.id, failed.id, "42501")

        async with owner_sessions() as owner:
            cached_completed = await _read_job(owner, completed.id)
            cached_failed = await _read_job(owner, failed.id)
            _assert_initial(cached_completed, {})
            _assert_initial(cached_failed, _FAILED_RESULT)

            await _assert_failure(owner, None, failed.id, "22023")
            await _assert_failure(owner, completed.id, None, "22023")
            await _assert_failure(owner, completed.id, completed.id, "22023")
            missing_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
            await _assert_failure(owner, completed.id, missing_id, "22023")
            _assert_initial(await _read_job(owner, completed.id), {})
            _assert_initial(await _read_job(owner, failed.id), _FAILED_RESULT)

            count = await _call(owner, completed.id, failed.id)
            assert type(count) is int and count == 2
            assert cached_completed.status == "pending"
            refreshed_completed = await _read_job(owner, completed.id)
            refreshed_failed = await _read_job(owner, failed.id)
            assert refreshed_completed is cached_completed
            assert refreshed_failed is cached_failed
            _assert_outcomes(refreshed_completed, refreshed_failed)
            await owner.rollback()
            _assert_initial(await _read_job(owner, completed.id), {})
            _assert_initial(await _read_job(owner, failed.id), _FAILED_RESULT)
            await owner.rollback()

            savepoint = await owner.begin_nested()
            count = await _call(owner, completed.id, failed.id)
            assert type(count) is int and count == 2
            _assert_outcomes(
                await _read_job(owner, completed.id), await _read_job(owner, failed.id)
            )
            await savepoint.rollback()
            _assert_initial(await _read_job(owner, completed.id), {})
            _assert_initial(await _read_job(owner, failed.id), _FAILED_RESULT)
            await owner.rollback()

        receipt = await _run_node_acceptance(test_engine, completed.id, failed.id)
        record_property("jobs_filter_fixture_node", json.dumps(receipt))

        async with owner_sessions() as owner:
            final_completed = await _read_job(owner, completed.id)
            final_failed = await _read_job(owner, failed.id)
            _assert_outcomes(final_completed, final_failed)

        async with api_sessions() as api:
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
        await _drop_disposable_role(test_engine, runtime_role)
        async with owner_sessions() as cleanup:
            cleanup_ids = ids + ([lifecycle_id] if lifecycle_id is not None else [])
            await cleanup.execute(delete(AsyncJob).where(AsyncJob.id.in_(cleanup_ids)))
            await cleanup.execute(
                text(
                    "DELETE FROM yoao_fixture_v1.fixture_presentation_jobs "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            )
            await cleanup.execute(
                text("DELETE FROM yoao_fixture_v1.fixture_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )
            await cleanup.commit()


async def test_filter_fixture_preserves_original_mapper_update_masks_and_order(
    test_engine,
):
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(test_engine.url)
    completed, failed = _new_pair()
    run_id = uuid.uuid4()
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
                await _admit_presentation_proofs(
                    session, run_id, completed_id, failed_id
                )
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
                assert await _call(session, completed_id, failed_id) == 2
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
