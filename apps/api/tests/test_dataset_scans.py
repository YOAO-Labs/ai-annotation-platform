from datetime import timedelta, datetime, timezone
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, delete

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_source import DatasetScanEntry
from app.schemas.dataset_source import DatasetSourceCreate, DatasetSourceUpdate
from app.schemas.storage_location import FilesystemLocationCreate
from app.services import (
    dataset_scans as svc,
    filesystem_storage as fs,
    storage_location as locations,
)


@pytest.fixture
async def world(db_session, super_admin, tmp_path, monkeypatch):
    root = tmp_path / "controlled"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(root)})
    actor = super_admin[0]
    dataset = Dataset(
        display_id=f"ds-{uuid.uuid4().hex[:10]}", name="Inventory", created_by=actor.id
    )
    db_session.add(dataset)
    await db_session.flush()
    location = await locations.create_filesystem(
        db_session,
        FilesystemLocationCreate(
            name=f"inventory-{uuid.uuid4().hex}", root_handle="test"
        ),
    )
    result = fs.probe_directory(location.config)
    await locations.store_probe(
        db_session, location, "worker:scan@test", result, datetime.now(timezone.utc)
    )
    source = await svc.create_source(
        db_session,
        DatasetSourceCreate(
            dataset_id=dataset.id,
            location_id=location.id,
            name="Inventory",
            node_id="scan@test",
        ),
        actor,
    )
    await db_session.flush()
    return root, actor, dataset, location, source


async def begin(db, world):
    run, created = await svc.start_scan(db, world[-1].id, world[1])
    assert created
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await svc.claim(db, run.id, job.celery_task_id, "scan@test")
    assert fence is not None
    await db.flush()
    return run, job, fence


async def next_work(db, run, job, fence):
    while True:
        work = await svc.next_directory(db, run.id, job.celery_task_id, fence)
        await db.flush()
        if work is None or not work.get("transition"):
            return work


def manifest(work):
    return fs.scan_directory(
        work["config"],
        work["path"],
        expected_anchor=work["anchor"],
        expected_identity=work["identity"],
    )


async def drain(db, run, job, fence):
    for _ in range(100):
        work = await next_work(db, run, job, fence)
        if work is None:
            return
        await svc.apply_directory(
            db, run.id, job.celery_task_id, fence, work, manifest(work)
        )
        await db.flush()
    raise AssertionError("Inventory did not terminate")


async def test_excluded_directory_is_not_traversed_and_file_patterns_apply(
    db_session, world
):
    root, _, _, _, source = world
    (root / "excluded").mkdir()
    (root / "excluded" / "hidden.png").write_bytes(b"hidden")
    (root / "keep.png").write_bytes(b"keep")
    (root / "skip.png").write_bytes(b"skip")
    source.config = {**source.config, "exclude_globs": ["excluded", "skip.*"]}
    run, job, fence = await begin(db_session, world)
    await drain(db_session, run, job, fence)
    entries = list(
        await db_session.scalars(
            select(DatasetScanEntry).where(DatasetScanEntry.run_id == run.id)
        )
    )
    states = {entry.relative_path: entry.state for entry in entries}
    assert "excluded/hidden.png" not in states
    assert states["excluded"] == states["skip.png"] == "skipped"
    assert states["keep.png"] == "observed"


async def test_recursive_inventory_is_idempotent_and_never_creates_samples(
    db_session, world
):
    root, actor, dataset, _, source = world
    (root / "子目录 空格").mkdir()
    (root / "子目录 空格" / "sample.jpg").write_bytes(b"not an image; metadata only")
    (root / "link").symlink_to("/etc", target_is_directory=True)
    os.mkfifo(root / "fifo")
    run, job, fence = await begin(db_session, world)
    duplicate, created = await svc.start_scan(db_session, source.id, actor)
    assert duplicate.id == run.id and not created
    assert await svc.claim(db_session, run.id, job.celery_task_id, "scan@test") is None
    await drain(db_session, run, job, fence)
    assert run.complete and run.status == job.status == "completed"
    assert run.counts == {
        "processed_count": 4,
        "file_count": 1,
        "directory_count": 1,
        "skipped_count": 2,
    }
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == dataset.id)
        )
        == 0
    )
    assert await svc.claim(db_session, run.id, job.celery_task_id, "scan@test") is None


async def test_cancel_resume_retains_checkpoints_and_fences_late_worker_and_signal(
    db_session, world
):
    root, actor, _, _, source = world
    (root / "child").mkdir()
    (root / "child" / "a.jpg").write_bytes(b"a")
    run, job, fence = await begin(db_session, world)
    work = await next_work(db_session, run, job, fence)
    await svc.apply_directory(
        db_session, run.id, job.celery_task_id, fence, work, manifest(work)
    )
    await svc.cancel_job(db_session, job.id, actor)
    await db_session.flush()
    assert not run.complete and run.status == "cancelled"
    await svc.resume_scan(db_session, run.id, actor)
    await db_session.flush()
    current = await db_session.get(AsyncJob, run.async_job_id)
    assert current.id != job.id
    new_fence = await svc.claim(db_session, run.id, current.celery_task_id, "scan@test")
    with pytest.raises(svc.LostScanLease):
        await svc.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest(work)
        )
    await svc.signal_interrupted(db_session, job, cancelled=False)
    assert run.status == "running"
    await drain(db_session, run, current, new_fence)
    assert run.complete and source.generation == 2


