import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, inspect, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import (
    DatasetRegistrationEntry as Entry,
    DatasetScanEntry,
)
from app.db.models.dataset_asset import StorageLocationProbe
from app.services import (
    dataset_registration as svc,
    dataset_scans as scans,
    filesystem_storage as fs,
)
from app.services import content_snapshot as snapshots
from app.workers import dataset_registration as worker
from tests.test_dataset_content import content_world as content_world, headers


async def start(db, world):
    run, created = await svc.start_preflight(db, world[6].id, world[2])
    assert created
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await svc.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    return run, job.celery_task_id, fence


async def drain(db, run, task_id, fence):
    run_id = inspect(run).identity[0]
    for _ in range(20):
        work = await svc.next_entry(db, run_id, task_id, fence)
        await db.commit()
        if work is None:
            return
        try:
            await worker.process_one(db, run_id, task_id, fence, work)
        except (snapshots.ContentError, fs.FilesystemStorageError) as exc:
            await db.rollback()
            await svc.record(
                db,
                run_id,
                task_id,
                fence,
                work["path_digest"],
                "error",
                error_code=exc.code,
            )
            await db.commit()
    raise AssertionError("Registration did not terminate")


async def begin_confirmed(db, run, world):
    run, created = await svc.confirm(db, inspect(run).identity[0], world[2])
    assert created
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await svc.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    return job.celery_task_id, fence


async def test_preflight_is_not_ingestion_and_confirmation_is_idempotent(
    db_session, content_world
):
    db = db_session
    run, task_id, fence = await start(db, content_world)
    assert run.counts["indexed_count"] == 1 and run.counts["estimated_new_count"] == 1
    await drain(db, run, task_id, fence)
    assert run.phase == "preflight" and run.status == "completed"
    assert run.counts["ready_count"] == 1
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0
    task_id, fence = await begin_confirmed(db, run, content_world)
    await drain(db, run, task_id, fence)
    assert (
        run.counts["registered_count"] == 1 and run.counts["original_copy_bytes"] == 0
    )
    same, created = await svc.confirm(db, run.id, content_world[2])
    assert not created and same.id == run.id
    same, created = await svc.start_preflight(db, content_world[6].id, content_world[2])
    assert not created and same.id == run.id
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


async def test_cancel_during_io_prevents_receipt_and_registration(
    db_session, content_world, monkeypatch
):
    db = db_session
    run, task_id, fence = await start(db, content_world)
    work = await svc.next_entry(db, run.id, task_id, fence)
    await db.commit()
    run_id, scan_id = run.id, content_world[6].id
    capture = snapshots.capture

    async def cancel_after_capture(*args, **kwargs):
        snapshot = await capture(*args, **kwargs)
        await svc.cancel_job(db, run.async_job_id, content_world[2])
        await db.commit()
        return snapshot

    monkeypatch.setattr(snapshots, "capture", cancel_after_capture)
    with pytest.raises(svc.LostRegistrationLease):
        await worker.process_one(db, run.id, task_id, fence, work)
    await db.rollback()
    observed = await db.get(DatasetScanEntry, (scan_id, work["path_digest"]))
    assert observed.verified_content is None
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0
    run = await db.get(type(run), run_id)
    assert run.status == "cancelled" and run.counts["pending_count"] == 1


async def test_bad_file_isolated_and_explicit_repair_rechecks_current_bytes(
    db_session, content_world
):
    db = db_session
    root = content_world[0]
    bad = root / "损坏.png"
    bad.write_bytes(b"broken")
    digest = scans.path_digest(bad.name)
    db.add(
        DatasetScanEntry(
            run_id=content_world[6].id,
            path_digest=digest,
            relative_path=bad.name,
            kind="file",
            state="observed",
            observed=fs.file_observation(bad.stat()),
        )
    )
    await db.flush()
    run, task_id, fence = await start(db, content_world)
    await drain(db, run, task_id, fence)
    assert run.counts["ready_count"] == run.counts["error_count"] == 1
    bad.write_bytes(content_world[1])
    run = await svc.resume(db, run.id, content_world[2], path_digest=digest)
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await svc.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    assert (
        run.phase == "preflight"
        and run.counts["ready_count"] == 2
        and run.counts["error_count"] == 0
    )
    task_id, fence = await begin_confirmed(db, run, content_world)
    await drain(db, run, task_id, fence)
    assert run.counts["registered_count"] == 2


