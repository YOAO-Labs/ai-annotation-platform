"""Native PostgreSQL acceptance for Data Manager job idempotency queries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import ARRAY, Text, bindparam, delete, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.user import User
from app.services.data_management.actions import assert_idempotent_request_matches
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
from tests.factory import create_project, create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres


ROOT = Path(__file__).resolve().parents[3]
ROLE = "identity_api_consumer"
SCHEMA = ROOT / "apps/jobs/src/aap_jobs/action_idempotency.schema.json"
NODE = ROOT / "apps/jobs/conformance/node/action_idempotency_acceptance.mjs"
JOB_KIND = "data_manager_acceptance"
ACTION = "data_manager_acceptance"
KEY = "动作-雪-🚙"
ROW_COLUMNS = (
    "id",
    "kind",
    "project_id",
    "retained_project_id",
    "user_id",
    "status",
    "progress_pct",
    "payload",
    "result",
    "error_message",
    "celery_task_id",
    "started_at",
    "completed_at",
    "created_at",
    "updated_at",
)
ROW_RETURN = (
    "TABLE(id uuid, kind character varying, project_id uuid, retained_project_id uuid, "
    "user_id uuid, status character varying, progress_pct integer, payload jsonb, "
    "result jsonb, error_message text, celery_task_id character varying, "
    "started_at timestamp with time zone, completed_at timestamp with time zone, "
    "created_at timestamp with time zone, updated_at timestamp with time zone)"
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned Jobs action-idempotency capability",
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


def _api_url(test_engine):
    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    assert url.username == ROLE
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.database and url.database.endswith(("_test", "_e2e"))
    return url


def _job(
    project_id,
    actor_id,
    *,
    job_id=None,
    kind=JOB_KIND,
    status="pending",
    payload,
    created_at,
):
    return AsyncJob(
        id=job_id or uuid.uuid4(),
        kind=kind,
        project_id=project_id,
        user_id=actor_id,
        status=status,
        progress_pct=0,
        payload=payload,
        result={"opaque_counter": 9007199254740993, "enabled": False},
        created_at=created_at,
        updated_at=created_at,
    )


@asynccontextmanager
async def _committed_world(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with sessions() as setup:
        actor = await create_user(
            setup, "employee", f"jobs-action-{suffix}@test.local", "Action actor"
        )
        other_actor = await create_user(
            setup,
            "employee",
            f"jobs-action-other-{suffix}@test.local",
            "Other action actor",
        )
        project = await create_project(setup, owner_id=actor.id, name="Action target")
        other_project = await create_project(
            setup, owner_id=other_actor.id, name="Action unrelated"
        )
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        tie_ids = sorted((uuid.uuid4(), uuid.uuid4()), key=lambda value: value.int)
        older = _job(
            project.id,
            actor.id,
            status="completed",
            payload={"data_manager_idempotency_key": KEY, "sequence": "older"},
            created_at=base + timedelta(seconds=1),
        )
        tie_low = _job(
            project.id,
            actor.id,
            job_id=tie_ids[0],
            status="completed",
            payload={"data_manager_idempotency_key": KEY, "sequence": "tie-low"},
            created_at=base + timedelta(seconds=2, microseconds=123456),
        )
        tie_high = _job(
            project.id,
            actor.id,
            job_id=tie_ids[1],
            status="failed",
            payload={
                "data_manager_idempotency_key": KEY,
                "data_manager_request_digest": "d" * 64,
                "opaque_counter": 9007199254740993,
                "nested": {"enabled": False, "missing": None},
                "label": "雪🚙",
            },
            created_at=base + timedelta(seconds=2, microseconds=123456),
        )
        null_missing = _job(
            project.id,
            actor.id,
            payload={"sequence": "missing key"},
            created_at=base + timedelta(seconds=5),
        )
        null_json = _job(
            project.id,
            actor.id,
            payload={"data_manager_idempotency_key": None},
            created_at=base + timedelta(seconds=6),
        )
        other_key = _job(
            project.id,
            actor.id,
            payload={"data_manager_idempotency_key": "other"},
            created_at=base + timedelta(seconds=7),
        )
        wrong_kind = _job(
            project.id,
            actor.id,
            kind="unrelated_kind",
            payload={"data_manager_idempotency_key": KEY},
            created_at=base + timedelta(seconds=8),
        )
        wrong_project = _job(
            other_project.id,
            actor.id,
            payload={"data_manager_idempotency_key": KEY},
            created_at=base + timedelta(seconds=9),
        )
        wrong_actor = _job(
            project.id,
            other_actor.id,
            payload={"data_manager_idempotency_key": KEY},
            created_at=base + timedelta(seconds=10),
        )
        retained = _job(
            project.id,
            actor.id,
            payload={"data_manager_idempotency_key": KEY, "sequence": "retained"},
            created_at=base + timedelta(seconds=11),
        )
        jobs = [
            older,
            tie_low,
            tie_high,
            null_missing,
            null_json,
            other_key,
            wrong_kind,
            wrong_project,
            wrong_actor,
            retained,
        ]
        setup.add_all(jobs)
        await setup.flush()
        await setup.execute(
            text("UPDATE public.async_jobs SET project_id=NULL WHERE id=:id"),
            {"id": retained.id},
        )
        await setup.commit()
        world = {
            "sessions": sessions,
            "actor_id": actor.id,
            "other_actor_id": other_actor.id,
            "project_id": project.id,
            "other_project_id": other_project.id,
            "matching_ids": [tie_high.id, tie_low.id, older.id],
            "null_ids": [null_json.id, null_missing.id],
            "nonmatching_ids": [
                other_key.id,
                wrong_kind.id,
                wrong_project.id,
                wrong_actor.id,
                retained.id,
                null_json.id,
                null_missing.id,
            ],
            "all_ids": [job.id for job in jobs],
            "expected_times": {str(job.id): job.created_at.isoformat() for job in jobs},
            "first_id": tie_high.id,
        }

    try:
        async with test_engine.connect() as connection:
            retained_scope = (
                await connection.execute(
                    text(
                        "SELECT project_id,retained_project_id FROM public.async_jobs "
                        "WHERE id=:id"
                    ),
                    {"id": retained.id},
                )
            ).one()
        assert retained_scope == (None, project.id)
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
            await cleanup.execute(
                delete(User).where(
                    User.id.in_([world["actor_id"], world["other_actor_id"]])
                )
            )
            await cleanup.commit()


@asynccontextmanager
async def _without_api_private_job_select(test_engine):
    columns = [column.name for column in AsyncJob.__table__.columns]
    quote = test_engine.dialect.identifier_preparer.quote_identifier
    quoted_columns = ", ".join(quote(column) for column in columns)
    async with test_engine.begin() as owner:
        original = (
            await owner.execute(
                text(
                    "SELECT pg_catalog.has_table_privilege(:role, 'public.async_jobs', "
                    "'SELECT'), pg_catalog.has_table_privilege(:role, "
                    "'public.async_jobs', 'SELECT WITH GRANT OPTION'), "
                    "pg_catalog.has_any_column_privilege(:role, 'public.async_jobs', "
                    "'SELECT'), "
                    "pg_catalog.has_any_column_privilege(:role, 'public.async_jobs', "
                    "'SELECT WITH GRANT OPTION'), "
                    "pg_catalog.has_table_privilege(:role, 'public.async_jobs', "
                    "'UPDATE'), pg_catalog.has_any_column_privilege(:role, "
                    "'public.async_jobs', 'UPDATE')"
                ),
                {"role": ROLE},
            )
        ).one()
        assert original == (True, False, True, False, False, False)
        await owner.execute(
            text(f"REVOKE SELECT ON TABLE public.async_jobs FROM {ROLE}")
        )
        await owner.execute(
            text(
                f"REVOKE SELECT ({quoted_columns}) ON TABLE public.async_jobs FROM {ROLE}"
            )
        )

    try:
        async with test_engine.connect() as consumer:
            rights = (
                await consumer.execute(
                    text(
                        "SELECT pg_catalog.has_table_privilege(:role, "
                        "'public.async_jobs', 'SELECT'), "
                        "pg_catalog.has_any_column_privilege(:role, "
                        "'public.async_jobs', 'SELECT')"
                    ),
                    {"role": ROLE},
                )
            ).one()
        assert rights == (False, False)
        yield
    finally:
        async with test_engine.begin() as owner:
            await owner.execute(
                text(f"GRANT SELECT ON TABLE public.async_jobs TO {ROLE}")
            )
        async with test_engine.connect() as restored:
            restored_rights = (
                await restored.execute(
                    text(
                        "SELECT pg_catalog.has_table_privilege(:role, "
                        "'public.async_jobs', 'SELECT'), "
                        "pg_catalog.has_any_column_privilege(:role, "
                        "'public.async_jobs', 'SELECT'), "
                        "pg_catalog.has_table_privilege(:role, 'public.async_jobs', "
                        "'UPDATE'), pg_catalog.has_any_column_privilege(:role, "
                        "'public.async_jobs', 'UPDATE')"
                    ),
                    {"role": ROLE},
                )
            ).one()
        assert restored_rights == (True, True, False, False)


async def _assert_contract(test_engine):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$id"] == "aap-jobs-action-idempotency/v1"
    assert schema["x-row-columns"] == list(ROW_COLUMNS)
    functions = schema["x-postgresql-functions"]
    assert functions["aap_jobs_v1.find_action_idempotent_jobs"]["returns"] == ROW_RETURN
    expected = {
        "lock_action_idempotency": (
            "text, uuid, uuid, text",
            "void",
        ),
        "find_action_idempotent_jobs": (
            "text, uuid, uuid, text",
            ROW_RETURN,
        ),
    }
    async with test_engine.connect() as catalog:
        rows = (
            (
                await catalog.execute(
                    text(
                        "SELECT p.proname, pg_catalog.oidvectortypes(p.proargtypes) AS args, "
                        "pg_catalog.pg_get_function_result(p.oid) AS result, "
                        "p.provolatile::text, p.proparallel::text, p.prosecdef, p.proconfig, "
                        "pg_catalog.pg_get_userbyid(p.proowner) AS owner_name, "
                        "pg_catalog.has_function_privilege(:role,p.oid,'EXECUTE') AS can_execute, "
                        "EXISTS (SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl, "
                        "pg_catalog.acldefault('f',p.proowner))) acl "
                        "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_execute, "
                        "pg_catalog.has_function_privilege('jobs_lifecycle_client',p.oid,'EXECUTE') "
                        "AS lifecycle_execute, CASE WHEN pg_catalog.to_regrole("
                        "'jobs_list_query_client') IS NULL THEN false ELSE "
                        "pg_catalog.has_function_privilege('jobs_list_query_client',p.oid,"
                        "'EXECUTE') END AS list_execute "
                        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n "
                        "ON n.oid=p.pronamespace WHERE n.nspname='aap_jobs_v1' "
                        "AND p.proname::text=ANY(:names) ORDER BY p.proname"
                    ).bindparams(bindparam("names", type_=ARRAY(Text()))),
                    {"role": ROLE, "names": list(expected)},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    assert {row["proname"] for row in rows} == set(expected)
    for row in rows:
        args, result = expected[row["proname"]]
        assert (row["args"], row["result"]) == (args, result)
        assert (row["provolatile"], row["proparallel"]) == ("v", "u")
        assert row["prosecdef"] is True
        assert row["proconfig"] == [
            "search_path=pg_catalog, pg_temp",
            "TimeZone=UTC",
            "DateStyle=ISO, YMD",
        ]
        assert row["owner_name"] == "jobs_code"
        assert row["can_execute"] is True
        assert row["public_execute"] is False
        assert row["lifecycle_execute"] is False
        assert row["list_execute"] is False


async def _run_node_acceptance(test_engine, world, api_url):
    node = shutil.which("node")
    assert node is not None
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "JOBS_ACTION_IDEMPOTENCY_DATABASE_URL": api_url.set(
            drivername="postgresql"
        ).render_as_string(hide_password=False),
        "JOBS_ACTION_IDEMPOTENCY_EXPECTED_ROLE": ROLE,
        "JOBS_ACTION_IDEMPOTENCY_EXPECTED_DATABASE": test_engine.url.database,
        "JOBS_ACTION_IDEMPOTENCY_EXPECTED_HOST": test_engine.url.host.lower(),
        "JOBS_ACTION_IDEMPOTENCY_EXPECTED_PORT": str(test_engine.url.port or 5432),
        "JOBS_ACTION_IDEMPOTENCY_KIND": JOB_KIND,
        "JOBS_ACTION_IDEMPOTENCY_ACTION": ACTION,
        "JOBS_ACTION_IDEMPOTENCY_KEY": KEY,
        "JOBS_ACTION_IDEMPOTENCY_PROJECT_ID": str(world["project_id"]),
        "JOBS_ACTION_IDEMPOTENCY_ACTOR_ID": str(world["actor_id"]),
        "JOBS_ACTION_IDEMPOTENCY_EXPECTED_JOB_IDS": json.dumps(
            [str(value) for value in world["matching_ids"]]
        ),
        "JOBS_ACTION_IDEMPOTENCY_EXPECTED_NULL_IDS": json.dumps(
            [str(value) for value in world["null_ids"]]
        ),
        "JOBS_ACTION_IDEMPOTENCY_EXPECTED_TIMES": json.dumps(world["expected_times"]),
    }
    result = await asyncio.to_thread(
        subprocess.run,
        [node, str(NODE), "--live"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt == {
        "status": "passed",
        "functions": 2,
        "rowColumns": len(ROW_COLUMNS),
        "matchedRows": len(world["matching_ids"]),
        "nullRows": len(world["null_ids"]),
    }
    return receipt


@pytest.mark.asyncio
async def test_action_idempotency_abi_api_login_node_and_orm_cache(
    test_engine, record_property
):
    api_url = _api_url(test_engine)
    await _assert_contract(test_engine)
    api_engine = create_async_engine(
        api_url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    api_sessions = async_sessionmaker(api_engine, expire_on_commit=False)
    try:
        async with _committed_world(test_engine) as world:
            async with _without_api_private_job_select(test_engine):
                async with api_engine.connect() as api:
                    identity = (
                        await api.execute(
                            text(
                                "SELECT session_user::text,current_user::text, "
                                "pg_catalog.has_table_privilege(current_user, "
                                "'public.async_jobs','SELECT'), "
                                "pg_catalog.has_any_column_privilege(current_user, "
                                "'public.async_jobs','SELECT'), "
                                "pg_catalog.has_table_privilege(current_user, "
                                "'public.async_jobs','UPDATE'), "
                                "pg_catalog.has_any_column_privilege(current_user, "
                                "'public.async_jobs','UPDATE')"
                            )
                        )
                    ).one()
                    assert identity == (ROLE, ROLE, True, True, False, False)
                    with pytest.raises(DBAPIError) as denied:
                        await api.execute(
                            text(
                                "SELECT id FROM public.async_jobs WHERE kind=:kind "
                                "AND project_id=:project_id AND user_id=:actor_id "
                                "AND payload->>'data_manager_idempotency_key'=:key "
                                "ORDER BY created_at DESC,id DESC FOR UPDATE"
                            ),
                            {
                                "kind": JOB_KIND,
                                "project_id": world["project_id"],
                                "actor_id": world["actor_id"],
                                "key": KEY,
                            },
                        )
                    assert _sqlstate(denied.value) == "42501"
                    await api.rollback()

                receipt = await _run_node_acceptance(test_engine, world, api_url)
                record_property(
                    "jobs_action_idempotency_node_consumer", json.dumps(receipt)
                )

                legacy_key = f"aap:data-manager:{ACTION}:{world['project_id']}:{world['actor_id']}:{KEY}"
                async with api_sessions() as api:
                    pending = _job(
                        world["project_id"],
                        world["actor_id"],
                        payload={"data_manager_idempotency_key": KEY},
                        created_at=datetime.now(timezone.utc),
                    )
                    api.add(pending)
                    await JobsDomainQueryPostgresData(api).lock_action_idempotency(
                        action=ACTION,
                        project_id=world["project_id"],
                        actor_id=world["actor_id"],
                        key=KEY,
                    )
                    assert pending in api.new
                    assert await _try_legacy_lock(api_engine, legacy_key) is False
                    await api.rollback()
                assert await _try_legacy_lock(api_engine, legacy_key) is True

                async with api_sessions() as api:
                    adapter = JobsDomainQueryPostgresData(api)
                    first = await adapter.find_action_idempotent_job(
                        kind=JOB_KIND,
                        project_id=world["project_id"],
                        actor_id=world["actor_id"],
                        key=KEY,
                    )
                    assert first is not None and first.id == world["first_id"]
                    assert first.status == "failed"
                    assert first.payload["opaque_counter"] == 9007199254740993
                    assert first.payload["nested"] == {
                        "enabled": False,
                        "missing": None,
                    }
                    assert_idempotent_request_matches(first, digest="d" * 64)
                    with pytest.raises(HTTPException) as conflict:
                        assert_idempotent_request_matches(first, digest="e" * 64)
                    assert getattr(conflict.value, "status_code", None) == 409
                    assert conflict.value.detail["code"] == "idempotency_conflict"
                    assert conflict.value.detail["job_id"] == str(first.id)
                    await api.commit()

                    async with owner_sessions() as owner:
                        await owner.execute(
                            update(AsyncJob)
                            .where(AsyncJob.id == first.id)
                            .values(status="running")
                        )
                        await owner.commit()

                    second = await adapter.find_action_idempotent_job(
                        kind=JOB_KIND,
                        project_id=world["project_id"],
                        actor_id=world["actor_id"],
                        key=KEY,
                    )
                    assert second is first
                    assert second.status == "failed"
                    assert (
                        await adapter.find_action_idempotent_job(
                            kind=JOB_KIND,
                            project_id=world["project_id"],
                            actor_id=world["actor_id"],
                            key=None,
                        )
                        is not None
                    )
                    await api.rollback()
    finally:
        await api_engine.dispose()


async def _try_legacy_lock(engine, advisory_key: str) -> bool:
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT pg_catalog.pg_try_advisory_xact_lock("
                "pg_catalog.hashtextextended(:key, 0))"
            ),
            {"key": advisory_key},
        )


@pytest.mark.asyncio
async def test_action_idempotency_lookup_locks_every_duplicate_and_waits(
    test_engine,
):
    api_url = _api_url(test_engine)
    api_engine = create_async_engine(
        api_url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    try:
        async with _committed_world(test_engine) as world:
            async with _without_api_private_job_select(test_engine):
                async with async_sessionmaker(
                    api_engine, expire_on_commit=False
                )() as api:
                    latest = await JobsDomainQueryPostgresData(
                        api
                    ).find_action_idempotent_job(
                        kind=JOB_KIND,
                        project_id=world["project_id"],
                        actor_id=world["actor_id"],
                        key=KEY,
                    )
                    assert latest is not None and latest.id == world["first_id"]

                    for job_id in world["matching_ids"]:
                        async with test_engine.connect() as probe:
                            with pytest.raises(DBAPIError) as locked:
                                await probe.execute(
                                    text(
                                        "SELECT id FROM public.async_jobs "
                                        "WHERE id=:id FOR UPDATE NOWAIT"
                                    ),
                                    {"id": job_id},
                                )
                            assert _sqlstate(locked.value) == "55P03"
                            await probe.rollback()

                    for job_id in world["nonmatching_ids"]:
                        async with test_engine.connect() as probe:
                            assert (
                                await probe.scalar(
                                    text(
                                        "SELECT id FROM public.async_jobs "
                                        "WHERE id=:id FOR UPDATE NOWAIT"
                                    ),
                                    {"id": job_id},
                                )
                                == job_id
                            )
                            await probe.rollback()

                    await api.rollback()
                    async with test_engine.connect() as blocker:
                        await blocker.begin()
                        await blocker.execute(
                            text(
                                "SELECT id FROM public.async_jobs WHERE id=:id FOR UPDATE"
                            ),
                            {"id": world["first_id"]},
                        )
                        waiting = asyncio.create_task(
                            api.execute(
                                text(
                                    "SELECT * FROM aap_jobs_v1.find_action_idempotent_jobs("
                                    "CAST(:kind AS text), CAST(:project_id AS uuid), "
                                    "CAST(:actor_id AS uuid), CAST(:key AS text))"
                                ),
                                {
                                    "kind": JOB_KIND,
                                    "project_id": world["project_id"],
                                    "actor_id": world["actor_id"],
                                    "key": KEY,
                                },
                            )
                        )
                        await asyncio.sleep(0.1)
                        assert not waiting.done(), "row-locking query did not wait"
                        await blocker.commit()
                        result = await asyncio.wait_for(waiting, timeout=5)
                        assert len(result.all()) == len(world["matching_ids"])
                        await api.rollback()
    finally:
        await api_engine.dispose()


@pytest.mark.asyncio
async def test_action_idempotency_advisory_lock_skips_autoflush_and_lookup_keeps_it(
    db_session,
):
    actor = await create_user(
        db_session, "employee", f"jobs-action-flush-{uuid.uuid4().hex}@test.local"
    )
    project = await create_project(db_session, owner_id=actor.id)
    pending = _job(
        project.id,
        actor.id,
        payload={"data_manager_idempotency_key": "autoflush"},
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(pending)
    adapter = JobsDomainQueryPostgresData(db_session)

    await adapter.lock_action_idempotency(
        action=ACTION,
        project_id=project.id,
        actor_id=actor.id,
        key="autoflush",
    )
    assert pending in db_session.new
    with db_session.no_autoflush:
        assert (
            await adapter.find_action_idempotent_job(
                kind=JOB_KIND,
                project_id=project.id,
                actor_id=actor.id,
                key="autoflush",
            )
            is None
        )
        assert pending in db_session.new

    found = await adapter.find_action_idempotent_job(
        kind=JOB_KIND,
        project_id=project.id,
        actor_id=actor.id,
        key="autoflush",
    )
    assert found is pending
    assert pending not in db_session.new
