"""Exercise bounded Jobs reads with the three actual restricted runtime logins."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.async_job_contracts import AsyncJobOperationFailure
from app.application.async_job_read import SqlAsyncJobReadAdapter
from app.db.models.dataset import Dataset
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import DatasetScanRun, DatasetSource
from app.services import async_job
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
from app.services.jobs_lifecycle_data import job_lock_statement
from tests.factory import create_user
from tests.test_workspace_project_authority_locking import _api_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable verifier and actual runtime logins",
    ),
]


@asynccontextmanager
async def _directory(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    nonce = uuid4().hex
    run_id = uuid4()
    async with sessions.begin() as owner:
        actor = await create_user(
            owner,
            "project_admin",
            f"jobs-owner-{nonce}@example.test",
            "Directory owner",
        )
        outsider = await create_user(
            owner, "project_admin", f"jobs-outside-{nonce}@example.test", "Outsider"
        )
        dataset = Dataset(
            name="Job scope", display_id=f"J-{nonce[:10]}", created_by=actor.id
        )
        location = StorageLocation(
            id=uuid4(),
            name=f"location-{nonce}",
            kind="filesystem",
            config={"scope": "global"},
        )
        owner.add_all([dataset, location])
        await owner.flush()
        source = DatasetSource(
            dataset_id=dataset.id, location_id=location.id, name="directory", config={}
        )
        owner.add(source)
        await owner.flush()
        payload = {"dataset_id": str(dataset.id), "run_id": str(run_id)}
        current = await async_job.create_job(
            owner, kind="dataset_scan", payload=payload
        )
        old = await async_job.create_job(owner, kind="dataset_scan", payload=payload)
        malformed = await async_job.create_job(
            owner, kind="dataset_scan", payload={**payload, "run_id": "not-a-uuid"}
        )
        owner.add(
            DatasetScanRun(
                id=run_id, source_id=source.id, async_job_id=current.id, snapshot={}
            )
        )
        await async_job.mark_failed(owner, old.id, error="attempt superseded")
    try:
        yield (
            actor.id,
            outsider.id,
            dataset.id,
            location.id,
            current.id,
            old.id,
            malformed.id,
        )
    finally:
        async with sessions.begin() as owner:
            await owner.execute(
                text("DELETE FROM public.dataset_scan_runs WHERE id=:id"),
                {"id": run_id},
            )
            await owner.execute(
                text(
                    "DELETE FROM public.async_jobs WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [current.id, old.id, malformed.id]},
            )
            await owner.execute(
                text("DELETE FROM public.dataset_sources WHERE id=:id"),
                {"id": source.id},
            )
            await owner.execute(
                text("DELETE FROM public.storage_locations WHERE id=:id"),
                {"id": location.id},
            )
            await owner.execute(
                text("DELETE FROM public.datasets WHERE id=:id"), {"id": dataset.id}
            )
            await owner.execute(
                text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": [actor.id, outsider.id]},
            )


@pytest.mark.parametrize(
    ("selector", "role"),
    [
        (
            "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL",
            "identity_embedded_api_consumer",
        ),
        ("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer"),
        ("AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime"),
    ],
    ids=("lan-embedded", "lan-isolated-issuer", "worktree"),
)
async def test_jobs_private_reads_and_directory_scope(test_engine, selector, role):
    verified = _api_url(test_engine)
    url = make_url(os.environ[selector])
    assert (url.host, url.port, url.database, url.username) == (
        verified.host,
        verified.port,
        verified.database,
        role,
    )
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with _directory(test_engine) as world:
            actor, outsider, dataset, location, current, old, malformed = world
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                assert await db.scalar(text("SELECT SESSION_USER")) == role
                denied_sql = (
                    (
                        "SELECT id FROM public.async_jobs WHERE id=:job",
                        {"job": current},
                    ),
                    (
                        "UPDATE public.async_jobs SET status='failed' WHERE id=:job",
                        {"job": current},
                    ),
                    (
                        "SELECT yoao_jobs_v1._directory_job_ids(:actor,:location)",
                        {"actor": actor, "location": location},
                    ),
                    (
                        "SELECT yoao_jobs_v1.dataset_active_job_facts(:dataset)",
                        {"dataset": dataset},
                    ),
                    ("SELECT yoao_jobs_v1.claim_dispatches('{}'::jsonb)", {}),
                    (
                        "SELECT yoao_jobs_v1.begin_dispatch_publication(NULL,NULL,NULL,NULL,NULL)",
                        {},
                    ),
                    (
                        "SELECT yoao_jobs_v1.settle_dataset_scan_job("
                        ":job,NULL,'failed',:stamp,NULL,'{}'::jsonb)",
                        {"job": current, "stamp": datetime.now(timezone.utc)},
                    ),
                )
                for sql, params in denied_sql:
                    with pytest.raises(DBAPIError) as denied:
                        async with db.begin_nested():
                            await db.execute(text(sql), params)
                    assert denied.value.orig.sqlstate == "42501", sql

                data = JobsDomainQueryPostgresData(db)
                assert await data.directory_job_total(actor, location) == 2
                assert await data.directory_job_total(outsider, location) == 0
                expected = sorted([current, old])
                for offset, identifier in enumerate(expected):
                    page = await data.directory_job_page(actor, location, 1, offset)
                    (row,) = page
                    assert row["id"] == identifier
                    assert set(row) == {
                        "id",
                        "kind",
                        "status",
                        "created_at",
                        "completed_at",
                    }
                assert not await data.directory_job_page(actor, location, 1, 2)
                adapter = SqlAsyncJobReadAdapter(db)
                assert (await adapter.get_job(job_id=old, actor_id=actor)).id == old
                for job_id, reader in ((old, outsider), (malformed, actor)):
                    with pytest.raises(AsyncJobOperationFailure) as rejected:
                        await adapter.get_job(job_id=job_id, actor_id=reader)
                    assert rejected.value.status_code == 403
                counts = (
                    await db.execute(
                        text(
                            "SELECT kind, status, count "
                            "FROM yoao_jobs_v1.active_storage_writer_job_counts()"
                        )
                    )
                ).all()
                assert any(
                    kind == "dataset_scan" and status == "pending" and count >= 2
                    for kind, status, count in counts
                )

                row = await async_job.get_job(db, current)
                await db.refresh(row, attribute_names=["progress_pct"])
                assert row.progress_pct == 0
                share_lock = job_lock_statement(current, mode="share_nowait")
                update_lock = job_lock_statement(current, mode="update_nowait")
                async with db.begin_nested() as fence:
                    assert await db.scalar(share_lock) is row
                    async with test_engine.connect() as observer:
                        with pytest.raises(DBAPIError) as busy:
                            await observer.execute(
                                text(
                                    "SELECT id FROM public.async_jobs "
                                    "WHERE id=:job FOR UPDATE NOWAIT"
                                ),
                                {"job": current},
                            )
                        assert busy.value.orig.sqlstate == "55P03"
                    await fence.rollback()
                assert await db.scalar(update_lock) is row
    finally:
        await engine.dispose()
