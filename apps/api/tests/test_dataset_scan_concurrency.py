import asyncio
from datetime import datetime, timezone
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import DatasetSource, DatasetScanRun
from app.db.models.notification import Notification
from app.db.models.user import User
from app.schemas.dataset_source import DatasetSourceCreate
from app.schemas.storage_location import FilesystemLocationCreate
from app.services import (
    dataset_scans as svc,
    storage_location as locations,
    filesystem_storage as fs,
)


async def test_retention_resume_signal_and_concurrent_start_are_serialized(
    test_engine, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(tmp_path)})
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        actor = User(
            id=uuid.uuid4(),
            email=f"race-{uuid.uuid4().hex}@example.test",
            name="Race",
            role="super_admin",
            password_hash="test-only",
            is_active=True,
        )
        db.add(actor)
        await db.flush()
        dataset = Dataset(
            display_id=f"ds-{uuid.uuid4().hex[:12]}", name="Race", created_by=actor.id
        )
        db.add(dataset)
        await db.flush()
        location = await locations.create_filesystem(
            db,
            FilesystemLocationCreate(
                name=f"race-{uuid.uuid4().hex}", root_handle="test"
            ),
        )
        await locations.store_probe(
            db,
            location,
            "worker:scan@test",
            fs.probe_directory(location.config),
            datetime.now(timezone.utc),
        )
        source = await svc.create_source(
            db,
            DatasetSourceCreate(
                dataset_id=dataset.id,
                location_id=location.id,
                name="Race",
                node_id="scan@test",
            ),
            actor,
        )
        run, _ = await svc.start_scan(db, source.id, actor)
        old_job_id = run.async_job_id
        job = await db.get(AsyncJob, old_job_id)
        await svc.finish(db, run, job, "failed", "worker_interrupted")
        actor_id, source_id, run_id, dataset_id, location_id = (
            actor.id,
            source.id,
            run.id,
            dataset.id,
            location.id,
        )
        await db.commit()
    try:
        job_locked, source_locked = asyncio.Event(), asyncio.Event()

        async def purge():
            async with maker() as db:
                await db.scalar(
                    select(AsyncJob).where(AsyncJob.id == old_job_id).with_for_update()
                )
                job_locked.set()
                await source_locked.wait()
                await db.execute(delete(AsyncJob).where(AsyncJob.id == old_job_id))
                await db.commit()

        async def resume():
            await job_locked.wait()
            async with maker() as db:
                await svc.source_context(db, source_id)
                source_locked.set()
                actor = await db.get(User, actor_id)
                resumed = await svc.resume_scan(db, run_id, actor)
                assert resumed.async_job_id != old_job_id
                await db.commit()
                await svc.cancel_job(db, resumed.async_job_id, actor)
                await db.commit()

        await asyncio.wait_for(asyncio.gather(purge(), resume()), timeout=8)

        signal_read, attempt_replaced = asyncio.Event(), asyncio.Event()
        original_context = svc.source_context
        async with maker() as stale_db:
            old_attempt = await stale_db.get(DatasetScanRun, run_id)
            old_signal_job = await stale_db.get(AsyncJob, old_attempt.async_job_id)

            async def pause_stale_reader(db, source_id):
                if db is stale_db:
                    signal_read.set()
                    await attempt_replaced.wait()
                return await original_context(db, source_id)

            async def replace_attempt():
                await signal_read.wait()
                async with maker() as db:
                    actor = await db.get(User, actor_id)
                    run = await svc.resume_scan(db, run_id, actor)
                    new_job_id = run.async_job_id
                    await db.commit()
                    attempt_replaced.set()
                    return new_job_id

            async def stale_signal():
                await svc.signal_interrupted(stale_db, old_signal_job, cancelled=False)
                await stale_db.commit()

            with monkeypatch.context() as patch:
                patch.setattr(svc, "source_context", pause_stale_reader)
                _, new_job_id = await asyncio.wait_for(
                    asyncio.gather(stale_signal(), replace_attempt()), timeout=8
                )
            async with maker() as db:
                run = await db.get(DatasetScanRun, run_id)
                current_job = await db.get(AsyncJob, new_job_id)
                assert run.async_job_id == new_job_id
                assert run.status == current_job.status == "pending"
                actor = await db.get(User, actor_id)
                await svc.cancel_job(db, new_job_id, actor)
                await db.commit()

        async def start():
            async with maker() as db:
                actor = await db.get(User, actor_id)
                run, created = await svc.start_scan(db, source_id, actor)
                await db.commit()
                return run.id, created

        results = await asyncio.wait_for(asyncio.gather(start(), start()), timeout=8)
        assert (
            results[0][0] == results[1][0]
            and sum(created for _, created in results) == 1
        )
    finally:
        async with maker() as db:
            await db.execute(
                delete(DatasetScanRun).where(DatasetScanRun.source_id == source_id)
            )
            await db.execute(delete(DatasetSource).where(DatasetSource.id == source_id))
            await db.execute(
                delete(Notification).where(Notification.user_id == actor_id)
            )
            await db.execute(delete(AsyncJob).where(AsyncJob.user_id == actor_id))
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == location_id)
            )
            await db.execute(delete(User).where(User.id == actor_id))
            await db.commit()
