"""Known-owner GC honors business references, publications and UUID permits."""

from datetime import timedelta
import io
import uuid

import pytest
from sqlalchemy import delete, select, text

from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.db.models.user import User
from app.services import raster_mask_objects as owners, storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.test_raster_mask_objects import harness as harness
from tests.factory import create_project, create_task, create_user


async def ready(harness):
    _, reference, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        assert await owners.bind_publications(caller, reference) == 1
        await caller.commit()
    return reference, data, (await harness.rows())[-1]


def age(monkeypatch):
    clock = owners.now() + timedelta(hours=25)
    monkeypatch.setattr(owners, "now", lambda: clock)
    return clock


async def test_gc_dry_run_is_read_only_and_retirement_ends_first_round(
    harness, monkeypatch
):
    _, _, row = await ready(harness)
    before = (row.state, row.last_attempted_at, row.retired_at)
    age(monkeypatch)
    async with harness.factory() as db:
        result = await owners.cleanup(db, dry_run=True, storage=harness.blobs)
        assert result["eligible"] == 1 and result["deleted"] == result["retired"] == 0
        row = (await harness.rows())[0]
        assert (row.state, row.last_attempted_at, row.retired_at) == before
        result = await owners.cleanup(db, storage=harness.blobs)
        assert result["retired"] == 1 and result["deleted"] == 0
        assert not harness.blobs.deleted
        assert (await harness.rows())[0].state == "retired"
        result = await owners.cleanup(db, storage=harness.blobs)
        assert result["deleted"] == 1 and not await harness.rows()
        assert len(harness.blobs.deleted) == 1
        assert not await harness.operations()
        assert all(body.closed for body in harness.blobs.bodies)


async def test_last_used_grace_blocks_gc_even_after_initial_publication_ages(
    harness, monkeypatch
):
    reference, data, _ = await ready(harness)
    clock = age(monkeypatch)
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        await owners.bind_publications(caller, reference)
        await caller.commit()
        result = await owners.cleanup(caller, storage=harness.blobs)
    row = (await harness.rows())[0]
    assert row.last_used_at == clock and row.state == "ready"
    assert result["eligible"] == result["retired"] == result["deleted"] == 0


async def test_exact_business_reference_and_publication_each_block_retirement(
    harness, monkeypatch
):
    reference, _, _ = await ready(harness)
    harness.references.add(reference["object_key"])
    age(monkeypatch)
    async with harness.factory() as db:
        result = await owners.cleanup(db, storage=harness.blobs)
        assert result["referenced"] == 1 and result["retired"] == 0
        harness.references.clear()
        # An independently committed caller guard remains effective even if the
        # caller performs an unrelated progress commit and then rolls back.
        _, second, data = harness.content()
        await owners.store(second, data, db=db, storage=harness.blobs)
        await db.commit()
        await db.rollback()
        clock = owners.now() + timedelta(hours=25)
        monkeypatch.setattr(owners, "now", lambda: clock)
        result = await owners.cleanup(db, storage=harness.blobs)
        assert result["retired"] == 1
        rows = {row.logical_key: row for row in await harness.rows()}
        assert rows[second["object_key"]].state == "ready"
        assert rows[reference["object_key"]].state == "retired"


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
async def test_exact_uuid_operation_blocks_both_retirement_and_delete(
    harness, monkeypatch, operation
):
    _, _, row = await ready(harness)
    age(monkeypatch)
    async with harness.factory() as db:
        async with await storage_access.acquire(
            db, LEGACY_LOCATION_ID, owners.purpose(operation, row.id)
        ):
            result = await owners.cleanup(db, storage=harness.blobs)
            assert result["eligible"] == result["retired"] == result["deleted"] == 0
        assert (await owners.cleanup(db, storage=harness.blobs))["retired"] == 1
        async with await storage_access.acquire(
            db, LEGACY_LOCATION_ID, owners.purpose(operation, row.id)
        ):
            result = await owners.cleanup(db, storage=harness.blobs)
            assert result["eligible"] == result["deleted"] == 0
        assert (await owners.cleanup(db, storage=harness.blobs))["deleted"] == 1


async def test_pending_failure_never_becomes_gc_candidate(harness, monkeypatch):
    _, reference, data = harness.content()
    harness.blobs.fail_put = True
    async with harness.factory() as db:
        with pytest.raises(ValueError):
            await owners.store(reference, data, db=db, storage=harness.blobs)
        clock = owners.now() + timedelta(days=365)
        monkeypatch.setattr(owners, "now", lambda: clock)
        result = await owners.cleanup(db, storage=harness.blobs)
    assert result["scanned"] == result["eligible"] == result["deleted"] == 0
    assert (await harness.rows())[0].state == "pending"
    assert len(await harness.guards()) == 1


