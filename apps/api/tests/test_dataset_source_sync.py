"""Periodic sync is delegated observation, never automatic business ingestion."""

from datetime import timedelta
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import StorageLocationProbe
from app.db.models.dataset_source import DatasetScanRun, DatasetRegistrationRun
from app.schemas.dataset_source import (
    SourceSyncScheduleUpdate as Update,
    SourceSyncScheduleContinue as Continue,
)
from app.services import (
    dataset_source_sync as sync,
    dataset_scans as scans,
    dataset_registration as registrations,
)
from app.services import filesystem_storage as fs, storage_location as locations
from app.workers import dataset_source_sync as worker
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import accept_all_files
from tests.test_dataset_registration import drain, begin_confirmed


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    async def dispatch(*args):
        return None

    monkeypatch.setattr("app.workers.dataset_scan.dispatch", dispatch)
    monkeypatch.setattr("app.workers.dataset_registration.dispatch", dispatch)


async def enable(db, world):
    row = await sync.configure(
        db,
        world[5].id,
        Update(expected_revision=0, enabled=True, interval_minutes=15),
        world[2],
    )
    await db.commit()
    return row


async def wake(db, row):
    row.next_check_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()
    work = await sync.claim_due(db, row.source_id)
    await db.commit()
    assert work
    await sync.process(db, *work)
    return work


async def finish_scan(db, scan_id):
    run = await db.get(DatasetScanRun, scan_id)
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    for _ in range(20):
        work = await scans.next_directory(db, run.id, job.celery_task_id, fence)
        await db.commit()
        if work is None:
            return run
        if work.get("transition"):
            continue
        manifest = fs.scan_directory(
            work["config"],
            work["path"],
            expected_anchor=work["anchor"],
            expected_identity=work["identity"],
        )
        await scans.apply_directory(
            db, run.id, job.celery_task_id, fence, work, manifest
        )
        await db.commit()
    raise AssertionError("scan did not complete")


async def finish_preflight(db, row):
    run = await db.get(DatasetRegistrationRun, row.registration_id)
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registrations.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    return run


async def to_review(db, world):
    row = await enable(db, world)
    await wake(db, row)
    await finish_scan(db, row.scan_id)
    await wake(db, row)
    run = await finish_preflight(db, row)
    await wake(db, row)
    assert row.state == "awaiting_review"
    return row, run


async def test_periodic_flow_waits_for_review_and_explicit_continue(
    db_session, content_world
):
    db = db_session
    row, run = await to_review(db, content_world)
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0
    original_scan = row.scan_id
    for _ in range(2):
        await wake(db, row)
        assert row.scan_id == original_scan and row.state == "awaiting_review"
    with pytest.raises(HTTPException):
        await sync.continue_cycle(
            db,
            row.source_id,
            Continue(expected_revision=row.revision, scan_id=uuid.uuid4()),
            content_world[2],
        )
    await sync.continue_cycle(
        db,
        row.source_id,
        Continue(expected_revision=row.revision, scan_id=row.scan_id),
        content_world[2],
    )
    await db.commit()
    await wake(db, row)
    assert row.state == "idle" and row.scan_id == original_scan
    row.next_run_at = await scans.now(db) - timedelta(seconds=1)
    await wake(db, row)
    assert row.scan_id != original_scan
    assert run.comparison_complete and run.phase == "preflight"


async def test_no_change_runs_next_interval_without_duplicate_items(
    db_session, content_world
):
    db = db_session
    await accept_all_files(db, content_world)
    row = await enable(db, content_world)
    for _ in range(3):
        row.next_run_at = await scans.now(db) - timedelta(seconds=1)
        await wake(db, row)
        await finish_scan(db, row.scan_id)
        await wake(db, row)
        run = await finish_preflight(db, row)
        await wake(db, row)
        assert run.comparison_complete and row.state == "idle"
        assert row.next_run_at > await scans.now(db)
        assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