async def test_expired_lease_cannot_apply_and_requires_new_attempt(db_session, world):
    run, job, fence = await begin(db_session, world)
    work = await next_work(db_session, run, job, fence)
    run.lease_expires_at = await svc.now(db_session) - timedelta(seconds=1)
    await db_session.flush()
    with pytest.raises(svc.LostScanLease):
        await svc.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest(work)
        )
    assert await svc.claim(db_session, run.id, job.celery_task_id, "scan@test") is None
    await svc.resume_scan(db_session, run.id, world[1])
    assert run.async_job_id != job.id


async def test_failed_directory_transaction_does_not_advance_checkpoint(
    db_session, world
):
    (world[0] / "a.jpg").write_bytes(b"a")
    run, job, fence = await begin(db_session, world)
    work = await next_work(db_session, run, job, fence)
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await svc.apply_directory(
                db_session, run.id, job.celery_task_id, fence, work, manifest(work)
            )
            await db_session.flush()
            raise RuntimeError("Injected interruption")
    await db_session.refresh(run)
    await db_session.refresh(job)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetScanEntry)
            .where(DatasetScanEntry.run_id == run.id)
        )
        == 1
    )
    work = await next_work(db_session, run, job, fence)
    assert work["relative_path"] == ""
    await drain(db_session, run, job, fence)
    assert run.complete and run.counts["file_count"] == 1


async def test_directory_changes_make_resume_incomplete(db_session, world):
    root, actor, *_ = world
    run, job, fence = await begin(db_session, world)
    work = await next_work(db_session, run, job, fence)
    await svc.apply_directory(
        db_session, run.id, job.celery_task_id, fence, work, manifest(work)
    )
    await svc.cancel_job(db_session, job.id, actor)
    await svc.resume_scan(db_session, run.id, actor)
    current = await db_session.get(AsyncJob, run.async_job_id)
    fence = await svc.claim(db_session, run.id, current.celery_task_id, "scan@test")
    (root / "new.jpg").write_bytes(b"new")
    work = await next_work(db_session, run, current, fence)
    with pytest.raises(fs.FilesystemStorageError, match="directory_changed"):
        await svc.apply_directory(
            db_session, run.id, current.celery_task_id, fence, work, manifest(work)
        )
    assert not run.complete


async def test_revocation_configuration_change_and_budget_block_batches(
    db_session, world
):
    root, actor, _, location, _ = world
    (root / "a.jpg").write_bytes(b"a")
    run, job, fence = await begin(db_session, world)
    work = await next_work(db_session, run, job, fence)
    original_role = actor.role
    actor.role = "employee"
    await db_session.flush()
    with pytest.raises(HTTPException, match="access_revoked"):
        await svc.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest(work)
        )
    actor.role = original_role
    location.config_revision += 1
    await db_session.flush()
    with pytest.raises(fs.FilesystemStorageError, match="configuration_changed"):
        await svc.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest(work)
        )
    location.config_revision -= 1
    run.snapshot = {**run.snapshot, "budget": {"max_entries": 0, "max_directories": 1}}
    await db_session.flush()
    with pytest.raises(fs.FilesystemStorageError, match="scan_budget_exceeded"):
        await svc.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest(work)
        )


async def test_job_retention_and_dataset_removal_preserve_inventory(db_session, world):
    run, job, fence = await begin(db_session, world)
    await drain(db_session, run, job, fence)
    await db_session.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
    await db_session.execute(delete(Dataset).where(Dataset.id == world[2].id))
    await db_session.refresh(run)
    await db_session.refresh(world[-1])
    assert run.complete and run.async_job_id is None and world[-1].dataset_id is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetScanEntry)
            .where(DatasetScanEntry.run_id == run.id)
        )
        == 1
    )


async def test_source_api_hides_unauthorized_sources_and_reports_dispatch_failure(
    httpx_client, db_session, world, super_admin, project_admin, monkeypatch
):
    headers = {"Authorization": f"Bearer {super_admin[1]}"}

    async def unavailable(db, run_id):
        _, _, run, job = await svc.run_context(db, run_id)
        await svc.finish(db, run, job, "failed", "dispatch_unavailable")
        await db.commit()

    monkeypatch.setattr("app.workers.dataset_scan.dispatch", unavailable)
    response = await httpx_client.post(
        f"/api/v1/dataset-sources/{world[-1].id}/scans", headers=headers
    )
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "failed" and not data["complete"]
    assert str(world[0]) not in response.text
    manager = {"Authorization": f"Bearer {project_admin[1]}"}
    assert (
        await httpx_client.get(
            f"/api/v1/dataset-scans/{data['id']}/entries", headers=manager
        )
    ).status_code == 404
    job = await db_session.get(AsyncJob, uuid.UUID(data["async_job_id"]))
    job.user_id = project_admin[0].id
    await db_session.flush()
    assert (
        await httpx_client.get(f"/api/v1/async-jobs/{job.id}", headers=manager)
    ).status_code == 403
    rows = (await httpx_client.get("/api/v1/async-jobs", headers=manager)).json()
    assert not any(row["kind"] == "dataset_scan" for row in rows["items"])


async def test_source_cas_and_active_edit_guard(db_session, world):
    run, job, _ = await begin(db_session, world)
    with pytest.raises(HTTPException):
        await svc.update_source(
            db_session,
            world[-1].id,
            DatasetSourceUpdate(expected_revision=1, name="Renamed"),
            world[1],
        )
    await svc.cancel_job(db_session, job.id, world[1])
    source = await svc.update_source(
        db_session,
        world[-1].id,
        DatasetSourceUpdate(expected_revision=1, name="Renamed"),
        world[1],
    )
    assert source.config_revision == 2 and source.name == "Renamed"
    with pytest.raises(HTTPException):
        await svc.update_source(
            db_session,
            source.id,
            DatasetSourceUpdate(expected_revision=1, enabled=True),
            world[1],
        )