async def test_content_lock_conflict_does_not_wait_or_retire(harness, monkeypatch):
    reference, _, _ = await ready(harness)
    age(monkeypatch)
    async with harness.factory() as caller:
        await caller.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "aap:raster-mask:" + reference["object_key"]},
        )
        result = await owners.cleanup(caller, storage=harness.blobs)
        assert result["retained"] == 1 and result["retired"] == result["deleted"] == 0
        assert (await harness.rows())[0].state == "ready"
        await caller.rollback()


async def test_retained_first_page_cannot_starve_later_eligible_owner(
    harness, monkeypatch
):
    _, guarded, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(guarded, data, db=caller, storage=harness.blobs)
    reference, _, _ = await ready(harness)
    age(monkeypatch)
    async with harness.factory() as db:
        first = await owners.cleanup(db, limit=1, storage=harness.blobs)
        assert first["scanned"] == first["retained"] == 1 and first["retired"] == 0
        second = await owners.cleanup(db, limit=1, storage=harness.blobs)
        assert second["retired"] == 1
        rows = {row.logical_key: row for row in await harness.rows()}
        assert rows[guarded["object_key"]].state == "ready"
        assert rows[guarded["object_key"]].last_attempted_at is not None
        assert rows[reference["object_key"]].state == "retired"


@pytest.fixture
async def quota_task(harness):
    async with harness.factory() as db:
        user = await create_user(
            db,
            "super_admin",
            f"raster-quota-{uuid.uuid4().hex}@test.local",
            "Raster quota owner",
        )
        project = await create_project(db, owner_id=user.id)
        task = await create_task(db, project_id=project.id)
        user_id, project_id, task_id = user.id, project.id, task.id
        await db.commit()
    try:
        yield task_id
    finally:
        async with harness.factory() as db:
            await db.execute(
                delete(RasterMaskUpload).where(RasterMaskUpload.task_id == task_id)
            )
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_gc_expires_only_old_unlinked_reservations_without_guessing_legacy_bytes(
    harness, quota_task
):
    current = owners.now()
    identifiers = [uuid.uuid4() for _ in range(3)]
    keys = [
        f"raster-masks/sha256/legacy-{identifier.hex}.json"
        for identifier in identifiers
    ]
    for key in keys:
        harness.blobs.objects[harness.blobs.bucket, key] = b"unknown legacy original"
    async with harness.factory() as db:
        db.add_all(
            [
                RasterMaskUpload(
                    id=identifiers[0],
                    task_id=quota_task,
                    object_key=keys[0],
                    created_at=current - timedelta(hours=25),
                ),
                RasterMaskUpload(
                    id=identifiers[1],
                    task_id=quota_task,
                    object_key=keys[1],
                    created_at=current - timedelta(hours=25),
                    linked_at=current - timedelta(hours=24),
                ),
                RasterMaskUpload(
                    id=identifiers[2],
                    task_id=quota_task,
                    object_key=keys[2],
                    created_at=current,
                ),
            ]
        )
        await db.commit()
        result = await owners.cleanup(db, dry_run=True, storage=harness.blobs)
        assert result["expired_uploads"] == 0
        assert (
            len(
                list(
                    await db.scalars(
                        select(RasterMaskUpload.id).where(
                            RasterMaskUpload.task_id == quota_task
                        )
                    )
                )
            )
            == 3
        )
        await db.rollback()
        result = await owners.cleanup(db, storage=harness.blobs)
        assert result["expired_uploads"] == 1
        remaining = set(
            await db.scalars(
                select(RasterMaskUpload.id).where(
                    RasterMaskUpload.task_id == quota_task
                )
            )
        )
        assert remaining == set(identifiers[1:])
    assert not harness.blobs.reads and not harness.blobs.deleted
    assert all((harness.blobs.bucket, key) in harness.blobs.objects for key in keys)


async def test_publish_lock_conflict_is_bounded_and_keeps_owner(harness, monkeypatch):
    reference, _, _ = await ready(harness)
    age(monkeypatch)
    async with harness.factory() as caller:
        await caller.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "aap:rle-publish:" + reference["object_key"]},
        )
        result = await owners.cleanup(caller, storage=harness.blobs)
        assert result["errors"] == 1 and result["retired"] == result["deleted"] == 0
        assert (await harness.rows())[0].state == "ready"
        await caller.rollback()