async def test_manual_confirmation_releases_review_gate(db_session, content_world):
    db = db_session
    row, run = await to_review(db, content_world)
    task, fence = await begin_confirmed(db, run, content_world)
    await drain(db, run, task, fence)
    await wake(db, row)
    assert (
        row.state == "idle"
        and await db.scalar(select(func.count()).select_from(DatasetItem)) == 1
    )


async def test_pause_fences_queued_wakeup_but_keeps_current_run(
    db_session, content_world
):
    db, world = db_session, content_world
    row = await enable(db, world)
    old = await sync.claim_due(db, row.source_id)
    await db.commit()
    await sync.configure(
        db,
        row.source_id,
        Update(expected_revision=row.revision, enabled=False),
        world[2],
    )
    await db.commit()
    with pytest.raises(sync.LostWakeup):
        await sync.process(db, *old)
    assert await db.scalar(select(func.count()).select_from(DatasetScanRun)) == 1
    await sync.configure(
        db,
        row.source_id,
        Update(expected_revision=row.revision, enabled=True),
        world[2],
    )
    await db.commit()
    await wake(db, row)
    scan_id = row.scan_id
    await sync.configure(
        db,
        row.source_id,
        Update(expected_revision=row.revision, enabled=False),
        world[2],
    )
    await db.commit()
    assert (await finish_scan(db, scan_id)).complete
    assert await sync.claim_due(db, row.source_id) is None
    assert row.registration_id is None


async def test_duplicate_wakeups_and_recovery_after_lost_publish(
    db_session, content_world
):
    db = db_session
    row = await enable(db, content_world)
    old = await sync.claim_due(db, row.source_id)
    await db.commit()
    assert await sync.claim_due(db, row.source_id) is None
    row.dispatch_expires_at = row.next_check_at = await scans.now(db) - timedelta(
        seconds=1
    )
    await db.commit()
    new = await sync.claim_due(db, row.source_id)
    await db.commit()
    with pytest.raises(sync.LostWakeup):
        await sync.process(db, *old)
    await sync.process(db, *new)
    scan_id = row.scan_id
    with pytest.raises(sync.LostWakeup):
        await sync.process(db, *new)
    await wake(db, row)  # pending publication can be replayed
    assert row.scan_id == scan_id
    with pytest.raises(sync.LostWakeup):
        await sync.process(db, row.source_id, row.revision, uuid.uuid4(), "wrong@node")


async def test_manual_active_work_is_never_adopted(db_session, content_world):
    db, world = db_session, content_world
    manual, _ = await scans.start_scan(db, world[5].id, world[2])
    await db.commit()
    row = await enable(db, world)
    await wake(db, row)
    assert row.scan_id is None and row.last_error == "source_busy"
    await finish_scan(db, manual.id)
    run, _ = await registrations.start_preflight(db, manual.id, world[2])
    await db.commit()
    await wake(db, row)
    assert row.scan_id is None and row.last_error == "source_busy"
    await registrations.cancel_job(db, run.async_job_id, world[2])
    await db.commit()
    await wake(db, row)
    assert row.scan_id and row.scan_id != manual.id


async def test_refreshes_expired_worker_probe_without_faking_api_node(
    db_session, content_world
):
    db, world = db_session, content_world
    probe = await db.get(StorageLocationProbe, (world[4].id, "worker:content@test"))
    probe.observed_at -= timedelta(hours=1)
    api_probe = await db.get(
        StorageLocationProbe, (world[4].id, locations.api_node_id())
    )
    await db.delete(api_probe)
    await db.commit()
    row = await enable(db, world)
    await wake(db, row)
    await db.refresh(probe)
    assert probe.observed_at > await scans.now(db) - timedelta(minutes=1)
    await finish_scan(db, row.scan_id)
    await wake(db, row)
    assert row.registration_id
    assert (
        await db.get(StorageLocationProbe, (world[4].id, locations.api_node_id()))
        is None
    )


