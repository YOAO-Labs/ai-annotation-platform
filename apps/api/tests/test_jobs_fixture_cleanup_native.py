"""Native PostgreSQL acceptance for scoped Jobs fixture cleanup."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_video_deletion_retirement_native import _retire, _seed_unretired

ROOT = Path(__file__).resolve().parents[3]
_ROLE = "identity_api_consumer"
_CALL = "SELECT aap_jobs_v1.delete_fixture_project_jobs(CAST(:ids AS uuid[]))"
_SCHEMA = ROOT / "apps/jobs/src/aap_jobs/fixture_cleanup.schema.json"
_NODE = ROOT / "apps/jobs/conformance/node/fixture_cleanup_acceptance.mjs"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned Jobs fixture-cleanup capability",
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


def _identity_api_url(test_engine):
    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    assert url.username == _ROLE
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.database and url.database.endswith(("_test", "_e2e"))
    return url


def _job(project_id, actor_id, *, kind, status, payload):
    return AsyncJob(
        id=uuid.uuid4(),
        kind=kind,
        project_id=project_id,
        user_id=actor_id,
        status=status,
        progress_pct=0,
        payload=payload,
        result={},
    )


@asynccontextmanager
async def _cleanup_world(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with sessions() as setup:
        actor = await create_user(
            setup, "super_admin", f"jobs-cleanup-{suffix}@test.local", "Jobs cleanup"
        )
        project = await create_project(setup, owner_id=actor.id, name="Cleanup target")
        other_project = await create_project(
            setup, owner_id=actor.id, name="Cleanup unrelated"
        )
        rows = [
            _job(
                project.id,
                actor.id,
                kind="export",
                status="completed",
                payload={"cleanup": "target", "sequence": 1},
            ),
            _job(
                project.id,
                actor.id,
                kind="batch_predict",
                status="failed",
                payload="legacy scalar JSON",
            ),
            _job(
                project.id,
                actor.id,
                kind="future_fixture_kind",
                status="cancelled",
                payload=["legacy array", 9007199254740993],
            ),
            _job(
                project.id,
                actor.id,
                kind="mask_qc",
                status="pending",
                payload=7,
            ),
            _job(
                other_project.id,
                actor.id,
                kind="export",
                status="running",
                payload={"cleanup": "unrelated"},
            ),
            _job(
                project.id,
                actor.id,
                kind="point_cloud_cross_frame",
                status="completed",
                payload={"cleanup": "retained"},
            ),
        ]
        retained = rows[-1]
        setup.add_all(rows)
        await setup.flush()
        await setup.execute(
            text("UPDATE public.async_jobs SET project_id=NULL WHERE id=:id"),
            {"id": retained.id},
        )
        await setup.commit()
        world = {
            "sessions": sessions,
            "actor_id": actor.id,
            "project_id": project.id,
            "other_project_id": other_project.id,
            "target_ids": [row.id for row in rows[:4]],
            "unrelated_id": rows[4].id,
            "retained_id": retained.id,
            "all_ids": [row.id for row in rows],
        }

    try:
        yield world
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AsyncJob).where(AsyncJob.id.in_(world["all_ids"]))
            )
            await cleanup.execute(
                delete(Project).where(
                    Project.id.in_([world["project_id"], world["other_project_id"]])
                )
            )
            await cleanup.execute(delete(User).where(User.id == world["actor_id"]))
            await cleanup.commit()


async def _job_rows_on(connection, ids):
    rows = await connection.execute(
        text(
            "SELECT id::text,project_id::text,retained_project_id::text,kind,status,payload "
            "FROM public.async_jobs WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
        ),
        {"ids": ids},
    )
    return rows.all()


async def _job_rows(engine, ids):
    async with engine.connect() as connection:
        return await _job_rows_on(connection, ids)


async def _workspace_guard_definition(engine):
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT pg_catalog.pg_get_functiondef("
                "'aap_workspace_fixture_v1.guard_project_cleanup(uuid[])'::regprocedure)"
            )
        )


async def _assert_contract(test_engine):
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    assert schema["$id"] == "aap-jobs-fixture-cleanup/v1"
    contract = schema["x-postgresql-functions"][
        "aap_jobs_v1.delete_fixture_project_jobs"
    ]
    assert contract["arguments"] == "uuid[]"
    assert contract["returns"] == "bigint"
    assert contract["volatility"] == "volatile"

    async with test_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT pg_catalog.oidvectortypes(p.proargtypes), "
                    "pg_catalog.pg_get_function_result(p.oid),p.provolatile::text, "
                    "p.prosecdef,p.proconfig,pg_catalog.pg_get_userbyid(p.proowner), "
                    "pg_catalog.has_function_privilege('identity_api_consumer',p.oid,'EXECUTE'), "
                    "EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl, "
                    "pg_catalog.acldefault('f',p.proowner))) acl "
                    "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') "
                    "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n "
                    "ON n.oid=p.pronamespace WHERE n.nspname='aap_jobs_v1' "
                    "AND p.proname='delete_fixture_project_jobs'"
                )
            )
        ).one()
    assert row == (
        "uuid[]",
        "bigint",
        "v",
        True,
        [
            "search_path=pg_catalog, pg_temp",
            "TimeZone=UTC",
            "DateStyle=ISO, YMD",
        ],
        "jobs_code",
        True,
        False,
    )


async def test_fixture_cleanup_contract_and_actual_api_login_preserve_scope_and_transactions(
    test_engine, record_property
):
    api_url = _identity_api_url(test_engine)
    await _assert_contract(test_engine)
    api_engine = create_async_engine(
        api_url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    try:
        async with _cleanup_world(test_engine) as world:
            async with api_engine.connect() as api:
                identity = (
                    await api.execute(
                        text(
                            "SELECT session_user::text,current_user::text, "
                            "pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT'), "
                            "pg_catalog.has_table_privilege(current_user,'public.async_jobs','INSERT'), "
                            "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','INSERT'), "
                            "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE'), "
                            "pg_catalog.has_table_privilege(current_user,'public.async_jobs','DELETE')"
                        )
                    )
                ).one()
                assert identity == (
                    _ROLE,
                    _ROLE,
                    True,
                    False,
                    False,
                    True,
                    False,
                )

                assert (
                    await api.scalar(
                        text(_CALL),
                        {"ids": []},
                    )
                    == 0
                )
                for invalid in (None, [None]):
                    with pytest.raises(DBAPIError) as rejected:
                        await api.scalar(
                            text(_CALL),
                            {"ids": invalid},
                        )
                    assert _sqlstate(rejected.value) == "22023"
                    await api.rollback()

            before = await _job_rows(test_engine, world["all_ids"])
            async with world["sessions"]() as lock_holder:
                await lock_holder.begin()
                await lock_holder.execute(
                    text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                    {"id": world["project_id"]},
                )
                async with api_engine.connect() as api:
                    with pytest.raises(DBAPIError) as busy:
                        await api.scalar(
                            text(_CALL),
                            {"ids": [world["project_id"]]},
                        )
                    assert _sqlstate(busy.value) == "55P03"
                    await api.rollback()
                await lock_holder.rollback()
            assert await _job_rows(test_engine, world["all_ids"]) == before

            environment = os.environ.copy()
            environment.update(
                {
                    "JOBS_FIXTURE_CLEANUP_DATABASE_URL": api_url.set(
                        drivername="postgresql"
                    ).render_as_string(hide_password=False),
                    "JOBS_FIXTURE_CLEANUP_EXPECTED_ROLE": _ROLE,
                    "JOBS_FIXTURE_CLEANUP_EXPECTED_DATABASE": api_url.database,
                    "JOBS_FIXTURE_CLEANUP_EXPECTED_HOST": api_url.host,
                    "JOBS_FIXTURE_CLEANUP_EXPECTED_PORT": str(api_url.port or 5432),
                    "JOBS_FIXTURE_CLEANUP_PROJECT_ID": str(world["project_id"]),
                    "JOBS_FIXTURE_CLEANUP_TARGET_IDS": json.dumps(
                        [str(item) for item in world["target_ids"]]
                    ),
                    "JOBS_FIXTURE_CLEANUP_UNRELATED_IDS": json.dumps(
                        [str(world["unrelated_id"])]
                    ),
                    "JOBS_FIXTURE_CLEANUP_RETAINED_IDS": json.dumps(
                        [str(world["retained_id"])]
                    ),
                    "JOBS_FIXTURE_CLEANUP_ALL_IDS": json.dumps(
                        [str(item) for item in world["all_ids"]]
                    ),
                }
            )
            node = await asyncio.to_thread(
                subprocess.run,
                ["node", str(_NODE), "--live"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert node.returncode == 0, node.stdout + node.stderr
            assert "Jobs fixture-cleanup Node acceptance passed" in node.stdout
            rows = await _job_rows(test_engine, world["all_ids"])
            assert {row[0] for row in rows} == {
                str(world["unrelated_id"]),
                str(world["retained_id"]),
            }
            assert next(row for row in rows if row[0] == str(world["retained_id"]))[
                2
            ] == str(world["project_id"])
            record_property(
                "jobs_fixture_cleanup",
                json.dumps(
                    {
                        "actual_api_login": True,
                        "scoped_delete_count": len(world["target_ids"]),
                        "unrelated_and_retained_rows_preserved": True,
                        "node_int8_and_transaction_abi": True,
                        "private_delete_denied": True,
                        "project_nowait_and_invalid_scope": True,
                    }
                ),
            )
    finally:
        await api_engine.dispose()


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            {
                "schema": "aap-workspace-e2e-fixtures/v1",
                "ok": True,
                "reason": None,
                "unexpected": "extra",
            },
            id="extra-field",
        ),
        pytest.param(
            {"schema": "aap-workspace-e2e-fixtures/v1", "ok": True},
            id="missing-reason",
        ),
        pytest.param(
            {
                "schema": "aap-workspace-e2e-fixtures/v1",
                "ok": "true",
                "reason": None,
            },
            id="non-boolean-ok",
        ),
        pytest.param("invalid-reply", id="non-object"),
        pytest.param(None, id="json-null"),
    ],
)
async def test_fixture_cleanup_rejects_malformed_workspace_guard_replies_rollback_only(
    test_engine, reply, record_property
):
    _identity_api_url(test_engine)
    before_definition = await _workspace_guard_definition(test_engine)
    async with _cleanup_world(test_engine) as world:
        before_rows = await _job_rows(test_engine, world["all_ids"])
        async with test_engine.connect() as owner:
            transaction = await owner.begin()
            try:
                assert await owner.scalar(text("SELECT session_user::text")) == (
                    test_engine.url.username
                )
                encoded = json.dumps(reply, ensure_ascii=False).replace("'", "''")
                await owner.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION "
                        "aap_workspace_fixture_v1.guard_project_cleanup(p_project_ids uuid[]) "
                        "RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER "
                        "SET search_path = pg_catalog, pg_temp SET TimeZone TO 'UTC' "
                        "SET DateStyle TO 'ISO, YMD' "
                        f"AS $function$ SELECT '{encoded}'::pg_catalog.jsonb $function$"
                    )
                )
                await owner.execute(text("SET LOCAL ROLE identity_api_consumer"))
                assert await owner.scalar(text("SELECT current_user::text")) == _ROLE
                savepoint = await owner.begin_nested()
                with pytest.raises(DBAPIError) as rejected:
                    await owner.scalar(text(_CALL), {"ids": [world["project_id"]]})
                assert _sqlstate(rejected.value) == "22023"
                await savepoint.rollback()
                assert await _job_rows_on(owner, world["all_ids"]) == before_rows
            finally:
                if transaction.is_active:
                    await transaction.rollback()

        assert await _job_rows(test_engine, world["all_ids"]) == before_rows
        assert await _workspace_guard_definition(test_engine) == before_definition
        record_property(
            "jobs_fixture_cleanup_malformed_guard_reply",
            json.dumps(
                {"sqlstate": "22023", "jobs_unchanged": True, "ddl_rolled_back": True}
            ),
        )


async def _video_fixture_snapshot(engine, fixture):
    queries = {
        "project": (
            "SELECT pg_catalog.to_jsonb(item) FROM public.projects item WHERE item.id=:id",
            {"id": fixture["project_id"]},
        ),
        "tasks": (
            "SELECT pg_catalog.to_jsonb(item) FROM public.tasks item WHERE item.project_id=:id ORDER BY item.id",
            {"id": fixture["project_id"]},
        ),
        "tracker_job": (
            "SELECT pg_catalog.to_jsonb(item) FROM public.video_tracker_jobs item WHERE item.id=:id",
            {"id": fixture["job_id"]},
        ),
        "journal": (
            "SELECT pg_catalog.to_jsonb(item) FROM aap_jobs_v1.video_execution_journals item WHERE item.job_id=:id",
            {"id": fixture["job_id"]},
        ),
        "input_scope": (
            "SELECT pg_catalog.to_jsonb(item) FROM public.video_tracker_input_scopes item WHERE item.tracker_job_id=:id",
            {"id": fixture["job_id"]},
        ),
        "accepted_input": (
            "SELECT pg_catalog.to_jsonb(item) FROM public.asset_accepted_video_inputs item WHERE item.input_id=:id",
            {"id": fixture["input_id"]},
        ),
        "jobs": (
            "SELECT pg_catalog.to_jsonb(item) FROM public.async_jobs item "
            "WHERE item.project_id=:id OR item.retained_project_id=:id ORDER BY item.id",
            {"id": fixture["project_id"]},
        ),
    }
    result = {}
    async with engine.connect() as connection:
        for name, (query, params) in queries.items():
            result[name] = (await connection.scalars(text(query), params)).all()
    return result


async def test_fixture_cleanup_rejects_unretired_video_without_changing_rows(
    test_engine, record_property
):
    api_url = _identity_api_url(test_engine)
    api_engine = create_async_engine(
        api_url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    fixture = None
    try:
        async with sessions() as setup:
            fixture = await _seed_unretired(setup)
            await setup.commit()
        before = await _video_fixture_snapshot(test_engine, fixture)
        assert (
            before["tracker_job"] and before["input_scope"] and before["accepted_input"]
        )
        assert before["journal"]
        assert before["journal"][0]["execution_retired"] is False

        async with api_engine.connect() as api:
            with pytest.raises(DBAPIError) as rejected:
                await api.scalar(
                    text(_CALL),
                    {"ids": [fixture["project_id"]]},
                )
            assert _sqlstate(rejected.value) == "23514"
            await api.rollback()

        assert await _video_fixture_snapshot(test_engine, fixture) == before
        record_property(
            "jobs_fixture_cleanup_unretired_video",
            json.dumps(
                {
                    "actual_api_login": True,
                    "sqlstate": "23514",
                    "all_rows_preserved": True,
                }
            ),
        )
    finally:
        await api_engine.dispose()
        if fixture is not None:
            async with sessions() as cleanup:
                await _retire(cleanup, fixture)
                await cleanup.execute(
                    delete(AsyncJob).where(
                        (AsyncJob.project_id == fixture["project_id"])
                        | (AsyncJob.retained_project_id == fixture["project_id"])
                    )
                )
                removed = await cleanup.scalar(
                    text(
                        "SELECT aap_workspace_fixture_v1.delete_projects(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [fixture["project_id"]]},
                )
                assert removed == 1
                await cleanup.execute(
                    delete(User).where(User.id == fixture["actor_id"])
                )
                await cleanup.commit()