async def test_preflight_receipt_cannot_accept_replacement_on_retry(
    db_session, content_world
):
    db = db_session
    run, task_id, fence = await start(db, content_world)
    await drain(db, run, task_id, fence)
    task_id, fence = await begin_confirmed(db, run, content_world)
    (content_world[0] / "图片 空格.png").write_bytes(b"replacement")
    await drain(db, run, task_id, fence)
    assert run.counts["error_count"] == 1
    entry = await db.scalar(select(Entry).where(Entry.run_id == run.id))
    await svc.resume(db, run.id, content_world[2], path_digest=entry.path_digest)
    assert not entry.refresh_unverified
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0


async def test_claimed_worker_reads_after_probe_ttl_and_resumed_old_fence_cannot_write(
    db_session, content_world
):
    db = db_session
    run, task_id, fence = await start(db, content_world)
    probe = await db.get(
        StorageLocationProbe, (content_world[4].id, "worker:content@test")
    )
    probe.observed_at -= timedelta(minutes=10)
    await db.commit()
    work = await svc.next_entry(db, run.id, task_id, fence)
    await db.commit()
    await worker.process_one(db, run.id, task_id, fence, work)
    assert run.counts["ready_count"] == 1
    await svc.yield_attempt(db, run.id, task_id, fence)
    await db.commit()
    with pytest.raises(svc.LostRegistrationLease):
        await svc.record(db, run.id, task_id, fence, work["path_digest"], "error")


async def test_old_signal_waiting_for_source_cannot_kill_new_chunk(
    db_session, content_world, monkeypatch
):
    db = db_session
    run, _, _ = await start(db, content_world)
    old = await db.get(AsyncJob, run.async_job_id)
    original = svc.context

    async def newer_context(*args):
        old.celery_task_id = str(uuid.uuid4())
        await db.flush()
        return await original(*args)

    monkeypatch.setattr(svc, "context", newer_context)
    await svc.signal_interrupted(db, old, cancelled=False)
    assert run.status == "running"


async def test_publish_failure_stays_recoverable(
    db_session, content_world, monkeypatch
):
    run, created = await svc.start_preflight(
        db_session, content_world[6].id, content_world[2]
    )
    assert created
    await db_session.commit()

    def unavailable(*args):
        raise OSError("broker unavailable")

    monkeypatch.setattr(worker, "_publish", unavailable)
    await worker.dispatch(db_session, run.id)
    assert run.status == "pending" and run.error_code == "dispatch_unavailable"
    assert run.lease_expires_at is not None


async def test_registration_entry_errors_paginate_and_are_admin_only(
    db_session, content_world, httpx_client, super_admin, annotator
):
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    url = f"/api/v1/dataset-registrations/{run.id}/entries"
    response = await httpx_client.get(
        url, params={"state": "ready", "limit": 1}, headers=headers(super_admin)
    )
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["sha256"]
    assert (await httpx_client.get(url, headers=headers(annotator))).status_code == 403


async def test_preflight_refuses_wrong_api_mount(
    db_session, content_world, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "storage_filesystem_roots", {})
    with pytest.raises(HTTPException) as exc:
        await svc.start_preflight(db_session, content_world[6].id, content_world[2])
    assert (
        exc.value.status_code == 409
        and exc.value.detail["code"] == "api_source_unavailable"
    )


async def test_missing_first_file_does_not_block_other_preflight_entries(
    db_session, content_world
):
    root, data, _, _, _, _, scan, first = content_world
    second = root / "可用.png"
    second.write_bytes(data)
    db_session.add(
        DatasetScanEntry(
            run_id=scan.id,
            path_digest=scans.path_digest(second.name),
            relative_path=second.name,
            kind="file",
            state="observed",
            observed=fs.file_observation(second.stat()),
        )
    )
    await db_session.flush()
    (root / first.relative_path).unlink()
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    assert (
        run.status == "completed"
        and run.counts["ready_count"] == run.counts["error_count"] == 1
    )
    task_id, fence = await begin_confirmed(db_session, run, content_world)
    await drain(db_session, run, task_id, fence)
    assert run.counts["registered_count"] == 1