async def test_retired_uuid_cleanup_cannot_delete_new_generation_shared_logical_key(
    harness, monkeypatch
):
    reference, data, old = await ready(harness)
    age(monkeypatch)
    async with harness.factory() as db:
        assert (await owners.cleanup(db, storage=harness.blobs))["retired"] == 1
        await owners.store(reference, data, db=db, storage=harness.blobs)
        await owners.bind_publications(db, reference)
        await db.commit()
        rows = await harness.rows()
        current = next(row for row in rows if row.state == "ready")
        assert current.id != old.id
        assert current.storage_ref["object_key"] != old.storage_ref["object_key"]
        harness.references.add(reference["object_key"])
        result = await owners.cleanup(db, storage=harness.blobs)
        assert result["deleted"] == 1 and result["retired"] == 0
        assert [row.id for row in await harness.rows()] == [current.id]
        assert (
            current.storage_ref["bucket"],
            current.storage_ref["object_key"],
        ) in harness.blobs.objects
        assert harness.blobs.deleted == [
            (old.storage_ref["bucket"], old.storage_ref["object_key"])
        ]


async def test_retired_owner_without_new_owner_retains_new_logical_reference(
    harness, monkeypatch
):
    reference, _, _ = await ready(harness)
    age(monkeypatch)
    async with harness.factory() as db:
        assert (await owners.cleanup(db, storage=harness.blobs))["retired"] == 1
        harness.references.add(reference["object_key"])
        result = await owners.cleanup(db, storage=harness.blobs)
        assert result["eligible"] == result["deleted"] == 0
        assert (await harness.rows())[0].state == "retired"
        assert not harness.blobs.deleted


async def test_delete_failure_and_unknown_close_keep_retired_receipt(
    harness, monkeypatch
):
    _, _, row = await ready(harness)
    age(monkeypatch)
    async with harness.factory() as db:
        assert (await owners.cleanup(db, storage=harness.blobs))["retired"] == 1
        harness.blobs.fail_delete = True
        assert (await owners.cleanup(db, storage=harness.blobs))["errors"] == 1
        assert (await harness.rows())[0].state == "retired"
        assert not await harness.operations()
        harness.blobs.fail_delete = False

        class Unclosed(io.BytesIO):
            def close(self):
                raise OSError("private GC close failure")

        harness.blobs.body_factory = Unclosed
        assert (await owners.cleanup(db, storage=harness.blobs))["errors"] == 1
        assert (await harness.rows())[0].id == row.id
        operations = await harness.operations()
        assert len(operations) == 1 and operations[0].purpose == owners.purpose(
            "delete", row.id
        )
        assert not harness.blobs.deleted


async def test_gc_rechecks_reference_after_scan_before_retirement(harness, monkeypatch):
    reference, _, _ = await ready(harness)
    age(monkeypatch)
    original = owners._gc_one

    async def gc_one(*args):
        harness.references.add(reference["object_key"])
        return await original(*args)

    monkeypatch.setattr(owners, "_gc_one", gc_one)
    async with harness.factory() as db:
        result = await owners.cleanup(db, storage=harness.blobs)
    assert result["eligible"] == 1 and result["retained"] == 1
    assert (await harness.rows())[0].state == "ready" and not harness.blobs.deleted


async def test_stopped_gate_prevents_retirement_and_physical_delete(
    harness, monkeypatch
):
    await ready(harness)
    age(monkeypatch)
    async with harness.factory() as db:
        gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
        gate.state, gate.revision = "stopped", gate.revision + 1
        await db.commit()
        result = await owners.cleanup(db, storage=harness.blobs)
    assert result["errors"] == 1 and result["retired"] == result["deleted"] == 0
    assert (await harness.rows())[0].state == "ready"
    assert not harness.blobs.deleted


async def test_gc_does_not_commit_callers_unrelated_transaction(harness, monkeypatch):
    await ready(harness)
    age(monkeypatch)
    async with harness.factory() as caller:
        await caller.execute(
            text("CREATE TEMP TABLE raster_gc_sentinel (value int) ON COMMIT DROP")
        )
        await caller.execute(text("INSERT INTO raster_gc_sentinel VALUES (9)"))
        assert (await owners.cleanup(caller, storage=harness.blobs))["retired"] == 1
        assert await caller.scalar(text("SELECT value FROM raster_gc_sentinel")) == 9
        await caller.rollback()
