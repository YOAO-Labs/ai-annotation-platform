"""Independent transactions fence concurrent observations and retained schema."""

import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.services import storage_job_progress as progress
from tests.test_storage_job_progress import observe, public_job, running


async def test_independent_concurrent_transactions_increment_under_job_lock(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        job = await running(db)
        job_id, task_id = job.id, job.celery_task_id
        await db.commit()

    async def callback(marker):
        async with maker() as db:
            current = await db.get(AsyncJob, job_id)
            changed = await observe(db, current, marker=marker)
            await db.commit()
            return changed

    try:
        assert await asyncio.gather(callback("a"), callback("b")) == [True, True]
        async with maker() as db:
            current = await db.get(AsyncJob, job_id)
            assert current.progress_observation["sequence"] == 2
            assert current.progress_observation["execution_id"] == str(
                progress.execution_id(job_id, task_id)
            )
    finally:
        async with maker() as db:
            await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
            await db.commit()


async def test_new_task_does_not_inherit_or_accept_previous_execution(db_session):
    job = await running(db_session)
    old_task = job.celery_task_id
    assert await observe(db_session, job, sequence=10)
    old = copy.deepcopy(job.progress_observation)
    job.celery_task_id = str(uuid.uuid4())
    await db_session.flush()
    assert progress.public(
        job.id,
        job.kind,
        job.celery_task_id,
        job.last_progress_at,
        old,
        observed_at=job.last_progress_at,
    ) == (None, None)
    assert not await observe(
        db_session, job, task_id=old_task, marker="late", sequence=11
    )
    assert await observe(db_session, job, marker="new-task", sequence=1)
    assert job.progress_observation["sequence"] == 1
    assert job.progress_observation["execution_id"] != old["execution_id"]


def test_other_job_cannot_reuse_even_same_task_observation():
    job = public_job()
    assert not progress.valid(
        job.progress_observation, uuid.uuid4(), job.kind, job.celery_task_id
    )


@pytest.mark.parametrize("field", ["last_progress_at", "progress_observation"])
async def test_downgrade_preserves_any_new_observation_fact(db_session, field):
    from datetime import datetime, timezone

    job = await running(db_session)
    # The retained fact is written as the relation owner: an application flush
    # cannot reach these columns, and the downgrade guard must still protect
    # any stored value even when it is not a valid observation.
    if field == "last_progress_at":
        await db_session.execute(
            text(
                "UPDATE public.async_jobs SET last_progress_at = :value "
                "WHERE id = :job_id"
            ),
            {"value": datetime.now(timezone.utc), "job_id": job.id},
        )
    else:
        await db_session.execute(
            text(
                "UPDATE public.async_jobs SET progress_observation = "
                "CAST(:value AS pg_catalog.jsonb) WHERE id = :job_id"
            ),
            {"value": json.dumps({"retained": True}), "job_id": job.id},
        )
    path = Path("alembic/versions/0259_storage_job_progress.py")
    spec = importlib.util.spec_from_file_location("progress_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(session):
        with Operations.context(MigrationContext.configure(session.connection())):
            module.downgrade()

    with pytest.raises(RuntimeError, match="keep schema"):
        await db_session.run_sync(downgrade)
    # The rejection precedes DDL, and the transaction remains usable.
    assert (
        await db_session.execute(
            select(AsyncJob.last_progress_at, AsyncJob.progress_observation).where(
                AsyncJob.id == job.id
            )
        )
    ).one()


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned API role and disposable database",
)
async def test_restricted_api_records_progress_without_private_relation_rights(
    db_session,
):
    job = await running(db_session)
    job_id, task_id = job.id, job.celery_task_id
    await db_session.flush()
    db_session.expunge(job)

    grants = (
        await db_session.execute(
            text(
                "SELECT pg_catalog.has_function_privilege("
                "'identity_api_consumer', "
                "'aap_jobs_v1.read_job_progress_for_update(uuid,text,text)'"
                "::regprocedure, 'EXECUTE'), "
                "pg_catalog.has_function_privilege("
                "'identity_api_consumer', "
                "'aap_jobs_v1.record_job_progress(uuid,text,text,"
                "timestamp with time zone,jsonb)'::regprocedure, 'EXECUTE')"
            )
        )
    ).one()
    assert grants == (True, True)

    # The owner test transaction restores this ACL change during fixture rollback.
    await db_session.execute(
        text("REVOKE SELECT, UPDATE ON public.async_jobs FROM identity_api_consumer")
    )
    await db_session.execute(text("SET LOCAL ROLE identity_api_consumer"))
    try:
        rights = (
            await db_session.execute(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(current_user, "
                    "'public.async_jobs', 'SELECT'), "
                    "pg_catalog.has_any_column_privilege(current_user, "
                    "'public.async_jobs', 'UPDATE')"
                )
            )
        ).one()
        assert rights == (False, False)
        assert await progress.record(
            db_session,
            job,
            task_id=task_id,
            stage="enumerating",
            unit="pages",
            completed=3,
            marker="restricted-page",
            sequence=1,
        )
    finally:
        await db_session.execute(text("RESET ROLE"))

    last_at, observation = (
        await db_session.execute(
            text(
                "SELECT last_progress_at, progress_observation "
                "FROM aap_jobs_v1.read_job_progress("
                "CAST(:job_id AS pg_catalog.uuid))"
            ),
            {"job_id": job_id},
        )
    ).one()
    assert last_at is not None
    assert observation["completed"] == 3 and observation["sequence"] == 1


@pytest.mark.parametrize(
    ("signature", "returns_set"),
    [
        ("aap_jobs_v1.read_job_progress_for_update(uuid,text,text)", True),
        (
            "aap_jobs_v1.record_job_progress(uuid,text,text,"
            "timestamp with time zone,jsonb)",
            False,
        ),
    ],
)
async def test_progress_owner_functions_keep_fixed_definer_and_signature(
    db_session, signature, returns_set
):
    metadata = (
        (
            await db_session.execute(
                text(
                    "SELECT p.prosecdef AS security_definer, "
                    "p.provolatile::text AS volatility, "
                    "p.proconfig AS settings, "
                    "p.proretset AS returns_set, "
                    "EXISTS (SELECT 1 FROM pg_catalog.aclexplode("
                    "COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) acl "
                    "WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
                    "AS public_execute "
                    "FROM pg_catalog.pg_proc AS p "
                    "WHERE p.oid = pg_catalog.to_regprocedure("
                    "CAST(:signature AS pg_catalog.text))"
                ),
                {"signature": signature},
            )
        )
        .mappings()
        .one()
    )
    assert metadata["security_definer"] is True
    assert metadata["volatility"] == "v"
    assert metadata["settings"] == ["search_path=pg_catalog, pg_temp"]
    assert metadata["returns_set"] is returns_set
    assert metadata["public_execute"] is False