@pytest.mark.parametrize("mutation", ["replace", "delete"])
async def test_api_confirmation_skips_changed_candidate_and_keeps_stable_images(
    db_session, content_world, mutation
):
    root, data, _, _, _, _, scan, first = content_world
    second = root / "可用.png"
    second.write_bytes(data)
    db_session.add(
        DatasetScanEntry(
            run_id=scan.id,
            path_digest=scans.path_digest(second.name),
            relative_path=second.name,
            kind="file",
            state="observed",
            observed=fs.file_observation(second.stat()),
        )
    )
    await db_session.flush()
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    # Change the first ready candidate in the API's actual keyset order.
    digest = await db_session.scalar(
        select(Entry.path_digest)
        .where(Entry.run_id == run.id)
        .order_by(Entry.path_digest)
        .limit(1)
    )
    candidate = await db_session.get(DatasetScanEntry, (scan.id, digest))
    if mutation == "replace":
        (root / candidate.relative_path).write_bytes(b"changed")
    else:
        (root / candidate.relative_path).unlink()
    task_id, fence = await begin_confirmed(db_session, run, content_world)
    await drain(db_session, run, task_id, fence)
    assert run.counts["registered_count"] == run.counts["error_count"] == 1


async def test_source_directory_offline_is_not_classified_as_individual_file_errors(
    db_session, content_world
):
    root, _, actor, _, location, source, *_ = content_world
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    root.rename(root.with_name("offline-source"))
    with pytest.raises(fs.FilesystemStorageError, match="source_unavailable"):
        await svc.confirm(db_session, run.id, actor)
    assert run.counts["ready_count"] == 1 and run.counts["error_count"] == 0


async def test_confirm_and_retry_report_other_active_run_as_conflict(
    db_session, content_world
):
    from app.db.models.dataset_source import DatasetRegistrationRun

    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    scan2 = type(content_world[6])(
        source_id=run.source_id, snapshot=content_world[6].snapshot
    )
    db_session.add(scan2)
    await db_session.flush()
    other = DatasetRegistrationRun(
        scan_run_id=scan2.id,
        source_id=run.source_id,
        phase="preflight",
        status="pending",
        generation=0,
        snapshot=run.snapshot,
        counts={},
    )
    db_session.add(other)
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await svc.confirm(db_session, run.id, content_world[2])
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await svc.resume(
            db_session,
            run.id,
            content_world[2],
            path_digest=content_world[7].path_digest,
        )
    assert exc.value.status_code == 409


async def test_transient_deferral_keeps_file_pending_and_invalidates_chunk(
    db_session, content_world
):
    run, task_id, fence = await start(db_session, content_world)
    await svc.yield_attempt(
        db_session, run.id, task_id, fence, delay=5, code="snapshot_capacity_exceeded"
    )
    assert (
        run.status == "pending"
        and run.counts["pending_count"] == 1
        and run.counts["error_count"] == 0
    )
    with pytest.raises(svc.LostRegistrationLease):
        await svc.fenced(db_session, run.id, task_id, fence)


async def test_read_only_key_cannot_cancel_registration(
    db_session, content_world, httpx_client, super_admin
):
    run, _, _ = await start(db_session, content_world)
    key = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "read-only-registration", "scopes": ["datasets:read"]},
    )
    assert key.status_code == 201, key.text
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{run.async_job_id}/cancel",
        headers={"Authorization": f"Bearer {key.json()['plaintext']}"},
    )
    assert response.status_code == 403, response.text
    assert run.status == "running"


@pytest.mark.parametrize("phase", ["preflight", "confirm"])
async def test_rotated_key_during_probe_cannot_authorize_new_attempt(
    db_session, content_world, httpx_client, super_admin, monkeypatch, phase
):
    from app.db.models.api_key import ApiKey

    key_response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "registration-write", "scopes": ["datasets:write"]},
    )
    key_id = uuid.UUID(key_response.json()["id"])
    if phase == "confirm":
        run, task_id, fence = await start(db_session, content_world)
        await drain(db_session, run, task_id, fence)
        original = svc.locations.perform_probe
    else:
        original = svc.probe_candidates

    async def rotate(*args, **kwargs):
        result = await original(*args, **kwargs)
        key = await db_session.get(ApiKey, key_id)
        key.key_hash = "rotated-registration-key"
        await db_session.flush()
        return result

    if phase == "confirm":
        monkeypatch.setattr(svc.locations, "perform_probe", rotate)
        call = svc.confirm(db_session, run.id, content_world[2], api_key_id=key_id)
    else:
        monkeypatch.setattr(svc, "probe_candidates", rotate)
        call = svc.start_preflight(
            db_session, content_world[6].id, content_world[2], api_key_id=key_id
        )
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 403
    assert await db_session.scalar(select(func.count()).select_from(DatasetItem)) == 0


