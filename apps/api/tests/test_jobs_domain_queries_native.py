"""Native PostgreSQL parity and least-privilege checks for Jobs domain queries."""

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

import pytest
from sqlalchemy import (
    ARRAY,
    Integer,
    Text,
    bindparam,
    delete,
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.user import User
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
from tests.factory import create_project, create_user


_ROLE_ACCEPTANCE = os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
pytestmark = pytest.mark.skipif(
    not _ROLE_ACCEPTANCE, reason="requires verifier-owned Jobs query capabilities"
)
_JOB_KIND = "point_cloud_cross_frame"
_ACTIVE_STATUSES = ("pending", "running")


def _adapter(db):
    return JobsDomainQueryPostgresData(db)


def _job(
    *,
    actor_id: uuid.UUID,
    kind: str = _JOB_KIND,
    status: str = "pending",
    project_id: uuid.UUID | None = None,
    retained_project_id: uuid.UUID | None = None,
    payload,
    created_at: datetime,
) -> AsyncJob:
    return AsyncJob(
        id=uuid.uuid4(),
        kind=kind,
        project_id=project_id,
        retained_project_id=retained_project_id,
        user_id=actor_id,
        status=status,
        progress_pct=0,
        payload=payload,
        result={},
        created_at=created_at,
    )


async def _legacy_active(db, actor_id, singleflight_key):
    return (
        await db.execute(
            select(AsyncJob)
            .where(AsyncJob.kind == _JOB_KIND)
            .where(AsyncJob.user_id == actor_id)
            .where(AsyncJob.status.in_(_ACTIVE_STATUSES))
            .where(AsyncJob.payload["singleflight_key"].astext == singleflight_key)
            .order_by(AsyncJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _legacy_scene_query(actor_id, scene_id):
    return (
        select(AsyncJob)
        .where(AsyncJob.kind == _JOB_KIND)
        .where(AsyncJob.user_id == actor_id)
        .where(AsyncJob.payload["scene_id"].astext == scene_id)
        .order_by(AsyncJob.created_at.desc())
    )


async def _legacy_batch_times(db, project_ids):
    return (
        await db.execute(
            select(AsyncJob.project_id, func.max(AsyncJob.created_at))
            .where(AsyncJob.kind == "batch_predict")
            .where(AsyncJob.project_id.in_(project_ids))
            .group_by(AsyncJob.project_id)
        )
    ).all()


async def _query_world(db, *, label: str):
    suffix = uuid.uuid4().hex
    actor = await create_user(
        db,
        "employee",
        f"jobs-domain-{label}-{suffix}@test.local",
        "Jobs domain actor",
    )
    other_actor = await create_user(
        db,
        "employee",
        f"jobs-domain-other-{label}-{suffix}@test.local",
        "Other Jobs domain actor",
    )
    project = await create_project(db, owner_id=actor.id)
    other_project = await create_project(db, owner_id=other_actor.id)
    return actor, other_actor, project, other_project


def _cross_frame_rows(actor_id, other_actor_id, project_id, other_project_id, base):
    scene_id = "場景-🚙"
    key = "合并-ß-雪"
    active_payload = {
        "singleflight_key": key,
        "scene_id": scene_id,
        "opaque_counter": 9007199254740993,
    }

    def row(
        second,
        *,
        actor=actor_id,
        kind=_JOB_KIND,
        status="pending",
        project=project_id,
        retained=None,
        payload,
    ):
        return _job(
            actor_id=actor,
            kind=kind,
            status=status,
            project_id=project,
            retained_project_id=retained,
            payload=payload,
            created_at=base + timedelta(seconds=second),
        )

    return [
        row(0, payload=active_payload),
        row(1, status="running", payload=active_payload),
        row(
            2,
            status="completed",
            payload={"singleflight_key": key, "scene_id": scene_id},
        ),
        row(
            3,
            status="cancelled",
            payload={"singleflight_key": key, "scene_id": scene_id},
        ),
        row(
            4,
            kind="point_cloud_cross_frame_unknown",
            status="running",
            payload=active_payload,
        ),
        row(5, actor=other_actor_id, status="running", payload=active_payload),
        row(
            6,
            status="failed",
            payload={"singleflight_key": "different", "scene_id": scene_id},
        ),
        row(
            7,
            status="failed",
            project=None,
            retained=project_id,
            payload={"scene_id": scene_id},
        ),
        row(
            8,
            status="completed",
            project=other_project_id,
            payload={"scene_id": scene_id},
        ),
        row(9, payload={"scene_id": "另一个场景"}),
        row(10, payload=None),
        row(11, payload=[{"scene_id": scene_id}]),
        row(12, payload="scene payload"),
        row(13, payload={}),
    ]


async def _persist_fixture_jobs(db, jobs):
    detached = []
    for job in jobs:
        if job.retained_project_id is not None:
            assert job.project_id is None
            job.project_id = job.retained_project_id
            job.retained_project_id = None
            detached.append(job)
    db.add_all(jobs)
    await db.flush()
    for job in detached:
        await db.execute(
            text("UPDATE public.async_jobs SET project_id=NULL WHERE id=:id"),
            {"id": job.id},
        )
        await db.refresh(job)


@asynccontextmanager
async def _committed_query_world(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with sessions() as setup:
        actor, other_actor, project, other_project = await _query_world(
            setup, label="api-" + suffix
        )
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        scene_id = "場景-🚙"
        key = "合并-ß-雪"
        jobs = _cross_frame_rows(
            actor.id, other_actor.id, project.id, other_project.id, base
        )
        batch_jobs = [
            _job(
                actor_id=actor.id,
                kind="batch_predict",
                project_id=project.id,
                payload={},
                created_at=base + timedelta(seconds=14),
            ),
            _job(
                actor_id=actor.id,
                kind="batch_predict",
                project_id=project.id,
                payload={},
                created_at=base + timedelta(seconds=15),
            ),
            _job(
                actor_id=actor.id,
                kind="batch_predict",
                project_id=other_project.id,
                payload={},
                created_at=base + timedelta(seconds=16),
            ),
        ]
        await _persist_fixture_jobs(setup, [*jobs, *batch_jobs])
        await setup.commit()
        world = {
            "sessions": sessions,
            "actor_id": actor.id,
            "other_actor_id": other_actor.id,
            "project_id": project.id,
            "other_project_id": other_project.id,
            "job_ids": [job.id for job in jobs],
            "batch_job_ids": [job.id for job in batch_jobs],
            "scene_id": scene_id,
            "singleflight_key": key,
            "batch_times": {
                project.id: base + timedelta(seconds=15),
                other_project.id: base + timedelta(seconds=16),
            },
        }

    try:
        yield world
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AsyncJob).where(
                    AsyncJob.id.in_([*world["job_ids"], *world["batch_job_ids"]])
                )
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


@asynccontextmanager
async def _without_api_private_job_access(test_engine):
    # The verifier owns this entire database; separate Node/API LOGINs need a
    # committed denial, restored before another case can use the ordinary role.
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(test_engine.url)
    async with test_engine.begin() as owner:
        rights = (
            await owner.execute(
                text(
                    "SELECT pg_catalog.has_table_privilege('identity_api_consumer', "
                    "'public.async_jobs','SELECT'), "
                    "pg_catalog.has_table_privilege('identity_api_consumer', "
                    "'public.async_jobs','UPDATE'), "
                    "pg_catalog.has_table_privilege('identity_api_consumer', "
                    "'public.async_jobs','SELECT WITH GRANT OPTION'), "
                    "pg_catalog.has_table_privilege('identity_api_consumer', "
                    "'public.async_jobs','UPDATE WITH GRANT OPTION')"
                )
            )
        ).one()
        assert rights == (True, True, False, False)
        await owner.execute(
            text(
                "REVOKE SELECT, UPDATE ON public.async_jobs FROM identity_api_consumer"
            )
        )
    try:
        yield
    finally:
        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    "GRANT SELECT, UPDATE ON public.async_jobs TO identity_api_consumer"
                )
            )


@pytest.mark.asyncio
async def test_cross_frame_queries_match_original_orm_filters_and_order(db_session):
    actor, other_actor, project, other_project = await _query_world(
        db_session, label="parity"
    )
    base = datetime(2026, 10, 2, tzinfo=timezone.utc)
    scene_id = "場景-🚙"
    key = "合并-ß-雪"
    rows = _cross_frame_rows(
        actor.id, other_actor.id, project.id, other_project.id, base
    )
    await _persist_fixture_jobs(db_session, rows)

    expected_active = await _legacy_active(db_session, actor.id, key)
    actual_active = await _adapter(db_session).active_cross_frame_job(actor.id, key)
    assert expected_active is rows[1]
    assert actual_active is expected_active

    expected_page = list(
        (await db_session.execute(_legacy_scene_query(actor.id, scene_id).limit(50)))
        .scalars()
        .all()
    )
    expected_count = (
        await db_session.execute(
            select(func.count())
            .select_from(AsyncJob)
            .where(AsyncJob.kind == _JOB_KIND)
            .where(AsyncJob.user_id == actor.id)
            .where(AsyncJob.payload["scene_id"].astext == scene_id)
        )
    ).scalar_one()
    native_statements: list[str] = []

    def observe_native(_connection, _cursor, statement, _parameters, _context, _many):
        native_statements.append(statement)

    sync_engine = db_session.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", observe_native)
    try:
        actual_page = await _adapter(db_session).cross_frame_page(
            actor.id, scene_id, 50
        )
        actual_count = await _adapter(db_session).cross_frame_count(actor.id, scene_id)
    finally:
        event.remove(sync_engine, "before_cursor_execute", observe_native)
    assert [row.id for row in expected_page] == [
        rows[8].id,
        rows[7].id,
        rows[6].id,
        rows[3].id,
        rows[2].id,
        rows[1].id,
        rows[0].id,
    ]
    assert [row.id for row in actual_page] == [row.id for row in expected_page]
    assert all(
        actual is expected
        for actual, expected in zip(actual_page, expected_page, strict=True)
    )
    assert actual_count == expected_count == len(expected_page)
    assert sum("cross_frame_job_page" in sql for sql in native_statements) == 1
    assert sum("cross_frame_job_count" in sql for sql in native_statements) == 1
    assert rows[7].project_id is None and rows[7].retained_project_id == project.id


@pytest.mark.asyncio
async def test_batch_prediction_times_match_original_query_for_empty_and_duplicate_ids(
    db_session,
):
    actor, other_actor, project, other_project = await _query_world(
        db_session, label="times"
    )
    base = datetime(2026, 10, 3, tzinfo=timezone.utc)
    jobs = [
        _job(
            actor_id=actor.id,
            kind="batch_predict",
            project_id=project.id,
            payload={},
            created_at=base,
        ),
        _job(
            actor_id=actor.id,
            kind="batch_predict",
            project_id=project.id,
            payload={},
            created_at=base + timedelta(seconds=1),
        ),
        _job(
            actor_id=actor.id,
            kind="batch_predict",
            project_id=other_project.id,
            payload={},
            created_at=base + timedelta(seconds=2),
        ),
        _job(
            actor_id=actor.id,
            kind="batch_predict_archive",
            project_id=project.id,
            payload={},
            created_at=base + timedelta(seconds=3),
        ),
        _job(
            actor_id=actor.id,
            kind="batch_predict",
            project_id=None,
            retained_project_id=project.id,
            payload={},
            created_at=base + timedelta(seconds=4),
        ),
        _job(
            actor_id=other_actor.id,
            kind="batch_predict",
            project_id=project.id,
            payload={},
            created_at=base + timedelta(seconds=5),
        ),
    ]
    await _persist_fixture_jobs(db_session, jobs)

    project_ids = [project.id, project.id, other_project.id]
    expected = dict(await _legacy_batch_times(db_session, project_ids))
    actual = dict(await _adapter(db_session).latest_batch_prediction_times(project_ids))
    assert (
        actual
        == expected
        == {
            project.id: base + timedelta(seconds=5),
            other_project.id: base + timedelta(seconds=2),
        }
    )

    empty_function = text(
        "SELECT * FROM aap_jobs_v1.latest_batch_prediction_times(:project_ids)"
    ).bindparams(bindparam("project_ids", type_=ARRAY(PG_UUID(as_uuid=True))))
    assert (await db_session.execute(empty_function, {"project_ids": []})).all() == []


@pytest.mark.asyncio
async def test_domain_query_session_flush_cache_and_caller_rollback(db_session):
    actor, _, project, _ = await _query_world(db_session, label="uow")
    adapter = _adapter(db_session)
    scene_id = "scene-uow"
    singleflight_key = "singleflight-uow"
    base = datetime(2026, 10, 4, tzinfo=timezone.utc)
    statements: list[str] = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", observe)
    lock_pending = _job(
        actor_id=actor.id,
        project_id=project.id,
        payload={"singleflight_key": singleflight_key},
        created_at=base - timedelta(seconds=1),
    )
    db_session.add(lock_pending)
    await adapter.lock_cross_frame_singleflight(actor.id, singleflight_key)
    assert lock_pending in db_session.new
    assert not any(
        "INSERT INTO aap_jobs_v1.async_job_creation" in sql for sql in statements
    )
    active_after_lock = await adapter.active_cross_frame_job(actor.id, singleflight_key)
    assert active_after_lock is lock_pending
    assert lock_pending not in db_session.new
    lock_index = next(
        index
        for index, sql in enumerate(statements)
        if "lock_cross_frame_singleflight" in sql
    )
    insert_index = next(
        index
        for index, sql in enumerate(statements)
        if "INSERT INTO aap_jobs_v1.async_job_creation" in sql
    )
    active_index = next(
        index for index, sql in enumerate(statements) if "active_cross_frame_job" in sql
    )
    assert lock_index < insert_index < active_index

    auto_page_job = _job(
        actor_id=actor.id,
        project_id=project.id,
        payload={"scene_id": scene_id},
        created_at=base,
    )
    db_session.add(auto_page_job)
    page = await adapter.cross_frame_page(actor.id, scene_id, 10)
    assert auto_page_job in page

    no_autoflush_page_job = _job(
        actor_id=actor.id,
        project_id=project.id,
        payload={"scene_id": scene_id},
        created_at=base + timedelta(seconds=1),
    )
    with db_session.no_autoflush:
        db_session.add(no_autoflush_page_job)
        page_without_pending = await adapter.cross_frame_page(actor.id, scene_id, 10)
        assert no_autoflush_page_job.id not in {row.id for row in page_without_pending}

    dirty = auto_page_job
    dirty.payload = {"scene_id": "uncommitted-scene"}
    with db_session.no_autoflush:
        page_with_stale_database_match = await adapter.cross_frame_page(
            actor.id, scene_id, 10
        )
    assert dirty in page_with_stale_database_match
    assert page_with_stale_database_match[0] is dirty
    assert dirty.payload == {"scene_id": "uncommitted-scene"}
    assert db_session.is_modified(dirty, include_collections=True)
    assert not any("UPDATE public.async_jobs" in sql for sql in statements)

    count_pending = _job(
        actor_id=actor.id,
        project_id=project.id,
        payload={"scene_id": scene_id},
        created_at=base + timedelta(seconds=2),
    )
    db_session.add(count_pending)
    assert await adapter.cross_frame_count(actor.id, scene_id) == 2

    no_autoflush_count_pending = _job(
        actor_id=actor.id,
        project_id=project.id,
        payload={"scene_id": scene_id},
        created_at=base + timedelta(seconds=3),
    )
    with db_session.no_autoflush:
        db_session.add(no_autoflush_count_pending)
        assert await adapter.cross_frame_count(actor.id, scene_id) == 2
        assert no_autoflush_count_pending in db_session.new

    batch_pending = _job(
        actor_id=actor.id,
        kind="batch_predict",
        project_id=project.id,
        payload={},
        created_at=base + timedelta(seconds=5),
    )
    db_session.add(batch_pending)
    batch_times = dict(await adapter.latest_batch_prediction_times([project.id]))
    assert batch_times == {project.id: batch_pending.created_at}

    no_autoflush_batch_pending = _job(
        actor_id=actor.id,
        kind="batch_predict",
        project_id=project.id,
        payload={},
        created_at=base + timedelta(seconds=6),
    )
    with db_session.no_autoflush:
        db_session.add(no_autoflush_batch_pending)
        assert (
            dict(await adapter.latest_batch_prediction_times([project.id]))
            == batch_times
        )
        assert no_autoflush_batch_pending in db_session.new

    await db_session.flush()
    savepoint = await db_session.begin_nested()
    rollback_job = _job(
        actor_id=actor.id,
        project_id=project.id,
        payload={"scene_id": scene_id},
        created_at=base + timedelta(seconds=4),
    )
    db_session.add(rollback_job)
    before_rollback = await adapter.cross_frame_page(actor.id, scene_id, 10)
    assert rollback_job in before_rollback
    await savepoint.rollback()
    assert db_session.in_transaction()
    after_rollback = await adapter.cross_frame_page(actor.id, scene_id, 10)
    assert rollback_job.id not in {row.id for row in after_rollback}


@pytest.mark.asyncio
async def test_native_cross_frame_page_rejects_limits_outside_one_to_fifty(db_session):
    invalid_page = text(
        "SELECT * FROM aap_jobs_v1.cross_frame_job_page(:actor_id, :scene_id, :limit)"
    ).bindparams(
        bindparam("actor_id", type_=PG_UUID(as_uuid=True)),
        bindparam("scene_id", type_=Text()),
        bindparam("limit", type_=Integer()),
    )
    for limit in (None, 0, 51):
        with pytest.raises(DBAPIError) as raised:
            await db_session.execute(
                invalid_page,
                {"actor_id": uuid.uuid4(), "scene_id": "scene", "limit": limit},
            )
        assert _sqlstate(raised.value) == "22023"
        await db_session.rollback()


@pytest.mark.asyncio
async def test_domain_query_abi_and_ordinary_api_login_are_read_only(
    test_engine, record_property
):
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(test_engine.url)
    api_url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    assert (api_url.host, api_url.port, api_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert api_url.database and api_url.database.endswith("_test")
    assert api_url.username == "identity_api_consumer"

    row_result = (
        "TABLE(id uuid, kind character varying, project_id uuid, "
        "retained_project_id uuid, user_id uuid, status character varying, "
        "progress_pct integer, payload jsonb, result jsonb, error_message text, "
        "celery_task_id character varying, started_at timestamp with time zone, "
        "completed_at timestamp with time zone, created_at timestamp with time zone, "
        "updated_at timestamp with time zone)"
    )
    expected = {
        "lock_cross_frame_singleflight": ("uuid, text", "void", "v"),
        "active_cross_frame_job": ("uuid, text", row_result, "s"),
        "cross_frame_job_page": ("uuid, text, integer", row_result, "s"),
        "cross_frame_job_count": ("uuid, text", "bigint", "s"),
        "latest_batch_prediction_times": (
            "uuid[]",
            "TABLE(project_id uuid, latest_created_at timestamp with time zone)",
            "s",
        ),
    }
    async with test_engine.connect() as catalog:
        rows = (
            (
                await catalog.execute(
                    text(
                        """
                    SELECT p.proname AS name,
                           pg_catalog.oidvectortypes(p.proargtypes) AS args,
                           pg_catalog.pg_get_function_result(p.oid) AS result,
                           p.provolatile::text AS provolatile,
                           p.prosecdef,
                           p.proconfig,
                           pg_catalog.pg_get_userbyid(p.proowner) AS owner_name,
                           EXISTS (
                               SELECT FROM pg_catalog.aclexplode(
                                   COALESCE(
                                       p.proacl,
                                       pg_catalog.acldefault('f', p.proowner)
                                   )
                               ) AS privilege
                               WHERE privilege.grantee = 0
                                 AND privilege.privilege_type = 'EXECUTE'
                           ) AS public_execute,
                           pg_catalog.has_function_privilege(
                               'identity_api_consumer', p.oid, 'EXECUTE'
                           ) AS api_execute,
                           pg_catalog.has_function_privilege(
                               'jobs_lifecycle_client', p.oid, 'EXECUTE'
                           ) AS lifecycle_execute
                    FROM pg_catalog.pg_proc AS p
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = p.pronamespace
                    WHERE namespace.nspname = 'aap_jobs_v1'
                      AND p.proname::text = ANY(:names)
                    ORDER BY p.proname
                    """
                    ).bindparams(bindparam("names", type_=ARRAY(Text()))),
                    {"names": list(expected)},
                )
            )
            .mappings()
            .all()
        )
        assert {row["name"] for row in rows} == set(expected)
        for row in rows:
            name = row["name"]
            assert (row["args"], row["result"], row["provolatile"]) == expected[name]
            assert row["prosecdef"] is True
            assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
            assert row["owner_name"] == "jobs_code"
            assert row["public_execute"] is False
            assert row["api_execute"] is True
            assert row["lifecycle_execute"] is False

    engine = create_async_engine(
        api_url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with (
            _committed_query_world(test_engine) as world,
            _without_api_private_job_access(test_engine),
        ):
            async with sessions() as api:
                identity = (
                    await api.execute(
                        text(
                            "SELECT session_user::text, current_user::text, "
                            "pg_catalog.has_table_privilege(current_user, "
                            "'public.async_jobs', 'SELECT'), "
                            "pg_catalog.has_any_column_privilege(current_user, "
                            "'public.async_jobs', 'SELECT'), "
                            "pg_catalog.has_table_privilege(current_user, "
                            "'public.async_jobs', 'UPDATE'), "
                            "pg_catalog.has_any_column_privilege(current_user, "
                            "'public.async_jobs', 'UPDATE')"
                        )
                    )
                ).one()
                assert identity == (
                    "identity_api_consumer",
                    "identity_api_consumer",
                    False,
                    False,
                    False,
                    False,
                )

                adapter = _adapter(api)
                await adapter.lock_cross_frame_singleflight(
                    world["actor_id"], world["singleflight_key"]
                )
                active = await adapter.active_cross_frame_job(
                    world["actor_id"], world["singleflight_key"]
                )
                page = await adapter.cross_frame_page(
                    world["actor_id"], world["scene_id"], 50
                )
                count = await adapter.cross_frame_count(
                    world["actor_id"], world["scene_id"]
                )
                times = dict(
                    await adapter.latest_batch_prediction_times(
                        [world["project_id"], world["other_project_id"]]
                    )
                )
                assert active is not None and active.status == "running"
                assert {row.id for row in page} == set(world["job_ids"][:4]) | {
                    world["job_ids"][6],
                    world["job_ids"][7],
                    world["job_ids"][8],
                }
                assert count == len(page)
                assert times == world["batch_times"]

                with pytest.raises(DBAPIError) as denied_select:
                    await api.execute(text("SELECT id FROM public.async_jobs LIMIT 1"))
                assert _sqlstate(denied_select.value) == "42501"
                await api.rollback()
                with pytest.raises(DBAPIError) as denied_update:
                    await api.execute(
                        text(
                            "UPDATE public.async_jobs SET progress_pct = 0 WHERE false"
                        )
                    )
                assert _sqlstate(denied_update.value) == "42501"
                await api.rollback()

            record_property(
                "jobs_domain_queries_api_role",
                "five native functions callable; private async_jobs SELECT/UPDATE denied",
            )

            node = shutil.which("node")
            assert node is not None
            root = Path(__file__).resolve().parents[3]
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "JOBS_DOMAIN_QUERY_DATABASE_URL": api_url.set(
                    drivername="postgresql"
                ).render_as_string(hide_password=False),
                "JOBS_DOMAIN_QUERY_EXPECTED_ROLE": "identity_api_consumer",
                "JOBS_DOMAIN_QUERY_EXPECTED_DATABASE": api_url.database,
                "JOBS_DOMAIN_QUERY_EXPECTED_HOST": api_url.host.lower(),
                "JOBS_DOMAIN_QUERY_EXPECTED_PORT": str(api_url.port or 5432),
                "JOBS_DOMAIN_QUERY_ACTOR_ID": str(world["actor_id"]),
                "JOBS_DOMAIN_QUERY_SINGLEFLIGHT_KEY": world["singleflight_key"],
                "JOBS_DOMAIN_QUERY_SCENE_ID": world["scene_id"],
                "JOBS_DOMAIN_QUERY_EXPECTED_ACTIVE_ID": str(world["job_ids"][1]),
                "JOBS_DOMAIN_QUERY_EXPECTED_PAGE_IDS": json.dumps(
                    [str(world["job_ids"][index]) for index in (8, 7, 6, 3, 2, 1, 0)]
                ),
                "JOBS_DOMAIN_QUERY_PROJECT_IDS": json.dumps(
                    [
                        str(world["project_id"]),
                        str(world["project_id"]),
                        str(world["other_project_id"]),
                    ]
                ),
                "JOBS_DOMAIN_QUERY_EXPECTED_BATCH_TIMES": json.dumps(
                    {
                        str(project_id): created_at.isoformat()
                        for project_id, created_at in world["batch_times"].items()
                    }
                ),
            }
            harness = root / "apps/jobs/conformance/node/domain_query_acceptance.mjs"
            result = await asyncio.to_thread(
                subprocess.run,
                [node, str(harness), "--live"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stderr
            receipt = json.loads(result.stdout.strip().splitlines()[-1])
            assert receipt == {
                "status": "passed",
                "functions": 5,
                "rowColumns": 15,
                "rows": 7,
            }
            record_property("jobs_domain_queries_node_consumer", json.dumps(receipt))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_singleflight_lock_matches_legacy_namespace_and_transaction_lifetime(
    test_engine,
):
    owner_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    competing_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = uuid.uuid4()
    singleflight_key = "锁命名空间-🚙"
    advisory_key = f"aap:cross-frame-singleflight:{actor_id}:{singleflight_key}"
    owner = owner_sessions()
    competitor = competing_sessions()
    try:
        pending = User(
            id=actor_id,
            email=f"jobs-lock-{uuid.uuid4().hex}@test.local",
            name="Pending lock test actor",
            password_hash="not-used",
            role="employee",
            is_active=True,
        )
        owner.add(pending)
        await _adapter(owner).lock_cross_frame_singleflight(actor_id, singleflight_key)
        assert pending in owner.new

        acquired = (
            await competitor.execute(
                text(
                    "SELECT pg_catalog.pg_try_advisory_xact_lock("
                    "pg_catalog.hashtextextended(:advisory_key, 0))"
                ),
                {"advisory_key": advisory_key},
            )
        ).scalar_one()
        assert acquired is False

        await owner.rollback()
        acquired_after_owner_rollback = (
            await competitor.execute(
                text(
                    "SELECT pg_catalog.pg_try_advisory_xact_lock("
                    "pg_catalog.hashtextextended(:advisory_key, 0))"
                ),
                {"advisory_key": advisory_key},
            )
        ).scalar_one()
        assert acquired_after_owner_rollback is True
    finally:
        await competitor.rollback()
        await owner.rollback()
        await competitor.close()
        await owner.close()
