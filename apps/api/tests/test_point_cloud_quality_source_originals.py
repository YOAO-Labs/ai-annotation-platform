"""Real PCD ownership, continuing reviewer authority and atomic publication."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
from types import SimpleNamespace
import uuid

import pytest
import numpy as np
from fastapi import HTTPException
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.notification import Notification
from app.db.models.point_cloud_quality import PointCloudQualityIssue
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.project import Project
from app.db.models.user import User
from app.db.models.task import Task
from app.db.models.scene_track import SceneTrack
from app.schemas.point_cloud_quality import PointCloudQualityRunRequest
from app.services import async_job as jobs
from app.services.api_key_service import create_key
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.point_cloud_quality import originals, service
from app.workers import point_cloud_quality as worker
from tests.factory import create_membership, create_user
from tests.test_original_binary import request
from tests.test_point_cloud_quality_api import (
    PointCloudObjects,
    _seed_quality_scene,
    _enter_running,
    _pcd_bytes,
)


@pytest.fixture
async def current(db_session, super_admin, monkeypatch):
    owner, _token = super_admin
    objects = PointCloudObjects().install(monkeypatch)
    project, scene, tasks, track, annotations = await _seed_quality_scene(
        db_session,
        owner_id=owner.id,
        original_objects=objects,
    )
    # Quality currently has no Key scope dependency. Preserve that boundary.
    key, token = await create_key(
        db_session, owner, "PCD original", ["annotations:read"]
    )
    selection = PointCloudQualityRunRequest(scope="scene_ids", scene_ids=[scene.id])

    async def create(selection=selection, actor=owner, proof=None):
        proof = proof or request(owner, token=token, key_id=key.id, scopes=key.scopes)
        run, job, created = await service.create_quality_run(
            db_session,
            project=project,
            actor_id=actor.id,
            request=selection,
            http_request=proof,
        )
        assert created and job
        return run, job

    async def scan():
        run, job = await create()
        await _enter_running(db_session, run, job)
        result = await worker.execute_scan(db_session, run)
        return run, job, result

    yield SimpleNamespace(
        db=db_session,
        owner=owner,
        key=key,
        token=token,
        objects=objects,
        project=project,
        scene=scene,
        tasks=tasks,
        track=track,
        annotations=annotations,
        create=create,
        scan=scan,
    )
    assert all(body.closed for body in objects.bodies)


async def issues(db, project):
    return list(
        await db.scalars(
            select(PointCloudQualityIssue).where(
                PointCloudQualityIssue.project_id == inspect(project).identity[0]
            )
        )
    )


async def test_saved_bucket_complete_pcd_receipt_and_hidden_key_proof(
    current, httpx_client, monkeypatch
):
    from app.services.storage import storage_service

    monkeypatch.setattr(storage_service, "datasets_bucket", "wrong-current-datasets")
    for task in current.tasks.values():
        current.objects.put(
            "wrong-current-datasets", task.file_path, b"same-key wrong PCD"
        )
    run, job, summary = await current.scan()
    assert summary["issue_count"] > 0 and not summary["skips"]
    assert all(
        bucket == "saved-quality-originals"
        for op, bucket, _ in current.objects.calls
        if op != "close"
    )
    found = await issues(current.db, current.project)
    sourced = [row for row in found if row.evidence.get("pointcloud_item_id")]
    assert sourced and all(row.evidence["pointcloud_verified"] for row in sourced)
    assert all(
        set(row.evidence["pointcloud_original"]) == originals._FIELDS for row in sourced
    )
    assert await service.refresh_issue_staleness(current.db, sourced[0]) is False
    response = await httpx_client.get(
        f"/api/v1/async-jobs/{job.id}",
        headers={"Authorization": "Bearer " + current.token},
    )
    assert response.status_code == 200, response.text
    assert originals.AUTHORITY not in response.json()["payload"]
    assert (
        originals.AUTHORITY in job.payload and run.scope_json["original_contract"] == 1
    )


@pytest.mark.parametrize("boundary", ["parse", "close"])
@pytest.mark.parametrize("change", ["key", "actor", "gate", "owner", "axis"])
async def test_late_changes_refuse_publication_after_real_close(
    current, monkeypatch, boundary, change
):
    run, job = await current.create()
    await _enter_running(current.db, run, job)
    changed = False
    operation = (
        originals.bounded_thread if boundary == "parse" else originals.close_stage
    )

    async def mutate():
        nonlocal changed
        if changed:
            return
        changed = True
        if change == "key":
            current.key.key_hash = "rotated-key-hash"
        elif change == "actor":
            current.owner.is_active = False
        elif change == "gate":
            gate = await current.db.get(
                StorageMaintenanceGate,
                service.originals.original_sources.LEGACY_LOCATION_ID,
            )
            gate.revision += 1
        elif change == "owner":
            for task in current.tasks.values():
                replacement = next(
                    other for other in current.tasks.values() if other.id != task.id
                )
                task.dataset_item_id, task.file_path = (
                    replacement.dataset_item_id,
                    replacement.file_path,
                )
        else:
            dataset = await current.db.get(Dataset, current.scene.dataset_id)
            dataset.metadata_ = {"axis_convention": "kitti_camera"}
        await current.db.flush()

    async def wrapped(*args, **kwargs):
        result = await operation(*args, **kwargs)
        if boundary == "close" or args[0] is originals.parse_points:
            await mutate()
        return result

    monkeypatch.setattr(
        originals, "bounded_thread" if boundary == "parse" else "close_stage", wrapped
    )
    with pytest.raises((HTTPException, originals.ContentError)) as caught:
        await worker.execute_scan(current.db, run)
    if change in {"key", "actor"}:
        assert (
            isinstance(caught.value, HTTPException) and caught.value.status_code == 403
        )
    else:
        assert isinstance(caught.value, originals.ContentError)
    assert changed and not await issues(current.db, current.project)
    assert all(body.closed for body in current.objects.bodies)


async def test_reviewer_key_without_training_scopes_and_late_task_visibility(current):
    reviewer = await create_user(
        current.db, "employee", f"pcd-review-{uuid.uuid4()}@test.local", "Reviewer"
    )
    await create_membership(
        current.db, project_id=current.project.id, user_id=reviewer.id, role="reviewer"
    )
    task = current.tasks[0]
    task.reviewer_id = reviewer.id
    key, token = await create_key(
        current.db, reviewer, "Only annotation read", ["annotations:read"]
    )
    await current.db.flush()
    run, job = await current.create(
        PointCloudQualityRunRequest(scope="task_ids", task_ids=[task.id]),
        reviewer,
        request(reviewer, token=token, key_id=key.id, scopes=key.scopes),
    )
    await _enter_running(current.db, run, job)
    context = await originals.delegated(current.db, run)
    record = next(row for row in run.source_snapshot if row["kind"] == "task")
    source = await originals.freeze(current.db, context, record)
    task.reviewer_id = None
    await current.db.flush()
    with pytest.raises(HTTPException) as caught:
        await originals.read_points(current.db, context, source, "iso_8855")
    assert caught.value.status_code == 404
    assert not current.objects.calls and not await issues(current.db, current.project)


@pytest.mark.parametrize("change", ["axis", "owner", "legacy"])
async def test_current_issue_identity_includes_original_owner_and_axis(current, change):
    run, _job, _ = await current.scan()
    issue = next(
        row
        for row in await issues(current.db, current.project)
        if row.evidence.get("pointcloud_item_id")
    )
    assert await service.refresh_issue_staleness(current.db, issue) is False
    if change == "axis":
        dataset = await current.db.get(Dataset, current.scene.dataset_id)
        dataset.metadata_ = {"axis_convention": "kitti_camera"}
    elif change == "owner":
        task = next(task for task in current.tasks.values() if task.id == issue.task_id)
        other = next(other for other in current.tasks.values() if other.id != task.id)
        task.dataset_item_id, task.file_path = other.dataset_item_id, other.file_path
    else:
        issue.evidence = {
            k: v
            for k, v in issue.evidence.items()
            if not k.startswith("pointcloud_original") and k != "pointcloud_contract"
        }
    await current.db.flush()
    assert await service.refresh_issue_staleness(current.db, issue) is True
    assert issue.status == "stale"
    assert run.source_snapshot_digest == originals.canonical_digest(run.source_snapshot)


async def test_last_pcd_close_cancellation_publishes_no_partial_issues(
    current, monkeypatch
):
    run, job = await current.create()
    close, count = originals.close_stage, 0

    async def close_then_cancel(stage):
        nonlocal count
        await close(stage)
        count += 1
        if count == 2:
            await jobs.request_cancel(current.db, job.id)
            await current.db.commit()

    @asynccontextmanager
    async def session():
        yield current.db

    monkeypatch.setattr(originals, "close_stage", close_then_cancel)
    monkeypatch.setattr(worker, "task_session", session)
    await worker._run(run.id, job.celery_task_id)
    await current.db.refresh(run)
    await current.db.refresh(job)
    assert count == 2 and run.status == job.status == "cancelled"
    assert not await issues(current.db, current.project)


async def test_mid_publication_failure_rolls_back_new_issues(current, monkeypatch):
    run, job = await current.create()
    write = worker._upsert_finding
    writes = 0

    async def failing(*args, **kwargs):
        nonlocal writes
        await write(*args, **kwargs)
        writes += 1
        raise RuntimeError("publication interrupted after issue flush")

    @asynccontextmanager
    async def session():
        yield current.db

    monkeypatch.setattr(worker, "task_session", session)
    monkeypatch.setattr(worker, "_upsert_finding", failing)
    await worker._run(run.id, job.celery_task_id)
    await current.db.refresh(run)
    await current.db.refresh(job)
    assert writes == 1 and run.status == job.status == "failed"
    assert not await issues(current.db, current.project)


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_legacy_pending_run_never_reads_or_adopts_current_originals(
    current, monkeypatch, status
):
    run, job = await current.create()
    if status == "running":
        await _enter_running(current.db, run, job)
    run.scope_json = {
        k: v for k, v in run.scope_json.items() if k != "original_contract"
    }
    job.payload = {k: v for k, v in job.payload.items() if k != originals.AUTHORITY}
    await current.db.commit()
    before = len(current.objects.calls)

    @asynccontextmanager
    async def session():
        yield current.db

    monkeypatch.setattr(worker, "task_session", session)
    await worker._run(run.id, job.celery_task_id)
    await current.db.refresh(run)
    assert run.status == "stale" and run.error_message == "original_input_unrecorded"
    assert len(current.objects.calls) == before and not await issues(
        current.db, current.project
    )


@pytest.mark.parametrize("value", [None, ""])
async def test_empty_axis_preserves_iso_default(current, value):
    dataset = await current.db.get(Dataset, current.scene.dataset_id)
    dataset.metadata_ = {"axis_convention": value}
    await current.db.flush()
    _run, _job, summary = await current.scan()
    assert summary["issue_count"] > 0 and not summary["skips"]
    sourced = [
        row
        for row in await issues(current.db, current.project)
        if row.evidence.get("pointcloud_item_id")
    ]
    assert sourced and all(
        row.evidence["axis_convention"] == "iso_8855" for row in sourced
    )
    assert not await service.refresh_issue_staleness(current.db, sourced[0])


@pytest.mark.parametrize("boundary", ["issue_flush", "notification"])
async def test_key_expiry_during_publication_rolls_back_every_issue(
    current, monkeypatch, boundary
):
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    current.key.expires_at = deadline
    await current.db.flush()
    run, job = await current.create()
    operation = (
        worker._upsert_finding
        if boundary == "issue_flush"
        else worker.notify_job_terminal
    )
    calls = 0

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline + timedelta(seconds=1)

    async def expire_after_write(*args, **kwargs):
        nonlocal calls
        result = await operation(*args, **kwargs)
        calls += 1
        monkeypatch.setattr(originals.access, "datetime", ExpiredClock)
        return result

    @asynccontextmanager
    async def session():
        yield current.db

    monkeypatch.setattr(worker, "task_session", session)
    monkeypatch.setattr(
        worker,
        "_upsert_finding" if boundary == "issue_flush" else "notify_job_terminal",
        expire_after_write,
    )
    await worker._run(run.id, job.celery_task_id)
    await current.db.refresh(run)
    await current.db.refresh(job)
    assert calls and run.status == job.status == "failed"
    assert not await issues(current.db, current.project)
    notifications = list(
        await current.db.scalars(
            select(Notification).where(Notification.target_id == job.id)
        )
    )
    assert [row.type for row in notifications] == ["job.failed"]


async def test_success_notifies_once_and_failed_rescan_preserves_human_review(
    current, monkeypatch
):
    @asynccontextmanager
    async def session():
        yield current.db

    monkeypatch.setattr(worker, "task_session", session)
    run, job = await current.create()
    await worker._run(run.id, job.celery_task_id)
    await worker._run(run.id, job.celery_task_id)
    found = await issues(current.db, current.project)
    assert found
    issue = found[0]
    issue.status = "resolved"
    issue.review_verdict = "false_positive"
    await current.db.commit()
    saved = (issue.id, issue.evidence, issue.last_seen_run_id)
    notifications = list(
        await current.db.scalars(
            select(Notification).where(Notification.target_id == job.id)
        )
    )
    assert [row.type for row in notifications] == ["job.completed"]
    # An explicit fresh scan may find the same issues, but must not publish half
    # its updated evidence if final publication fails.
    run.status = "failed"
    await current.db.commit()
    next_run, next_job = await current.create()
    write = worker._upsert_finding

    async def failing(*args, **kwargs):
        await write(*args, **kwargs)
        raise RuntimeError("rescan publication interrupted")

    monkeypatch.setattr(worker, "_upsert_finding", failing)
    await worker._run(next_run.id, next_job.celery_task_id)
    found = await issues(current.db, current.project)
    retained = next(row for row in found if row.id == saved[0])
    assert (retained.evidence, retained.last_seen_run_id) == saved[1:]
    assert retained.status == "resolved" and retained.review_verdict == "false_positive"


async def test_same_path_with_distinct_content_owners_never_shares_point_cache(current):
    path = current.tasks[0].file_path
    raw = _pcd_bytes(np.tile([20.0, 0.0, 1.2], (32, 1)))
    digest = hashlib.sha256(raw).hexdigest()
    item = DatasetItem(
        dataset_id=current.scene.dataset_id,
        scene_id=current.scene.id,
        frame_index=2,
        file_name="different-owner.pcd",
        file_path=path,
        file_type="point_cloud",
        content_hash=digest,
        file_size=len(raw),
    )
    current.db.add(item)
    await current.db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="point_cloud",
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=digest,
        size_bytes=len(raw),
    )
    current.db.add(asset)
    await current.db.flush()
    bucket = "other-saved-quality-originals"
    current.db.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket=bucket,
            object_key=path,
            role="managed_original",
            reference_state="present",
        )
    )
    current.objects.put(bucket, path, raw)
    current.tasks[2].dataset_item_id = item.id
    current.tasks[2].file_path = path
    await current.db.flush()
    await current.scan()
    low = [
        row
        for row in await issues(current.db, current.project)
        if row.code == "low_point_count"
    ]
    assert [row.task_id for row in low] == [current.tasks[0].id]
    reads = {(bucket, key) for op, bucket, key in current.objects.calls if op == "get"}
    assert ("saved-quality-originals", path) in reads and (bucket, path) in reads


async def test_concurrent_duplicate_delivery_claims_once(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    owned = {}
    try:
        async with maker() as db:
            owner = await create_user(
                db, "super_admin", f"pcd-claim-{uuid.uuid4()}@test.local", "Claim"
            )
            owned[User] = owner.id
            project, scene, *_ = await _seed_quality_scene(
                db, owner_id=owner.id, original_objects=PointCloudObjects()
            )
            owned[Project], owned[Dataset] = project.id, scene.dataset_id
            run, job, _ = await service.create_quality_run(
                db,
                project=project,
                actor_id=owner.id,
                request=PointCloudQualityRunRequest(
                    scope="scene_ids", scene_ids=[scene.id]
                ),
                http_request=request(owner),
            )
            owned[AsyncJob] = job.id
            run_id, delivery = run.id, job.celery_task_id
            await db.commit()

        async def take():
            async with maker() as db:
                return await worker.claim(db, run_id, delivery)

        results = await asyncio.gather(take(), take())
        assert sum(result is not None for result in results) == 1
        async with maker() as db:
            assert (await db.get(AsyncJob, owned[AsyncJob])).status == "running"
    finally:
        async with maker() as db:
            if Project in owned:
                for model in (Annotation, SceneTrack, Task):
                    await db.execute(
                        delete(model).where(model.project_id == owned[Project])
                    )
            for model in (Project, Dataset, AsyncJob, User):
                if model in owned:
                    await db.execute(delete(model).where(model.id == owned[model]))
            await db.commit()