async def test_write_only_key_can_confirm_without_read_scope(
    db_session, content_world, httpx_client, super_admin
):
    key = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "registration-write-only", "scopes": ["datasets:write"]},
    )
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    confirmed, created = await svc.confirm(
        db_session, run.id, content_world[2], api_key_id=uuid.UUID(key.json()["id"])
    )
    assert created and confirmed.phase == "registering"


async def test_confirmation_capacity_failure_preserves_ready_files(
    db_session, content_world, monkeypatch
):
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)

    async def busy(*args, **kwargs):
        raise snapshots.ContentError("snapshot_capacity_exceeded", 429)

    monkeypatch.setattr(snapshots, "capture", busy)
    with pytest.raises(snapshots.ContentError, match="snapshot_capacity_exceeded"):
        await svc.confirm(db_session, run.id, content_world[2])
    await db_session.refresh(run)
    assert run.counts["ready_count"] == 1 and run.counts["error_count"] == 0


async def test_confirmation_refuses_new_preflight_completed_during_io(
    db_session, content_world, monkeypatch
):
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    original = snapshots.capture

    async def newer_attempt(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        run.generation += 1
        await db_session.commit()
        return snapshot

    monkeypatch.setattr(snapshots, "capture", newer_attempt)
    with pytest.raises(HTTPException) as exc:
        await svc.confirm(db_session, run.id, content_world[2])
    assert exc.value.status_code == 409 and run.phase == "preflight"


async def test_confirmation_reads_ready_candidate_after_eight_missing_files(
    db_session, content_world
):
    for i in range(8):
        # Force missing entries ahead of the actual image in the keyset order.
        db_session.add(
            DatasetScanEntry(
                run_id=content_world[6].id,
                path_digest=f"{i:064x}",
                relative_path=f"missing-{i}.png",
                kind="file",
                state="observed",
                observed={"size": 1, "mtime_ns": 1, "ctime_ns": 1, "identity": {}},
            )
        )
    await db_session.flush()
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    assert run.counts["error_count"] == 8 and run.counts["ready_count"] == 1
    _, created = await svc.confirm(db_session, run.id, content_world[2])
    assert created and run.phase == "registering"


async def test_registration_routes_accept_asynchronously_and_enforce_read_scope(
    db_session, content_world, httpx_client, super_admin, monkeypatch
):
    async def dispatched(*args):
        return

    monkeypatch.setattr(worker, "dispatch", dispatched)
    url = f"/api/v1/dataset-scans/{content_world[6].id}/registration"
    response = await httpx_client.post(url, headers=headers(super_admin))
    assert response.status_code == 202, response.text
    key = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "write-only-ledger", "scopes": ["datasets:write"]},
    )
    auth = {"Authorization": f"Bearer {key.json()['plaintext']}"}
    assert (await httpx_client.get(url, headers=auth)).status_code == 403
    entries_url = f"/api/v1/dataset-registrations/{response.json()['id']}/entries"
    assert (await httpx_client.get(entries_url, headers=auth)).status_code == 403


async def test_expired_attempt_resumes_checkpoint_and_rejects_old_fence(
    db_session, content_world
):
    run, task_id, fence = await start(db_session, content_world)
    work = await svc.next_entry(db_session, run.id, task_id, fence)
    await db_session.commit()
    await worker.process_one(db_session, run.id, task_id, fence, work)
    run.lease_expires_at = await scans.now(db_session) - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(svc.LostRegistrationLease):
        await svc.next_entry(db_session, run.id, task_id, fence)
    await svc.resume(db_session, run.id, content_world[2])
    await db_session.commit()
    job = await db_session.get(AsyncJob, run.async_job_id)
    new_fence = await svc.claim(db_session, run.id, job.celery_task_id, "content@test")
    await db_session.commit()
    await drain(db_session, run, job.celery_task_id, new_fence)
    assert run.status == "completed" and run.counts["ready_count"] == 1
    with pytest.raises(svc.LostRegistrationLease):
        await svc.record(
            db_session, run.id, task_id, fence, work["path_digest"], "error"
        )


async def test_cancelled_registration_survives_task_history_cleanup(
    db_session, content_world
):
    from sqlalchemy import delete

    run, _, _ = await start(db_session, content_world)
    await svc.cancel_job(db_session, run.async_job_id, content_world[2])
    await db_session.commit()
    await db_session.execute(delete(AsyncJob).where(AsyncJob.id == run.async_job_id))
    await db_session.commit()
    await db_session.refresh(run)
    assert run.async_job_id is None
    await svc.resume(db_session, run.id, content_world[2])
    await db_session.commit()
    assert run.async_job_id is not None and run.counts["pending_count"] == 1
