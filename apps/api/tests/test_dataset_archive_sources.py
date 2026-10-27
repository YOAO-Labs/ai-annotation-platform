"""Archive fences source writes while retained task content remains readable."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import DatasetRegistrationRun
from app.schemas.dataset_source import (
    SourceLifecycleChange,
    SourceRulesApply,
    SourceRulesPreview,
    SourceSyncScheduleUpdate,
)
from app.services import (
    content_snapshot,
    dataset_registration as registrations,
    dataset_scans as scans,
    dataset_source_lifecycle as lifecycle,
    dataset_source_rules as rules,
    dataset_source_sync as sync,
    storage_location,
    dataset_source_revisions as revisions,
)
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_dataset_registration import drain
from tests.test_dataset_source_revisions import candidate
from tests.test_dataset_source_relocations import moved_candidate


async def transition(db, dataset, *, archived):
    """Commit the owner state independently of the source operation being tested."""
    dataset.archived_at = datetime.now(timezone.utc) if archived else None
    dataset.archived_by = dataset.created_by if archived else None
    dataset.lifecycle_revision += 1
    await db.commit()


def no_source_io(monkeypatch):
    async def unexpected(*_args, **_kwargs):
        pytest.fail("An archive fence must reject before source I/O")

    monkeypatch.setattr(content_snapshot, "capture", unexpected)
    monkeypatch.setattr(storage_location, "perform_probe", unexpected)


@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "update",
        "scan",
        "verify",
        "register",
        "preflight",
        "lifecycle",
        "rules_preview",
        "rules_apply",
        "schedule",
    ],
)
async def test_archived_dataset_rejects_source_write_routes_without_io(
    db_session, content_world, httpx_client, super_admin, monkeypatch, operation
):
    db = db_session
    _, _, actor, dataset, location, source, scan, entry = content_world
    impact = await lifecycle.impact(db, source, actor)
    source_before = (source.enabled, source.config_revision, deepcopy(source.config))
    await transition(db, dataset, archived=True)
    no_source_io(monkeypatch)
    source_path = f"/api/v1/dataset-sources/{source.id}"
    scan_path = f"/api/v1/dataset-scans/{scan.id}"
    rules_body = {
        "expected_revision": source.config_revision,
        "recursive": True,
        "include_globs": ["**/*"],
        "exclude_globs": [],
    }
    method, path, body = {
        "create": (
            "POST",
            "/api/v1/dataset-sources",
            {
                "dataset_id": str(dataset.id),
                "location_id": str(location.id),
                "name": "Archived input",
                "node_id": "content@test",
                "relative_path": "source",
            },
        ),
        "update": (
            "PATCH",
            source_path,
            {"expected_revision": source.config_revision, "name": "Archived edit"},
        ),
        "scan": ("POST", source_path + "/scans", None),
        "verify": ("POST", scan_path + f"/entries/{entry.path_digest}/verify", None),
        "register": (
            "POST",
            scan_path + f"/entries/{entry.path_digest}/register",
            None,
        ),
        "preflight": ("POST", scan_path + "/registration", None),
        "lifecycle": (
            "POST",
            source_path + "/lifecycle",
            {
                "action": "pause",
                "expected_revision": source.config_revision,
                "impact_proof": impact["proof"],
            },
        ),
        "rules_preview": ("POST", source_path + "/rules/preview", rules_body),
        "rules_apply": (
            "POST",
            source_path + "/rules",
            {**rules_body, "impact_proof": "0" * 64},
        ),
        "schedule": (
            "PUT",
            source_path + "/sync-schedule",
            {"expected_revision": 0, "enabled": True},
        ),
    }[operation]
    response = await httpx_client.request(
        method, path, headers=headers(super_admin), json=body
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "dataset_archived"
    await db.refresh(source)
    assert (source.enabled, source.config_revision, source.config) == source_before
    assert scan.status == "completed"
    assert not await db.scalar(select(func.count()).select_from(DatasetItem))


async def test_archived_dataset_keeps_scan_and_assigned_task_reads_and_annotations(
    db_session, reference_world, httpx_client, annotator, super_admin
):
    registered, _, task, world = reference_world
    _, data, _, dataset, _, source, scan, entry = world
    await transition(db_session, dataset, archived=True)
    for path, actor in [
        (f"/api/v1/files/tasks/{task.id}/content", annotator),
        (
            f"/api/v1/dataset-scans/{scan.id}/entries/{entry.path_digest}/content",
            super_admin,
        ),
    ]:
        response = await httpx_client.get(
            path, headers=headers(actor), params={"sha256": registered["sha256"]}
        )
        assert response.status_code == 200 and response.content == data, response.text
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=headers(annotator),
        json={
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert response.status_code == 201, response.text
    assert source.enabled


@pytest.mark.parametrize("legacy", [False, True])
async def test_old_queued_scan_never_revives_after_archive_restore(
    db_session, content_world, monkeypatch, legacy
):
    db = db_session
    _, _, actor, dataset, _, source, *_ = content_world
    run, _ = await scans.start_scan(db, source.id, actor)
    assert run.snapshot["dataset_lifecycle_revision"] == 1
    if legacy:
        run.snapshot = {
            key: value
            for key, value in run.snapshot.items()
            if key != "dataset_lifecycle_revision"
        }
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    no_source_io(monkeypatch)
    result = await scans.claim(db, run.id, job.celery_task_id, "content@test")
    assert result is None
    assert run.status == job.status == "failed"
    assert run.error_code == "dataset_lifecycle_changed"


async def test_running_scan_stops_at_next_write_fence_but_can_finish_failure(
    db_session, content_world, monkeypatch
):
    db = db_session
    _, _, actor, dataset, _, source, *_ = content_world
    run, _ = await scans.start_scan(db, source.id, actor)
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await transition(db, dataset, archived=True)
    no_source_io(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await scans.next_directory(db, run.id, job.celery_task_id, fence)
    assert exc.value.detail["code"] == "dataset_archived"
    await scans.fail_attempt(db, run.id, job.celery_task_id, fence, "dataset_archived")
    assert run.status == "failed"


@pytest.mark.parametrize("operation", ["claim", "resume", "confirm"])
async def test_old_registration_and_completed_review_cannot_resume_after_restore(
    db_session, content_world, monkeypatch, operation
):
    db = db_session
    dataset, source = content_world[3], content_world[5]
    run, _ = await registrations.start_preflight(
        db, content_world[6].id, content_world[2]
    )
    assert run.snapshot["dataset_lifecycle_revision"] == 1
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    if operation == "confirm":
        fence = await registrations.claim(
            db, run.id, job.celery_task_id, "content@test"
        )
        await db.commit()
        await drain(db, run, job.celery_task_id, fence)
        assert run.status == "completed"
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    no_source_io(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        if operation == "claim":
            await registrations.claim(db, run.id, job.celery_task_id, "content@test")
        elif operation == "resume":
            await registrations.resume(db, run.id, content_world[2])
        else:
            await registrations.confirm(db, run.id, content_world[2])
    assert exc.value.detail["code"] == "dataset_lifecycle_changed"
    assert source.enabled


@pytest.mark.parametrize("operation", ["verify", "register"])
async def test_receipt_final_write_rechecks_archive_after_snapshot_io(
    db_session, content_world, httpx_client, super_admin, monkeypatch, operation
):
    db = db_session
    dataset, scan, entry = content_world[3], content_world[6], content_world[7]
    capture = content_snapshot.capture

    async def archived_after_read(*args, **kwargs):
        snapshot = await capture(*args, **kwargs)
        await transition(db, dataset, archived=True)
        return snapshot

    monkeypatch.setattr(content_snapshot, "capture", archived_after_read)
    path = f"/api/v1/dataset-scans/{scan.id}/entries/{entry.path_digest}/{operation}"
    response = await httpx_client.post(path, headers=headers(super_admin))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "dataset_archived"
    await db.refresh(entry)
    assert entry.verified_content is None
    assert not await db.scalar(select(func.count()).select_from(DatasetItem))


async def test_source_preview_proofs_are_not_reusable_after_archive_restore(
    db_session, content_world
):
    db = db_session
    actor, dataset, source = content_world[2], content_world[3], content_world[5]
    impact = await lifecycle.impact(db, source, actor)
    selection = SourceRulesPreview(
        expected_revision=source.config_revision,
        recursive=True,
        include_globs=["**/*.png"],
    )
    review = await rules.preview(db, source.id, selection, actor)
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    with pytest.raises(HTTPException) as exc:
        await lifecycle.change(
            db,
            source.id,
            SourceLifecycleChange(
                action="pause",
                expected_revision=source.config_revision,
                impact_proof=impact["proof"],
            ),
            actor,
        )
    assert exc.value.detail["code"] == "source_impact_changed"
    with pytest.raises(HTTPException) as exc:
        await rules.apply(
            db,
            source.id,
            SourceRulesApply(**selection.model_dump(), impact_proof=review["proof"]),
            actor,
        )
    assert exc.value.detail["code"] == "rules_impact_changed"
    assert source.enabled and source.config_revision == 1


async def test_periodic_delegation_blocks_after_restore_until_explicit_new_cycle(
    db_session, content_world, monkeypatch
):
    db = db_session
    actor, dataset, source, scan = (
        content_world[2],
        content_world[3],
        content_world[5],
        content_world[6],
    )
    row = await sync.configure(
        db,
        source.id,
        SourceSyncScheduleUpdate(
            expected_revision=0, enabled=True, interval_minutes=15
        ),
        actor,
    )
    assert row.snapshot["dataset_lifecycle_revision"] == 1
    old = DatasetRegistrationRun(
        scan_run_id=scan.id,
        source_id=source.id,
        snapshot={"dataset_lifecycle_revision": 1},
        status="completed",
        phase="preflight",
        comparison_complete=True,
    )
    db.add(old)
    await db.flush()
    row.scan_id, row.registration_id, row.state = scan.id, old.id, "awaiting_review"
    row.next_check_at = await scans.now(db) - timedelta(seconds=1)
    await db.commit()
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    work = await sync.claim_due(db, source.id)
    await db.commit()
    assert work
    no_source_io(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await sync.process(db, *work)
    assert exc.value.detail["code"] == "dataset_lifecycle_changed"
    await db.rollback()
    await db.refresh(actor)
    await sync.record_failure(db, *work[:3], "dataset_lifecycle_changed")
    await db.commit()
    assert not row.enabled and row.state == "blocked"
    new = await sync.configure(
        db,
        source.id,
        SourceSyncScheduleUpdate(
            expected_revision=row.revision, enabled=True, interval_minutes=15
        ),
        actor,
    )
    assert new.snapshot["dataset_lifecycle_revision"] == 3
    assert new.scan_id is None and new.registration_id is None and new.state == "idle"
    assert source.enabled


@pytest.mark.parametrize("operation", ["accept", "revalidate", "relocate"])
@pytest.mark.parametrize("restored", [False, True])
async def test_completed_revision_candidates_cannot_cross_a_lifecycle_transition(
    db_session, reference_world, monkeypatch, operation, restored
):
    db = db_session
    old, _, _, world = reference_world
    if operation == "relocate":
        world, run, _, change, payload = await moved_candidate(db, world)
    else:
        world, run, change, payload = await candidate(
            db, world, "red" if operation == "revalidate" else "blue"
        )
    dataset, source = world[3], world[5]
    await transition(db, dataset, archived=True)
    if restored:
        await transition(db, dataset, archived=False)
    no_source_io(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await revisions.accept(
            db,
            run.id,
            change["path_digest"],
            payload,
            world[2],
            revalidate=operation == "revalidate",
            relocate=operation == "relocate",
        )
    assert exc.value.detail["code"] == (
        "dataset_lifecycle_changed" if restored else "dataset_archived"
    )
    assert source.enabled
    assert await db.get(DatasetItem, old["item_id"]) is not None


@pytest.mark.parametrize("operation", ["verify", "register", "registration"])
async def test_completed_scan_cannot_publish_new_receipts_or_members_after_restore(
    db_session, content_world, httpx_client, super_admin, monkeypatch, operation
):
    db = db_session
    dataset, scan, entry = content_world[3], content_world[6], content_world[7]
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    no_source_io(monkeypatch)
    path = f"/api/v1/dataset-scans/{scan.id}"
    path += (
        "/registration"
        if operation == "registration"
        else f"/entries/{entry.path_digest}/{operation}"
    )
    response = await httpx_client.post(path, headers=headers(super_admin))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "dataset_lifecycle_changed"
    assert entry.verified_content is None


async def test_explicit_new_scan_uses_restored_lifecycle_revision(
    db_session, content_world
):
    db = db_session
    actor, dataset, source = content_world[2], content_world[3], content_world[5]
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    run, created = await scans.start_scan(db, source.id, actor)
    assert created and run.snapshot["dataset_lifecycle_revision"] == 3
    assert source.enabled and source.config_revision == 1
