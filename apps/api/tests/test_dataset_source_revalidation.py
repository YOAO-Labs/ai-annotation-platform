"""Explicit same-content observations preserve business identity and labels."""

import copy

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetSourceItem,
    DatasetSourceRevision,
)
from app.db.models.task import Task
from app.services import (
    dataset_source_changes as changes,
    dataset_source_revisions as revisions,
)
from app.services.dataset_references import register_entry
from app.services.content_snapshot import ContentError
from app.services.filesystem_storage import FilesystemStorageError
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_directory_policies import object_store as object_store
from tests.test_s3_references import s3_world as s3_world
from tests.test_sftp_references import sftp_world as sftp_world
from tests.test_dataset_source_revisions import candidate
from tests.test_dataset_source_changes import rescan, preflight


async def test_revalidation_restores_original_task_and_preserves_labels(
    db_session, reference_world, httpx_client, super_admin, annotator
):
    db = db_session
    old, project, task, world = reference_world
    task_id, item_id = task.id, old["item_id"]
    annotation = await httpx_client.post(
        f"/api/v1/tasks/{task_id}/annotations",
        headers=headers(annotator),
        json={
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert annotation.status_code == 201
    original_ref = await db.get(DatasetReference, item_id)
    original_run_id = original_ref.run_id
    original_meta = copy.deepcopy((await db.get(DatasetItem, item_id)).metadata_)
    original_task = (task.id, task.file_path, task.assignee_id, task.status)
    content_url = f"/api/v1/files/tasks/{task_id}/content?sha256={old['sha256']}"
    for number in (2, 3):
        world, run, change, payload = await candidate(db, world, "red")
        assert change["reason"] == "observation_changed" and change["can_revalidate"]
        assert not change["can_accept_revision"]
        assert (
            await httpx_client.get(content_url, headers=headers(annotator))
        ).status_code == 409
        # Ordinary registration is not permission to renew an old reference.
        plain = await register_entry(db, world[6].id, change["path_digest"], world[2])
        assert not plain["created"]
        assert (
            await httpx_client.get(content_url, headers=headers(annotator))
        ).status_code == 409
        url = f"/api/v1/dataset-registrations/{run.id}/changes/{change['path_digest']}/revalidate"
        response = await httpx_client.post(
            url, json=payload.model_dump(mode="json"), headers=headers(super_admin)
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert (
            result["item_id"] == str(item_id)
            and not result["created"]
            and result["revision_number"] == number
        )
        replay = await httpx_client.post(
            url, json=payload.model_dump(mode="json"), headers=headers(super_admin)
        )
        assert replay.status_code == 200 and replay.json() == result
        read = await httpx_client.get(content_url, headers=headers(annotator))
        assert read.status_code == 200 and read.content == world[1]
        page = await changes.page(db, run, "changed", 20, 0)
        assert (
            page["items"][0]["accepted_item_id"] == item_id
            and not page["items"][0]["can_revalidate"]
        )
    await db.refresh(original_ref)
    await db.refresh(task)
    assert original_ref.run_id == original_run_id
    assert (task.id, task.file_path, task.assignee_id, task.status) == original_task
    assert (await db.get(DatasetItem, item_id)).metadata_ == original_meta
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1
    assert await db.scalar(select(func.count()).select_from(DatasetAsset)) == 1
    assert await db.scalar(select(func.count()).select_from(Task)) == 1
    annotations = await httpx_client.get(
        f"/api/v1/tasks/{task_id}/annotations", headers=headers(annotator)
    )
    assert annotations.json()[0]["id"] == annotation.json()["id"]
    world = await rescan(db, world)
    next_run = await preflight(db, world)
    assert (await changes.page(db, next_run, None, 20, 0))["counts"]["unchanged"] == 1
    world, new_run, change, payload = await candidate(db, world, "blue")
    new = await revisions.accept(
        db, new_run.id, change["path_digest"], payload, world[2]
    )
    assert new["item_id"] != item_id
    assert (
        await httpx_client.get(content_url, headers=headers(annotator))
    ).status_code == 409


@pytest.mark.parametrize("operation", ["revalidate", "relocate"])
async def test_copy_revalidation_preserves_ownership_without_new_original(
    db_session, content_world, object_store, monkeypatch, operation
):
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.services.storage import storage_service
    from tests.fakes.managed_creation_storage import client_error

    db, world = db_session, content_world
    world[5].ownership_mode = "managed_copy"
    await db.commit()
    old = await register_entry(db, world[6].id, world[7].path_digest, world[2])
    item = await db.get(DatasetItem, old["item_id"])
    original_path = item.file_path
    asset_id = await db.scalar(
        select(DatasetAsset.id).where(DatasetAsset.dataset_item_id == item.id)
    )
    originals = select(ManagedOriginalCreation.id).where(
        ManagedOriginalCreation.dataset_id == world[3].id
    )
    original_ids = set(await db.scalars(originals))
    stored = dict(object_store)
    if operation == "relocate":
        from tests.test_dataset_source_relocations import moved_candidate

        world, run, _, change, payload = await moved_candidate(db, world)
    else:
        world, run, change, payload = await candidate(db, world, "red")

    def reject_original_write(**_kwargs):
        raise client_error("AccessDenied", 403, "PutObject")

    monkeypatch.setattr(storage_service.client, "put_object", reject_original_write)
    monkeypatch.setattr(
        storage_service.client, "create_multipart_upload", reject_original_write
    )
    result = await revisions.accept(
        db,
        run.id,
        change["path_digest"],
        payload,
        world[2],
        **{operation: True},
    )
    assert result["item_id"] == old["item_id"] and not result["created"]
    head = await db.get(DatasetSourceItem, (world[5].id, change["path_digest"]))
    assert head.owns_item
    assert dict(object_store) == stored
    assert set(await db.scalars(originals)) == original_ids
    await db.refresh(item)
    assert item.file_path == original_path
    assert (
        await db.scalar(
            select(DatasetAsset.id).where(DatasetAsset.dataset_item_id == item.id)
        )
        == asset_id
    )
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


@pytest.mark.parametrize("revalidate,color", [(False, "red"), (True, "blue")])
async def test_endpoints_cannot_expand_change_kind(
    db_session, content_world, revalidate, color
):
    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, change, payload = await candidate(db, world, color)
    with pytest.raises(HTTPException) as exc:
        await revisions.accept(
            db, run.id, change["path_digest"], payload, world[2], revalidate=revalidate
        )
    assert exc.value.detail["code"] == "revision_selection_changed"


@pytest.mark.parametrize("barrier", [1, 2])
async def test_file_changed_during_revalidation_does_not_renew(
    db_session, content_world, monkeypatch, barrier
):
    from app.services import content_snapshot as snapshots
    from tests.test_dataset_source_changes import picture

    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, change, payload = await candidate(db, world, "red")
    original = snapshots.capture
    calls = 0

    async def capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == barrier:
            (world[0] / "图片 空格.png").write_bytes(picture("green"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "capture", capture)
    with pytest.raises((HTTPException, ContentError, FilesystemStorageError)):
        await revisions.accept(
            db, run.id, change["path_digest"], payload, world[2], revalidate=True
        )
    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(DatasetSourceRevision)) == 1


async def test_external_same_sha_receipts_keep_original_identity(db_session, s3_world):
    from app.db.models.dataset_source import DatasetScanEntry
    from app.schemas.dataset_source import SourceRevisionAccept
    from app.services.dataset_references import reference_snapshot
    from app.services import dataset_registration as registration
    from tests.test_s3_references import scan
    from tests.test_dataset_registration import drain

    db = db_session
    memory, actor, _, _, source = s3_world
    memory.version = "v1"
    old_run = await scan(db, s3_world)
    entry = await db.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == old_run.id, DatasetScanEntry.kind == "file"
        )
    )
    old = await register_entry(db, old_run.id, entry.path_digest, actor)
    memory.version = "v2"
    new_run = await scan(db, s3_world)
    run, _ = await registration.start_preflight(db, new_run.id, actor)
    from app.db.models.async_job import AsyncJob

    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await drain(db, run, job.celery_task_id, fence)
    c = (await changes.page(db, run, "changed", 20, 0))["items"][0]
    assert c["can_revalidate"] and c["reason"] == "observation_changed"
    result = await revisions.accept(
        db,
        run.id,
        c["path_digest"],
        SourceRevisionAccept(
            expected_generation=run.generation,
            before_revision_id=c["before_revision_id"],
            candidate_sha256=c["candidate_sha256"],
        ),
        actor,
        revalidate=True,
    )
    assert result["item_id"] == old["item_id"]
    memory.calls.clear()
    snapshot = await reference_snapshot(db, old["item_id"], actor)
    snapshot.close()
    gets = [
        args
        for op, args in memory.calls
        if op == "get" and args.get("IfMatch") != '"aap-intentionally-invalid-etag"'
    ]
    assert gets and all(args.get("VersionId") == "v2" for args in gets)
    history = await revisions.history(db, source.id, c["path_digest"], 20, 0)
    assert history["items"][0]["acceptance_kind"] == "observation_revalidation"


async def test_sftp_same_bytes_new_mtime_can_be_explicitly_revalidated(
    db_session, sftp_world
):
    import os
    from app.db.models.async_job import AsyncJob
    from app.schemas.dataset_source import SourceRevisionAccept
    from app.services import dataset_registration as registration
    from app.services.dataset_references import reference_snapshot
    from tests.test_sftp_references import scan, first_entry
    from tests.test_dataset_registration import drain

    db = db_session
    root, _, actor, _, _, _ = sftp_world
    old_run = await scan(db, sftp_world)
    entry = await first_entry(db, old_run)
    old = await register_entry(db, old_run.id, entry.path_digest, actor)
    path = root / "collection" / entry.relative_path
    info = path.stat()
    os.utime(path, (info.st_atime, info.st_mtime + 5))
    with pytest.raises((ContentError, FilesystemStorageError)):
        await reference_snapshot(db, old["item_id"], actor)
    new_run = await scan(db, sftp_world)
    run, _ = await registration.start_preflight(db, new_run.id, actor)
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await drain(db, run, job.celery_task_id, fence)
    c = (await changes.page(db, run, "changed", 20, 0))["items"][0]
    result = await revisions.accept(
        db,
        run.id,
        c["path_digest"],
        SourceRevisionAccept(
            expected_generation=run.generation,
            before_revision_id=c["before_revision_id"],
            candidate_sha256=c["candidate_sha256"],
        ),
        actor,
        revalidate=True,
    )
    assert result["item_id"] == old["item_id"]
    snapshot = await reference_snapshot(db, old["item_id"], actor)
    assert snapshot.file.read() == path.read_bytes()
    snapshot.close()
