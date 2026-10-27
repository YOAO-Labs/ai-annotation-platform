"""Explicit same-source relocations retain business identity and consume targets."""

import copy

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetSourceItem,
)
from app.db.models.task import Task
from app.schemas.dataset_source import SourceRevisionAccept
from app.services import (
    dataset_source_changes as changes,
    dataset_source_revisions as revisions,
)
from app.services.dataset_references import register_entry, reference_snapshot
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_directory_policies import object_store as object_store
from tests.test_s3_references import s3_world as s3_world
from tests.test_dataset_source_changes import rescan, preflight
from tests.test_dataset_source_revisions import candidate


async def moved_candidate(
    db, world, before_path="图片 空格.png", after_path="moved/vehicle.png"
):
    target = world[0] / after_path
    target.parent.mkdir(parents=True, exist_ok=True)
    (world[0] / before_path).rename(target)
    world = await rescan(db, world)
    run = await preflight(db, world)
    old = next(
        c
        for c in (await changes.page(db, run, "not_seen", 20, 0))["items"]
        if c["relative_path"] == before_path
    )
    choices = await revisions.move_candidates(db, run, old["path_digest"], 20, 0)
    new = next(c for c in choices["items"] if c["relative_path"] == after_path)
    return (
        world,
        run,
        old,
        new,
        SourceRevisionAccept(
            expected_generation=run.generation,
            before_revision_id=old["before_revision_id"],
            candidate_sha256=new["candidate_sha256"],
        ),
    )