@pytest.mark.parametrize("change", ["role", "source_revision", "location_revision"])
async def test_revoked_authority_or_configuration_blocks_without_io(
    db_session, content_world, monkeypatch, change
):
    db, world = db_session, content_world
    row = await enable(db, world)
    if change == "role":
        world[2].role = "employee"
    elif change == "source_revision":
        world[5].config_revision += 1
    else:
        world[4].config_revision += 1
    await db.commit()
    work = await sync.claim_due(db, row.source_id)
    await db.commit()

    async def no_io(*args, **kwargs):
        pytest.fail("revoked schedule performed I/O")

    monkeypatch.setattr(locations, "perform_probe", no_io)
    with pytest.raises((HTTPException, fs.FilesystemStorageError)) as exc:
        await sync.process(db, *work)
    await db.rollback()
    await sync.record_failure(db, *work[:3], worker.error_code(exc.value))
    await db.commit()
    assert not row.enabled and row.state == "blocked"


async def test_cancelled_scan_is_not_automatically_resumed(db_session, content_world):
    db, world = db_session, content_world
    row = await enable(db, world)
    await wake(db, row)
    old_id = row.scan_id
    scan = await db.get(DatasetScanRun, old_id)
    await scans.cancel_job(db, scan.async_job_id, world[2])
    await db.commit()
    await wake(db, row)
    assert row.state == "idle" and row.next_run_at > await scans.now(db)
    assert scan.status == "cancelled" and row.registration_id is None


async def test_schedule_api_scope_cas_and_private_fields(
    db_session, content_world, httpx_client, super_admin, annotator
):
    url = f"/api/v1/dataset-sources/{content_world[5].id}/sync-schedule"
    response = await httpx_client.put(
        url,
        json={"expected_revision": 0, "enabled": True},
        headers=headers(super_admin),
    )
    assert response.status_code == 200
    assert (
        not {"api_key_id", "key_proof", "snapshot", "dispatch_token"}
        & response.json().keys()
    )
    assert (await httpx_client.get(url, headers=headers(annotator))).status_code == 403
    assert (
        await httpx_client.put(
            url,
            json={"expected_revision": 0, "enabled": False},
            headers=headers(super_admin),
        )
    ).status_code == 409
    assert (
        await httpx_client.put(
            url,
            json={"expected_revision": 1, "enabled": True, "interval_minutes": 1},
            headers=headers(super_admin),
        )
    ).status_code == 422


async def test_tick_preserves_crash_checkpoint_and_fair_due_order(
    db_session, content_world, monkeypatch
):
    db = db_session
    row = await enable(db, content_world)

    def offline(*args):
        raise OSError("offline")

    monkeypatch.setattr(worker, "_publish", offline)
    assert (await worker.tick(db))["dispatched"] == 0
    await db.refresh(row)
    assert row.last_error == "dispatch_unavailable" and row.dispatch_token is None
    calls = []
    monkeypatch.setattr(worker, "_publish", lambda *args: calls.append(args))
    row.next_check_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()
    assert (await worker.tick(db))["dispatched"] == 1
    assert (await worker.tick(db))["dispatched"] == 0
    assert calls[0][0] == row.source_id


async def test_formal_registration_is_observed_without_reauthorizing(
    db_session, content_world, monkeypatch
):
    db, world = db_session, content_world
    row, run = await to_review(db, world)
    task, _ = await begin_confirmed(db, run, world)
    snapshot, job_id = dict(run.snapshot), run.async_job_id
    run.lease_expires_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail("scheduler tried to resume a human registration")

    monkeypatch.setattr(registrations, "resume", forbidden)
    await wake(db, row)
    assert row.state == "registering"
    assert run.async_job_id == job_id and run.snapshot == snapshot
    assert (await db.get(AsyncJob, job_id)).celery_task_id == task


