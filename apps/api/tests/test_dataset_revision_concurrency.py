"""Independent transactions racing across the unlocked snapshot boundary."""

import asyncio
import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.user import User
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import (
    DatasetSource,
    DatasetScanRun,
    DatasetSourceRevision,
    DatasetSourceChange,
    DatasetRegistrationRun,
)
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.audit_log import AuditLog
from app.services import content_snapshot as snapshots
from app.services import dataset_source_revisions as revisions
from app.services.dataset_references import register_entry
from tests.test_dataset_content import content_world
from tests.test_dataset_source_revisions import candidate


async def test_concurrent_identical_confirmations_return_one_revision(
    test_engine, tmp_path, monkeypatch
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        actor = User(
            id=uuid.uuid4(),
            email=f"revision-{uuid.uuid4().hex}@example.test",
            name="Revision race",
            role="super_admin",
            password_hash="test-only",
            is_active=True,
        )
        db.add(actor)
        await db.commit()
        world = await content_world.__wrapped__(
            db, (actor, None), tmp_path, monkeypatch
        )
        await register_entry(db, world[6].id, world[7].path_digest, actor)
        world, run, change, payload = await candidate(db, world)
        actor_id, source_id, dataset_id, location_id, run_id = (
            actor.id,
            world[5].id,
            world[3].id,
            world[4].id,
            run.id,
        )
        await db.commit()
    try:
        ready = asyncio.Event()
        count = 0
        original = snapshots.capture

        async def capture(*args, **kwargs):
            nonlocal count
            snapshot = await original(*args, **kwargs)
            count += 1
            if count <= 2:
                if count == 2:
                    ready.set()
                await ready.wait()
            return snapshot

        monkeypatch.setattr(snapshots, "capture", capture)

        async def accept():
            async with maker() as db:
                actor = await db.get(User, actor_id)
                result = await revisions.accept(
                    db, run_id, change["path_digest"], payload, actor
                )
                await db.commit()
                return result

        first, second = await asyncio.wait_for(
            asyncio.gather(accept(), accept()), timeout=15
        )
        assert first["revision_id"] == second["revision_id"]
        assert first["item_id"] == second["item_id"]
        assert sum(r["created"] for r in (first, second)) == 1
        async with maker() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(DatasetItem)
                    .where(DatasetItem.dataset_id == dataset_id)
                )
                == 2
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(DatasetSourceRevision)
                    .where(DatasetSourceRevision.source_id == source_id)
                )
                == 2
            )
    finally:
        async with maker() as db:
            runs = select(DatasetRegistrationRun.id).where(
                DatasetRegistrationRun.source_id == source_id
            )
            await db.execute(
                delete(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
            )
            await db.execute(
                delete(DatasetSourceChange).where(DatasetSourceChange.run_id.in_(runs))
            )
            await db.execute(
                delete(DatasetSourceRevision).where(
                    DatasetSourceRevision.source_id == source_id
                )
            )
            await db.execute(
                delete(DatasetRegistrationRun).where(
                    DatasetRegistrationRun.source_id == source_id
                )
            )
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
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(delete(AuditLog).where(AuditLog.actor_id == actor_id))
            await db.execute(delete(User).where(User.id == actor_id))
            await db.commit()