async def test_relocation_restores_same_task_and_labels_across_path_local_numbers(
    db_session, reference_world, httpx_client, super_admin, annotator
):
    db = db_session
    original, project, task, world = reference_world
    item_id, task_id, original_run_id = original["item_id"], task.id, world[6].id
    meta = copy.deepcopy((await db.get(DatasetItem, item_id)).metadata_)
    r = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=headers(annotator),
        json={
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert r.status_code == 201
    annotation_id = r.json()["id"]
    # A high path-local number must not override a later B revision numbered 1.
    for _ in range(2):
        world, run, change, payload = await candidate(db, world, "red")
        await revisions.accept(
            db, run.id, change["path_digest"], payload, world[2], revalidate=True
        )
    source_id = world[5].id
    content_url = f"/api/v1/files/tasks/{task_id}/content?sha256={original['sha256']}"
    for before, after in [
        ("图片 空格.png", "moved/vehicle.png"),
        ("moved/vehicle.png", "图片 空格.png"),
    ]:
        world, run, old, new, payload = await moved_candidate(db, world, before, after)
        assert (
            await httpx_client.get(content_url, headers=headers(annotator))
        ).status_code == 409
        url = f"/api/v1/dataset-registrations/{run.id}/changes/{new['path_digest']}/relocate"
        r = await httpx_client.post(
            url, headers=headers(super_admin), json=payload.model_dump(mode="json")
        )
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["item_id"] == str(item_id) and not result["created"]
        replay = await httpx_client.post(
            url, headers=headers(super_admin), json=payload.model_dump(mode="json")
        )
        assert replay.status_code == 200 and replay.json() == result
        assert await db.get(DatasetSourceItem, (source_id, old["path_digest"])) is None
        head = await db.get(DatasetSourceItem, (source_id, new["path_digest"]))
        assert head.dataset_item_id == item_id
        read = await httpx_client.get(content_url, headers=headers(annotator))
        assert read.status_code == 200 and read.content == world[1]
        page = await changes.page(db, run, None, 20, 0)
        assert all(c["relocated_to_path"] == after for c in page["items"])
        assert page["counts"]["added"] == 1 and page["remaining_added_count"] == 0
        assert run.counts["ready_count"] == 0 and run.counts["registered_count"] == 1
        again = await register_entry(db, world[6].id, new["path_digest"], world[2])
        assert again["item_id"] == item_id and not again["created"]
    assert (await db.get(DatasetReference, item_id)).run_id == original_run_id
    assert (await db.get(DatasetItem, item_id)).metadata_ == meta
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1
    assert await db.scalar(select(func.count()).select_from(Task)) == 1
    labels = await httpx_client.get(
        f"/api/v1/tasks/{task_id}/annotations", headers=headers(annotator)
    )
    assert labels.json()[0]["id"] == annotation_id
    history = await revisions.history(db, source_id, None, 20, 0)
    assert sum(r["acceptance_kind"] == "relocation" for r in history["items"]) == 2


async def test_multiple_same_sha_choices_require_explicit_choice_and_returning_path_is_new(
    db_session, content_world
):
    db, world = db_session, content_world
    old = await register_entry(db, world[6].id, world[7].path_digest, world[2])
    (world[0] / "second.png").write_bytes(world[1])
    world, run, before, target, payload = await moved_candidate(db, world)
    assert (await revisions.move_candidates(db, run, before["path_digest"], 1, 0))[
        "total"
    ] == 2
    result = await revisions.accept(
        db, run.id, target["path_digest"], payload, world[2], relocate=True
    )
    assert result["item_id"] == old["item_id"]
    assert (await revisions.move_candidates(db, run, before["path_digest"], 20, 0))[
        "total"
    ] == 0
    # A returning former path is a new business sample; it cannot steal the moved locator.
    (world[0] / "图片 空格.png").write_bytes(world[1])
    world = await rescan(db, world)
    run = await preflight(db, world)
    c = next(
        c
        for c in (await changes.page(db, run, "added", 20, 0))["items"]
        if c["relative_path"] == "图片 空格.png"
    )
    returned = await register_entry(db, world[6].id, c["path_digest"], world[2])
    assert returned["created"] and returned["item_id"] != old["item_id"]
    snapshot = await reference_snapshot(db, old["item_id"], world[2])
    assert snapshot.file.read() == world[1]
    snapshot.close()
    (world[0] / "moved/vehicle.png").unlink()
    from app.services.content_snapshot import ContentError
    from app.services.filesystem_storage import FilesystemStorageError

    with pytest.raises((ContentError, FilesystemStorageError)):
        await reference_snapshot(db, old["item_id"], world[2])


async def test_managed_relocation_moves_only_preview_and_retains_original_ownership(
    db_session, content_world, object_store
):
    from app.db.models.dataset_source import DatasetSourcePreview
    from app.db.models.thumbnail_output import ThumbnailOutput
    from app.services.storage import storage_service

    db, world = db_session, content_world
    world[5].ownership_mode, world[5].cache_policy = "managed_copy", "preview"
    await db.commit()
    old = await register_entry(db, world[6].id, world[7].path_digest, world[2])
    original_keys = {k for k in object_store if k[0] == storage_service.datasets_bucket}
    previous_preview = await db.get(
        DatasetSourcePreview, (world[5].id, world[7].path_digest)
    )
    previous_output_id = previous_preview.thumbnail_output_id
    world, run, before, target, payload = await moved_candidate(db, world)
    result = await revisions.accept(
        db, run.id, target["path_digest"], payload, world[2], relocate=True
    )
    assert result["item_id"] == old["item_id"]
    assert original_keys == {
        k for k in object_store if k[0] == storage_service.datasets_bucket
    }
    head = await db.get(DatasetSourceItem, (world[5].id, target["path_digest"]))
    assert head.owns_item
    preview = await db.get(DatasetSourcePreview, (world[5].id, target["path_digest"]))
    assert preview.state == "ready" and preview.asset_id == head.asset_id
    assert preview.thumbnail_output_id != previous_output_id
    previous_output = await db.get(
        ThumbnailOutput, previous_output_id, populate_existing=True
    )
    assert previous_output.state == "retiring"
    assert previous_output.retired_at is not None


async def test_old_preflight_cannot_move_an_advanced_head(db_session, content_world):
    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, old_run, before, target, payload = await moved_candidate(db, world)
    world = await rescan(db, world)
    newer = await preflight(db, world)
    selected = SourceRevisionAccept(
        expected_generation=newer.generation,
        before_revision_id=payload.before_revision_id,
        candidate_sha256=payload.candidate_sha256,
    )
    await revisions.accept(
        db, newer.id, target["path_digest"], selected, world[2], relocate=True
    )
    with pytest.raises(HTTPException) as exc:
        await revisions.accept(
            db, old_run.id, target["path_digest"], payload, world[2], relocate=True
        )
    assert exc.value.detail["code"] == "source_revision_changed"


@pytest.mark.parametrize("barrier", [1, 2])
@pytest.mark.parametrize("revocation", ["actor", "policy", "candidate"])
async def test_relocation_io_fences(
    db_session, content_world, monkeypatch, barrier, revocation
):
    from app.services import content_snapshot as snapshots
    from app.services.filesystem_storage import FilesystemStorageError
    from sqlalchemy import text
    from tests.test_dataset_source_changes import picture

    db, world = db_session, content_world
    original_item = await register_entry(
        db, world[6].id, world[7].path_digest, world[2]
    )
    world, run, before, target, payload = await moved_candidate(db, world)
    source_id, actor_id = world[5].id, world[2].id
    original = snapshots.capture
    calls = 0

    async def capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        if revocation == "candidate" and calls == barrier:
            (world[0] / target["relative_path"]).write_bytes(picture("green"))
        result = await original(*args, **kwargs)
        if calls == barrier:
            if revocation == "actor":
                await db.execute(
                    text("UPDATE users SET is_active=false WHERE id=:id"),
                    {"id": actor_id},
                )
            elif revocation == "policy":
                await db.execute(
                    text(
                        "UPDATE dataset_sources SET policy_revision=policy_revision+1 WHERE id=:id"
                    ),
                    {"id": source_id},
                )
        return result

    monkeypatch.setattr(snapshots, "capture", capture)
    with pytest.raises((HTTPException, snapshots.ContentError, FilesystemStorageError)):
        await revisions.accept(
            db, run.id, target["path_digest"], payload, world[2], relocate=True
        )
    await db.rollback()
    head = await db.get(DatasetSourceItem, (source_id, before["path_digest"]))
    assert head.dataset_item_id == original_item["item_id"]
    assert await db.get(DatasetSourceItem, (source_id, target["path_digest"])) is None


async def test_external_s3_relocation_keeps_exact_key_and_business_asset(
    db_session, s3_world
):
    from app.db.models.async_job import AsyncJob
    from app.db.models.dataset_source import DatasetScanEntry
    from app.services import dataset_registration as registration
    from tests.test_s3_references import scan
    from tests.test_dataset_registration import drain

    db = db_session
    memory, actor, _, _, source = s3_world
    run = await scan(db, s3_world)
    entry = await db.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    original = await register_entry(db, run.id, entry.path_digest, actor)
    old_key = next(iter(memory.objects))
    new_key = "root/collection/moved/a//图 +%2F.png"
    memory.objects[new_key] = memory.objects.pop(old_key)
    scan_run = await scan(db, s3_world)
    run, _ = await registration.start_preflight(db, scan_run.id, actor)
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await drain(db, run, job.celery_task_id, fence)
    page = await changes.page(db, run, None, 20, 0)
    old = next(c for c in page["items"] if c["kind"] == "not_seen")
    target = next(c for c in page["items"] if c["kind"] == "added")
    payload = SourceRevisionAccept(
        expected_generation=run.generation,
        before_revision_id=old["before_revision_id"],
        candidate_sha256=target["candidate_sha256"],
    )
    result = await revisions.accept(
        db, run.id, target["path_digest"], payload, actor, relocate=True
    )
    assert result["item_id"] == original["item_id"]
    memory.calls.clear()
    snapshot = await reference_snapshot(db, original["item_id"], actor)
    assert snapshot.file.read() == memory.data
    snapshot.close()
    assert all(args["Key"] == new_key for op, args in memory.calls if op == "get")
