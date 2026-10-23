"""Automatic ingestion requires a pinned, wholly safe cycle and current delegation."""

from datetime import timedelta

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import DatasetRegistrationRun, DatasetScanRun
from app.schemas.dataset_source import SourceSyncScheduleUpdate as Update
from app.services import dataset_source_sync as sync, dataset_scans as scans
from app.services import (
    dataset_registration as registrations,
    dataset_content,
    storage_location as locations,
)
from tests.test_dataset_content import content_world as content_world
from tests.test_dataset_source_changes import accept_all_files, picture
from tests.test_dataset_source_sync import (
    no_broker as no_broker,
    wake,
    finish_scan,
    finish_preflight,
)


async def automatic_preflight(db, world, key_id=None):
    row = await sync.configure(
        db,
        world[5].id,
        Update(
            expected_revision=0,
            enabled=True,
            new_file_policy="pure_additions",
            timezone="Asia/Shanghai",
        ),
        world[2],
        key_id,
    )
    await db.commit()
    await wake(db, row)
    await finish_scan(db, row.scan_id)
    await wake(db, row)
    run = await finish_preflight(db, row)
    return row, run


async def test_pure_additions_automatic_and_pending_publish_recovery(
    db_session, content_world, monkeypatch
):
    db, world = db_session, content_world
    row, run = await automatic_preflight(db, world)
    calls = []
    original = locations.perform_probe

    async def probe(db, location, node, path, **kwargs):
        calls.append(node)
        return await original(db, location, node, path, **kwargs)

    monkeypatch.setattr(locations, "perform_probe", probe)
    await wake(db, row)
    assert run.phase == "registering" and row.state == "registering"
    assert (
        run.snapshot["sync_auto_ingestion"]
        and run.registration_scope == "additions_only"
    )
    assert calls and set(calls) == {"worker:content@test"}
    job_id = run.async_job_id
    await wake(db, row)  # broker publication replay, never creates a second attempt
    assert run.async_job_id == job_id
    job = await db.get(AsyncJob, job_id)
    await registrations.claim(db, run.id, job.celery_task_id, "content@test")
    run.lease_expires_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()
    await wake(db, row)
    assert run.async_job_id != job_id
    await finish_preflight(db, row)  # same drain handles formal registration
    await wake(db, row)
    assert (
        row.state == "idle"
        and await db.scalar(select(func.count()).select_from(DatasetItem)) == 1
    )
    row.next_run_at = await scans.now(db) - timedelta(seconds=1)
    await wake(db, row)
    await finish_scan(db, row.scan_id)
    await wake(db, row)
    unchanged = await finish_preflight(db, row)
    await wake(db, row)
    assert row.state == "idle" and unchanged.phase == "preflight"
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


@pytest.mark.parametrize("unsafe", ["changed", "not_seen", "unreadable", "excluded"])
async def test_one_unsafe_difference_holds_whole_cycle(
    db_session, content_world, unsafe
):
    db, world = db_session, content_world
    await accept_all_files(db, world)
    path = world[0] / "图片 空格.png"
    if unsafe == "changed":
        path.write_bytes(picture("red"))
    elif unsafe == "not_seen":
        path.unlink()
    elif unsafe == "unreadable":
        path.write_bytes(b"not an image")
    else:
        world[5].config = {**world[5].config, "exclude_globs": ["图片 空格.png"]}
        world[5].config_revision += 1
    (world[0] / "new.png").write_bytes(picture("blue"))
    row, run = await automatic_preflight(db, world)
    await wake(db, row)
    assert row.state == "awaiting_review" and run.phase == "preflight"
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


async def test_enabling_policy_does_not_adopt_manual_cycle(db_session, content_world):
    db, world = db_session, content_world
    row = await sync.configure(
        db, world[5].id, Update(expected_revision=0, enabled=True), world[2]
    )
    await db.commit()
    await wake(db, row)
    assert (await db.get(DatasetScanRun, row.scan_id)).snapshot[
        "sync_new_file_policy"
    ] == "manual"
    await sync.configure(
        db,
        row.source_id,
        Update(
            expected_revision=row.revision,
            enabled=True,
            new_file_policy="pure_additions",
        ),
        world[2],
    )
    await db.commit()
    await finish_scan(db, row.scan_id)
    await wake(db, row)
    run = await finish_preflight(db, row)
    await wake(db, row)
    assert (
        row.state == "awaiting_review"
        and run.snapshot["sync_new_file_policy"] == "manual"
    )
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0


@pytest.mark.parametrize("change", ["pause", "key_rotation"])
@pytest.mark.parametrize("barrier", ["probe", "snapshot"])
async def test_pause_during_automatic_confirmation_fences_attempt(
    db_session, content_world, monkeypatch, barrier, change
):
    db, world = db_session, content_world
    from app.db.models.api_key import ApiKey

    key = ApiKey(
        user_id=world[2].id,
        name="automatic",
        key_prefix="ak_auto",
        key_hash="original",
        scopes=["datasets:read", "datasets:write"],
    )
    db.add(key)
    await db.flush()
    row, run = await automatic_preflight(db, world, key.id)
    old_job, run_id = run.async_job_id, run.id
    original = (
        locations.perform_probe if barrier == "probe" else dataset_content.scan_snapshot
    )
    calls = 0
    captures = []
    original_capture = dataset_content.snapshots.capture

    async def capture(*args, **kwargs):
        captures.append(True)
        return await original_capture(*args, **kwargs)

    monkeypatch.setattr(dataset_content.snapshots, "capture", capture)

    async def pause(*args, **kwargs):
        nonlocal calls
        calls += 1
        # The first probe belongs to the coordinator, the second to confirmation.
        if barrier == "snapshot" or calls == 2:
            if change == "key_rotation":
                key.key_hash = "rotated"
                await db.flush()
            else:
                await sync.configure(
                    db,
                    row.source_id,
                    Update(
                        expected_revision=row.revision,
                        enabled=False,
                        new_file_policy="pure_additions",
                    ),
                    world[2],
                    key.id,
                )
            await db.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        locations if barrier == "probe" else dataset_content,
        "perform_probe" if barrier == "probe" else "scan_snapshot",
        pause,
    )
    with pytest.raises(sync.LostWakeup if change == "pause" else HTTPException):
        await wake(db, row)
    await db.rollback()
    current = await db.get(DatasetRegistrationRun, run_id, populate_existing=True)
    assert current.phase == "preflight" and current.async_job_id == old_job
    assert not captures
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0


def test_timezone_is_validated_and_manual_is_default():
    value = Update(expected_revision=0, enabled=True, timezone="Asia/Shanghai")
    assert value.new_file_policy == "manual"
    for zone in [
        "not/a-zone",
        "../etc/passwd",
        "",
        "localtime",
        "Factory",
        "posix/Asia/Shanghai",
        "right/UTC",
    ]:
        with pytest.raises(ValidationError):
            Update(expected_revision=0, enabled=True, timezone=zone)