async def test_manual_preflight_on_periodic_scan_is_not_adopted(
    db_session, content_world
):
    db, world = db_session, content_world
    row = await enable(db, world)
    await wake(db, row)
    await finish_scan(db, row.scan_id)
    manual, _ = await registrations.start_preflight(db, row.scan_id, world[2])
    await db.commit()
    job_id = manual.async_job_id
    await wake(db, row)
    assert row.registration_id is None and row.last_error == "source_busy"
    assert manual.async_job_id == job_id
    await registrations.cancel_job(db, job_id, world[2])
    await db.commit()
    await wake(db, row)
    assert row.state == "idle" and row.registration_id is None


async def test_plan_cannot_change_delegation_during_active_cycle(
    db_session, content_world
):
    from app.db.models.api_key import ApiKey

    db, world = db_session, content_world
    row = await enable(db, world)
    await wake(db, row)
    key = ApiKey(
        user_id=world[2].id,
        name="new authority",
        key_prefix="ak_cycle",
        key_hash="new-proof",
        scopes=["datasets:read", "datasets:write"],
    )
    db.add(key)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await sync.configure(
            db,
            row.source_id,
            Update(expected_revision=row.revision, enabled=True),
            world[2],
            key.id,
        )
    assert exc.value.detail["code"] == "schedule_cycle_authority_pinned"
    assert row.api_key_id is None


@pytest.mark.parametrize("change", ["revoked", "rotated", "read_only", "expired"])
async def test_key_delegation_is_fixed_and_requires_both_scopes(
    db_session, content_world, change
):
    from app.db.models.api_key import ApiKey

    db, world = db_session, content_world
    key = ApiKey(
        user_id=world[2].id,
        name="sync",
        key_prefix="ak_sync",
        key_hash="original-proof",
        scopes=["datasets:read", "datasets:write"],
    )
    db.add(key)
    await db.flush()
    row = await sync.configure(
        db, world[5].id, Update(expected_revision=0, enabled=True), world[2], key.id
    )
    if change == "revoked":
        key.revoked_at = await scans.now(db)
    elif change == "rotated":
        key.key_hash = "replacement-proof"
    elif change == "read_only":
        key.scopes = ["datasets:read"]
    else:
        key.expires_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException):
        await wake(db, row)
    assert row.scan_id is None


async def test_rotation_between_guard_and_creation_does_not_renew_authority(
    db_session, content_world, monkeypatch
):
    from app.db.models.api_key import ApiKey

    db, world = db_session, content_world
    key = ApiKey(
        user_id=world[2].id,
        name="sync",
        key_prefix="ak_race",
        key_hash="original-proof",
        scopes=["datasets:read", "datasets:write"],
    )
    db.add(key)
    await db.flush()
    row = await sync.configure(
        db, world[5].id, Update(expected_revision=0, enabled=True), world[2], key.id
    )
    await db.commit()
    original = scans.start_scan

    async def rotate(*args, **kwargs):
        key.key_hash = "rotated-in-window"
        await db.flush()
        return await original(*args, **kwargs)

    monkeypatch.setattr(scans, "start_scan", rotate)
    with pytest.raises(HTTPException) as exc:
        await wake(db, row)
    assert exc.value.status_code == 403
    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(DatasetScanRun)) == 1


async def test_pause_during_probe_prevents_creating_run(
    db_session, content_world, monkeypatch
):
    db, world = db_session, content_world
    row = await enable(db, world)
    original = locations.perform_probe

    async def pause(*args, **kwargs):
        result = await original(*args, **kwargs)
        await sync.configure(
            db,
            row.source_id,
            Update(expected_revision=row.revision, enabled=False),
            world[2],
        )
        await db.commit()
        return result

    monkeypatch.setattr(locations, "perform_probe", pause)
    with pytest.raises(sync.LostWakeup):
        await wake(db, row)
    assert row.scan_id is None


