"""Independent transactions racing across the unlocked snapshot boundary."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

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


@pytest.mark.parametrize(
    "mode",
    [
        "content",
        "revalidate",
        "relocate",
        "relocate_two_targets",
        "relocate_two_origins",
    ],
)
async def test_concurrent_confirmations_share_or_compete_for_one_revision(
    test_engine, tmp_path, monkeypatch, mode
):
    revalidate = mode == "revalidate"
    relocate = mode.startswith("relocate")
    competing = mode in {"relocate_two_targets", "relocate_two_origins"}
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
        if competing:
            (world[0] / "second.png").write_bytes(world[1])
            if mode == "relocate_two_origins":
                from tests.test_dataset_source_changes import rescan
                from app.services import dataset_scans

                world = await rescan(db, world)
                await register_entry(
                    db, world[6].id, dataset_scans.path_digest("second.png"), actor
                )
                (world[0] / "second.png").unlink()
        if relocate:
            from tests.test_dataset_source_relocations import moved_candidate

            world, run, _, change, payload = await moved_candidate(db, world)
        else:
            world, run, change, payload = await candidate(
                db, world, "red" if revalidate else "blue"
            )
        other_digest, other_payload = change["path_digest"], payload
        if competing:
            from app.services import dataset_source_changes

            page = await dataset_source_changes.page(
                db,
                run,
                "added" if mode == "relocate_two_targets" else "not_seen",
                20,
                0,
            )
            if mode == "relocate_two_targets":
                other_digest = next(
                    c["path_digest"]
                    for c in page["items"]
                    if c["path_digest"] != change["path_digest"]
                )
            else:
                other = next(
                    c for c in page["items"] if c["relative_path"] == "second.png"
                )
                other_payload = payload.model_copy(
                    update={"before_revision_id": other["before_revision_id"]}
                )
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

        async def accept(other=False):
            async with maker() as db:
                actor = await db.get(User, actor_id)
                result = await revisions.accept(
                    db,
                    run_id,
                    other_digest if other else change["path_digest"],
                    other_payload if other else payload,
                    actor,
                    revalidate=revalidate,
                    relocate=relocate,
                )
                await db.commit()
                return result

        results = await asyncio.wait_for(
            asyncio.gather(accept(), accept(True), return_exceptions=True), timeout=15
        )
        if competing:
            failures = [r for r in results if isinstance(r, BaseException)]
            successes = [r for r in results if isinstance(r, dict)]
            assert len(failures) == len(successes) == 1
            assert (
                isinstance(failures[0], HTTPException)
                and failures[0].status_code == 409
            )
            assert failures[0].detail["code"] in {
                "source_revision_changed",
                "relocation_target_unavailable",
                "revision_selection_changed",
            }
            assert not successes[0]["created"]
        else:
            first, second = results
            assert isinstance(first, dict) and isinstance(second, dict), results
            assert first["revision_id"] == second["revision_id"]
            assert first["item_id"] == second["item_id"]
            assert sum(r["created"] for r in (first, second)) == (
                0 if mode != "content" else 1
            )
        async with maker() as db:
            assert await db.scalar(
                select(func.count())
                .select_from(DatasetItem)
                .where(DatasetItem.dataset_id == dataset_id)
            ) == (2 if mode in {"content", "relocate_two_origins"} else 1)
            assert await db.scalar(
                select(func.count())
                .select_from(DatasetSourceRevision)
                .where(DatasetSourceRevision.source_id == source_id)
            ) == (3 if mode == "relocate_two_origins" else 2)
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
