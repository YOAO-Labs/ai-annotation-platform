"""Archived managed datasets remain readable, but old writes cannot revive."""

from contextlib import asynccontextmanager
import io
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas._jsonb_types import SensorCalibration
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import (
    dataset_source_access as access,
    pointcloud_import,
    scene as scenes,
    sensor_calibration,
    training_datasets,
)
from app.services.dataset import DatasetService, build_tasks_for_link
from tests.factory import create_batch, create_project, create_task
from tests.test_dataset_lifecycle import apply, path, preview, world
from tests.test_pointcloud_import import _seed_dataset, _seed_scene
from tests.test_storage_maintenance_impact import (
    dataset,
    forbid_source_io,
    frozen_version,
)
from tests.test_storage_locations_api import headers
from tests.test_training_datasets import (
    reviewed_project,
    source_headers as source_headers,
)
from tests.training_original_fixtures import register_original


async def transition(client, row, actor, operation="archive", revision=1):
    review = await preview(client, row, actor, operation=operation, revision=revision)
    response = await apply(client, row, actor, review)
    assert response.status_code == 200, response.text
    return response.json()


async def archive_restore(client, row, actor):
    await transition(client, row, actor)
    restored = await transition(client, row, actor, "restore", 2)
    assert restored["state"] == "active" and restored["lifecycle_revision"] == 3


async def identities(db, dataset_id, project_id):
    """Compare persisted membership, not possibly stale ORM instances."""
    return {
        "items": set(
            await db.scalars(
                select(DatasetItem.id).where(DatasetItem.dataset_id == dataset_id)
            )
        ),
        "links": set(
            await db.scalars(
                select(ProjectDataset.id).where(ProjectDataset.dataset_id == dataset_id)
            )
        ),
        "tasks": set(
            await db.scalars(select(Task.id).where(Task.project_id == project_id))
        ),
        "annotations": set(
            await db.scalars(
                select(Annotation.id).where(Annotation.project_id == project_id)
            )
        ),
        "scenes": set(
            await db.scalars(select(Scene.id).where(Scene.dataset_id == dataset_id))
        ),
    }


def conflict(error, code="dataset_archived"):
    assert error.value.status_code == 409
    assert error.value.detail["code"] == code