async def test_expired_scan_attempt_is_recovered_with_original_authority(
    db_session, content_world
):
    db, world = db_session, content_world
    row = await enable(db, world)
    await wake(db, row)
    run = await db.get(DatasetScanRun, row.scan_id)
    old_job = await db.get(AsyncJob, run.async_job_id)
    old_task = old_job.celery_task_id
    fence = await scans.claim(db, run.id, old_task, "content@test")
    run.lease_expires_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()
    await wake(db, row)
    assert run.async_job_id != old_job.id
    assert (await db.get(AsyncJob, run.async_job_id)).user_id == row.actor_id
    assert run.snapshot["key_proof"] == row.key_proof
    assert await scans.claim(db, run.id, old_task, "content@test") is None
    assert fence is not None


async def test_expired_preflight_is_recovered_without_replacing_baseline(
    db_session, content_world
):
    db, world = db_session, content_world
    row = await enable(db, world)
    await wake(db, row)
    await finish_scan(db, row.scan_id)
    await wake(db, row)
    run = await db.get(DatasetRegistrationRun, row.registration_id)
    old_job = await db.get(AsyncJob, run.async_job_id)
    await registrations.claim(db, run.id, old_job.celery_task_id, "content@test")
    run.lease_expires_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()
    baseline = run.snapshot.get("comparison_baseline")
    await wake(db, row)
    assert run.async_job_id != old_job.id
    assert run.snapshot.get("comparison_baseline") == baseline
    assert run.snapshot["sync_source_id"] == str(row.source_id)


async def test_completed_job_cleanup_does_not_lose_periodic_checkpoints(
    db_session, content_world
):
    db, world = db_session, content_world
    row = await enable(db, world)
    await wake(db, row)
    await finish_scan(db, row.scan_id)
    run = await db.get(DatasetScanRun, row.scan_id)
    await db.delete(await db.get(AsyncJob, run.async_job_id))
    await db.commit()
    await wake(db, row)
    assert row.registration_id
    preflight = await finish_preflight(db, row)
    await db.delete(await db.get(AsyncJob, preflight.async_job_id))
    await db.commit()
    await wake(db, row)
    assert row.state == "awaiting_review"


async def test_manual_scan_between_periodic_scan_and_preflight_is_respected(
    db_session, content_world
):
    db, world = db_session, content_world
    row = await enable(db, world)
    await wake(db, row)
    await finish_scan(db, row.scan_id)
    manual, _ = await scans.start_scan(db, row.source_id, world[2])
    await db.commit()
    await wake(db, row)
    assert row.registration_id is None and row.last_error == "source_busy"
    await finish_scan(db, manual.id)
    await wake(db, row)
    assert row.registration_id


@pytest.mark.parametrize("review", [False, True])
async def test_paused_schedule_can_be_reauthorized_without_adopting_old_work(
    db_session, content_world, review
):
    from app.db.models.api_key import ApiKey

    db, world = db_session, content_world
    if review:
        row, run = await to_review(db, world)
    else:
        row = await enable(db, world)
        await wake(db, row)
        run = await db.get(DatasetScanRun, row.scan_id)
        await scans.cancel_job(db, run.async_job_id, world[2])
        await db.commit()
    original = dict(run.snapshot)
    await sync.configure(
        db,
        row.source_id,
        Update(expected_revision=row.revision, enabled=False),
        world[2],
    )
    key = ApiKey(
        user_id=world[2].id,
        name="replacement",
        key_prefix="ak_new",
        key_hash="new-proof",
        scopes=["datasets:read", "datasets:write"],
    )
    db.add(key)
    await db.commit()
    await sync.configure(
        db,
        row.source_id,
        Update(expected_revision=row.revision, enabled=True),
        world[2],
        key.id,
    )
    await db.commit()
    assert run.snapshot == original and row.api_key_id == key.id
    await wake(db, row)
    if review:
        assert row.state == "awaiting_review" and row.registration_id == run.id
        assert row.next_run_at is None
        assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0
    else:
        assert row.scan_id != run.id and row.state == "scanning"