async def test_managed_tickets_capture_lifecycle_and_cannot_survive_restore(
    db_session, super_admin, httpx_client
):
    db = db_session
    row = await dataset(db, super_admin[0])
    await db.commit()
    ticket = await access.capture_dataset_write(db, row.id, super_admin[0], None)
    assert ticket == {"dataset_lifecycle_revision": 1}
    await access.validate_dataset_write(db, row.id, ticket, lock=True)
    await db.commit()
    await archive_restore(httpx_client, row, super_admin)
    for obsolete in (ticket, None, {}):
        with pytest.raises(HTTPException) as error:
            await access.validate_dataset_write(db, row.id, obsolete, lock=True)
        conflict(error, "dataset_lifecycle_changed")
    fresh = await access.capture_dataset_write(db, row.id, super_admin[0], None)
    assert fresh == {"dataset_lifecycle_revision": 3}
    await access.validate_dataset_write(db, row.id, fresh, lock=True)


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("PUT", "", {"name": "must not rename"}),
        ("DELETE", "", None),
        ("POST", "/items/upload-init", {"file_name": "new.png"}),
        ("POST", "/items/upload-complete/{item_id}", None),
        ("DELETE", "/items/{item_id}", None),
        ("POST", "/items/scan", None),
        ("POST", "/backfill-dimensions", None),
        ("POST", "/backfill-media", None),
        ("POST", "/scenes/backfill", None),
        ("POST", "/import-from-connection", {"connection_id": "connection_id"}),
        ("POST", "/link", {"project_id": "project_id"}),
        ("DELETE", "/link/{project_id}", None),
    ],
)
async def test_archived_managed_api_rejects_management_without_side_effects(
    db_session, super_admin, httpx_client, monkeypatch, method, suffix, body
):
    db = db_session
    row, item, project, *_ = await world(db, super_admin[0])
    await db.commit()
    await transition(httpx_client, row, super_admin)
    before = await identities(db, row.id, project.id)
    original_name = row.name
    forbid_source_io(monkeypatch)
    ids = {
        "item_id": str(item.id),
        "project_id": str(project.id),
        "connection_id": str(uuid.uuid4()),
    }
    payload = (
        {key: ids.get(value, value) for key, value in body.items()} if body else None
    )
    response = await httpx_client.request(
        method,
        path(row) + suffix.format(**ids),
        headers=headers(super_admin),
        json=payload,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "dataset_archived"
    assert await identities(db, row.id, project.id) == before
    assert (await db.get(Dataset, row.id, populate_existing=True)).name == original_name
    assert not list(
        await db.scalars(select(AsyncJob).where(AsyncJob.project_id == project.id))
    )


async def test_archived_managed_browsing_and_private_view_crud_remain_available(
    db_session, super_admin, annotator, httpx_client, monkeypatch
):
    db, client = db_session, httpx_client
    row, item, project, *_ = await world(db, super_admin[0])
    await db.commit()
    await transition(client, row, super_admin)
    forbid_source_io(monkeypatch)
    base, auth = path(row), headers(annotator)
    detail = await client.get(base, headers=auth)
    assert detail.status_code == 200, detail.text
    assert detail.json()["archived_at"] and detail.json()["lifecycle_revision"] == 2
    for suffix in ("/items", "/items/" + str(item.id), "/projects", "/workbench"):
        response = await client.get(base + suffix, headers=headers(super_admin))
        assert response.status_code == 200, response.text
    before = await identities(db, row.id, project.id)
    created = await client.post(
        base + "/saved-views",
        headers=auth,
        json={"name": "Archived PNG", "query": {"extension": "png"}},
    )
    assert created.status_code == 201, created.text
    view_id = created.json()["id"]
    listed = await client.get(base + "/saved-views", headers=auth)
    assert listed.status_code == 200 and listed.json()["items"][0]["id"] == view_id
    changed = await client.put(
        base + "/saved-views/" + view_id,
        headers=auth,
        json={"name": "Read later", "query": {}},
    )
    assert changed.status_code == 200, changed.text
    assert (
        await client.delete(base + "/saved-views/" + view_id, headers=auth)
    ).status_code == 204
    assert await identities(db, row.id, project.id) == before


@pytest.mark.parametrize(
    "operation",
    (
        "update",
        "add_item",
        "delete",
        "delete_item",
        "create_tasks",
        "unlink",
        "link",
        "build_tasks",
        "scan",
        "ingest",
        "create_scene",
        "assign_scene",
        "calibration",
    ),
)
async def test_direct_managed_writers_reject_archived_dataset(
    db_session, super_admin, httpx_client, monkeypatch, operation
):
    db = db_session
    row, item, project, *_ = await world(db, super_admin[0])
    scene = Scene(
        dataset_id=row.id,
        display_id=f"SC-{uuid.uuid4().hex[:10]}",
        name="retained scene",
    )
    db.add(scene)
    await db.commit()
    await transition(httpx_client, row, super_admin)
    before = await identities(db, row.id, project.id)
    forbid_source_io(monkeypatch)
    svc = DatasetService(db)
    with pytest.raises(HTTPException) as error:
        if operation == "update":
            await svc.update(row.id, "must not rename", "must not rewrite")
        elif operation == "add_item":
            await svc.add_item(row.id, "new.png", "new.png", "image")
        elif operation == "delete":
            await svc.delete(row.id)
        elif operation == "delete_item":
            await svc.delete_item(item.id, dataset_id=row.id)
        elif operation == "create_tasks":
            await svc.create_tasks_for_items(row.id, [item.id])
        elif operation == "unlink":
            await svc.unlink_project(row.id, project.id)
        elif operation == "link":
            await svc.link_project(row.id, project.id)
        elif operation == "build_tasks":
            await build_tasks_for_link(db, dataset_id=row.id, project_id=project.id)
        elif operation == "scan":
            await svc.scan_and_import(row.id)
        elif operation == "ingest":
            await svc.ingest_one(row.id, "new.png", io.BytesIO(b"never uploaded"))
        elif operation == "create_scene":
            await scenes.create_scene(db, dataset_id=row.id, name="new scene")
        elif operation == "assign_scene":
            await scenes.assign_items_to_scene(
                db, scene_id=scene.id, items_in_order=[item]
            )
        else:
            await sensor_calibration.update_calibration(
                db,
                dataset_item_id=item.id,
                calibration=SensorCalibration(
                    extrinsic=[0.0] * 16, intrinsic=[0.0] * 9
                ),
                expected_revision=1,
                expected_digest="0" * 64,
                actor_id=super_admin[0].id,
            )
    conflict(error)
    assert await identities(db, row.id, project.id) == before
    retained = await db.get(DatasetItem, item.id, populate_existing=True)
    assert retained.scene_id is None and retained.frame_index is None
    assert "calibration" not in retained.metadata_


@pytest.mark.parametrize("kind", ("dataset_import", "create_tasks"))
@pytest.mark.parametrize(
    "legacy", (False, True), ids=("captured-ticket", "legacy-ticket")
)
async def test_queued_managed_workers_reject_old_lifecycle_after_restore(
    db_session, super_admin, httpx_client, monkeypatch, kind, legacy
):
    from app.workers import create_tasks, dataset_import

    db = db_session
    row, _, project, *_ = await world(db, super_admin[0])
    ticket = await access.capture_dataset_write(db, row.id, super_admin[0], None)
    payload = {
        "dataset_id": str(row.id),
        "project_id": str(project.id),
        "connection_id": str(uuid.uuid4()),
        "settings_snapshot": {
            "dataset_import_max_files": 10,
            "dataset_import_max_total_bytes": 1024,
            "version": "archive-test",
        },
    }
    if not legacy:
        payload["directory_authority"] = ticket
    job = AsyncJob(
        kind=kind, user_id=super_admin[0].id, project_id=project.id, payload=payload
    )
    db.add(job)
    await db.commit()
    await archive_restore(httpx_client, row, super_admin)
    dataset_id, project_id, job_id = row.id, project.id, job.id
    before = await identities(db, dataset_id, project_id)
    forbid_source_io(monkeypatch)

    async def no_dispose():
        pass

    @asynccontextmanager
    async def sessions():
        yield db

    # Bind the real worker to the disposable fixture transaction; its business
    # logic, commits, rollback, and terminal job handling remain intact.
    worker = dataset_import if kind == "dataset_import" else create_tasks
    monkeypatch.setattr(
        worker,
        "create_async_engine",
        lambda *args, **kwargs: SimpleNamespace(dispose=no_dispose),
    )
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: sessions)
    run = (
        worker._run_dataset_import
        if kind == "dataset_import"
        else worker._run_create_tasks
    )
    with pytest.raises(HTTPException) as error:
        await run(job_id=str(job_id), celery_task_id="archive-old-ticket")
    conflict(error, "dataset_lifecycle_changed")
    finished = await db.get(AsyncJob, job_id, populate_existing=True)
    assert (
        finished.status == "failed"
        and "dataset_lifecycle_changed" in finished.error_message
    )
    assert await identities(db, dataset_id, project_id) == before


async def test_fresh_managed_ticket_can_build_tasks_after_restore(
    db_session, super_admin, httpx_client
):
    db = db_session
    row, _, project, *_ = await world(db, super_admin[0])
    new_item = DatasetItem(
        dataset_id=row.id, file_name="waiting.png", file_path="waiting.png"
    )
    db.add(new_item)
    await db.commit()
    await archive_restore(httpx_client, row, super_admin)
    ticket = await access.capture_dataset_write(db, row.id, super_admin[0], None)
    result = await build_tasks_for_link(
        db,
        dataset_id=row.id,
        project_id=project.id,
        directory_authority=ticket,
        chunk_size=1,
    )
    assert result == {"created": 1, "total": 1}
    linked = await db.scalar(
        select(Task).where(
            Task.project_id == project.id, Task.dataset_item_id == new_item.id
        )
    )
    assert linked and linked.file_path == new_item.file_path


async def test_pointcloud_old_ticket_cannot_infer_scene_or_create_frame_links(
    db_session, super_admin, httpx_client, monkeypatch
):
    db = db_session
    row = await _seed_dataset(db, super_admin[0].id)
    await _seed_scene(db, row, frames=["0001", "0002"], cameras=["front"])
    project = await create_project(db, owner_id=super_admin[0].id)
    project.data_type = "lidar"
    db.add(ProjectDataset(dataset_id=row.id, project_id=project.id))
    await db.commit()
    ticket = await access.capture_dataset_write(db, row.id, super_admin[0], None)
    await archive_restore(httpx_client, row, super_admin)
    before = await identities(db, row.id, project.id)
    forbid_source_io(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await pointcloud_import.build_pointcloud_tasks_for_link(
            db,
            dataset_id=row.id,
            project_id=project.id,
            directory_authority=ticket,
            chunk_size=1,
        )
    conflict(error, "dataset_lifecycle_changed")
    assert await identities(db, row.id, project.id) == before
    assert not before["scenes"] and not before["tasks"]
    assert all(
        item.scene_id is None
        for item in await db.scalars(
            select(DatasetItem).where(DatasetItem.dataset_id == row.id)
        )
    )


async def test_pointcloud_committed_chunk_is_retained_but_old_ticket_stops_next_chunk(
    db_session, super_admin, httpx_client, monkeypatch
):
    db = db_session
    row = await _seed_dataset(db, super_admin[0].id)
    await _seed_scene(db, row, frames=["0001", "0002"], cameras=["front"])
    project = await create_project(db, owner_id=super_admin[0].id)
    project.data_type = "lidar"
    db.add(ProjectDataset(dataset_id=row.id, project_id=project.id))
    await db.commit()
    ticket = await access.capture_dataset_write(db, row.id, super_admin[0], None)
    original_commit = db.commit
    changed = False

    async def archive_at_chunk_boundary():
        nonlocal changed
        await original_commit()
        if not changed:
            changed = True
            # Exercise a real lifecycle transition after a durable worker chunk,
            # before its next chunk. Restoring must not revive the old request.
            await archive_restore(httpx_client, row, super_admin)

    monkeypatch.setattr(db, "commit", archive_at_chunk_boundary)
    forbid_source_io(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await pointcloud_import.build_pointcloud_tasks_for_link(
            db,
            dataset_id=row.id,
            project_id=project.id,
            directory_authority=ticket,
            chunk_size=1,
        )
    conflict(error, "dataset_lifecycle_changed")
    partial = list(await db.scalars(select(Task).where(Task.project_id == project.id)))
    assert changed and len(partial) == 1
    links = list(
        await db.scalars(
            select(TaskDatasetItemLink).where(
                TaskDatasetItemLink.task_id == partial[0].id
            )
        )
    )
    assert {link.role for link in links} == {"primary_lidar", "camera_front"}
    fresh = await access.capture_dataset_write(db, row.id, super_admin[0], None)
    result = await pointcloud_import.build_pointcloud_tasks_for_link(
        db,
        dataset_id=row.id,
        project_id=project.id,
        directory_authority=fresh,
        chunk_size=1,
    )
    assert result == {"created": 1, "total": 1}
    complete = set(
        await db.scalars(select(Task.id).where(Task.project_id == project.id))
    )
    assert len(complete) == 2 and partial[0].id in complete


async def managed_training_world(db, owner, *, original_objects=None):
    project, tasks = await reviewed_project(db, owner)
    row = await dataset(db, owner)
    batch = await create_batch(db, project_id=project.id)
    db.add(ProjectDataset(dataset_id=row.id, project_id=project.id))
    for task in tasks:
        item = DatasetItem(
            dataset_id=row.id,
            file_name=task.file_name,
            file_path=task.file_path,
            width=32,
            height=24,
        )
        db.add(item)
        await db.flush()
        task.dataset_item_id = item.id
        task.batch_id = batch.id
        if original_objects is not None:
            await register_original(db, task, owner, provider=original_objects)
    await db.commit()
    return row, project, tasks, batch


@pytest.mark.parametrize("selector", ("all", "task_ids", "dataset", "batch"))
async def test_new_freeze_rejects_archived_eligible_managed_membership(
    db_session, super_admin, httpx_client, monkeypatch, selector
):
    db = db_session
    row, project, tasks, batch = await managed_training_world(db, super_admin[0])
    await transition(httpx_client, row, super_admin)
    forbid_source_io(monkeypatch)

    async def unexpected_media(*args, **kwargs):
        pytest.fail("Archived new selections must be rejected before media capture")

    monkeypatch.setattr(training_datasets.originals, "preflight", unexpected_media)
    selected = {
        "all": {},
        "task_ids": {"task_ids": [task.id for task in tasks]},
        "dataset": {"dataset_id": row.id},
        "batch": {"batch_id": batch.id},
    }[selector]
    with pytest.raises(HTTPException) as error:
        await training_datasets.freeze_selection(
            db,
            project,
            TrainingDatasetSelection(**selected),
            actor_id=super_admin[0].id,
            trusted_actor=True,
        )
    conflict(error)


@pytest.mark.parametrize("selection", ("all", "explicit-active"))
async def test_archived_excluded_or_unselected_rows_do_not_block_new_freeze(
    db_session, super_admin, httpx_client, source_headers, selection
):
    db = db_session
    row, project, tasks, _ = await managed_training_world(
        db, super_admin[0], original_objects=source_headers
    )
    archived = await dataset(db, super_admin[0])
    rejected_item = DatasetItem(
        dataset_id=archived.id,
        file_name="not-reviewed.png",
        file_path="not-reviewed.png",
    )
    db.add(rejected_item)
    await db.flush()
    excluded = await create_task(db, project_id=project.id)
    excluded.dataset_item_id = rejected_item.id
    db.add(ProjectDataset(dataset_id=archived.id, project_id=project.id))
    await db.commit()
    await transition(httpx_client, archived, super_admin)
    selected = (
        TrainingDatasetSelection(task_ids=[task.id for task in tasks])
        if selection == "explicit-active"
        else TrainingDatasetSelection()
    )
    snapshot, report = await training_datasets.freeze_selection(
        db, project, selected, actor_id=super_admin[0].id, trusted_actor=True
    )
    assert report["ready"], report
    assert {sample["task_id"] for sample in snapshot["samples"]} == {
        str(task.id) for task in tasks
    }
    assert report["summary"]["eligible"] == 3
    assert report["excluded"] == (
        []
        if selection == "explicit-active"
        else [{"task_id": str(excluded.id), "code": "not_reviewed"}]
    )
    assert (await db.get(Dataset, row.id, populate_existing=True)).archived_at is None


async def test_existing_frozen_version_remains_readable_after_dataset_archive(
    db_session, super_admin, httpx_client, source_headers, monkeypatch
):
    from app.workers.training_dataset import assert_dataset_authority

    db = db_session
    row, project, _, _ = await managed_training_world(
        db, super_admin[0], original_objects=source_headers
    )
    snapshot, report = await training_datasets.freeze_selection(
        db,
        project,
        TrainingDatasetSelection(),
        actor_id=super_admin[0].id,
        trusted_actor=True,
    )
    assert report["ready"]
    version = await frozen_version(db, project.id, snapshot, "ready")
    version.creator_id = super_admin[0].id
    await db.commit()
    await transition(httpx_client, row, super_admin)
    forbid_source_io(monkeypatch)
    # Existing packaging still checks current project authority, rather than
    # treating a dataset archive as revocation of its immutable snapshot.
    await assert_dataset_authority(db, version)
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/training-datasets/{version.id}",
        headers=headers(super_admin),
    )
    assert response.status_code == 200, response.text
    await db.refresh(version)
    assert version.status == "ready" and version.snapshot == snapshot
