"""Accepted bytes create new business identities without inheriting labels."""

import copy

from tests.test_directory_policies import object_store as object_store
from tests.test_s3_references import s3_world as s3_world

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import (
    DatasetSourceItem,
    DatasetSourceRevision,
    DatasetReference,
)
from app.db.models.task import Task
from app.schemas.dataset_source import SourceRevisionAccept
from app.services import dataset_source_revisions as revisions
from app.services import dataset_scans as scans
from app.services import dataset_source_changes as changes
from app.services import directory_policy, dataset_source_lifecycle
from app.services.dataset_references import register_entry
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_dataset_source_changes import picture, rescan, preflight


async def candidate(db, world, color="blue"):
    (world[0] / "图片 空格.png").write_bytes(picture(color))
    world = await rescan(db, world)
    run = await preflight(db, world)
    page = await changes.page(db, run, "changed", 20, 0)
    change = page["items"][0]
    return (
        world,
        run,
        change,
        SourceRevisionAccept(
            expected_generation=run.generation,
            before_revision_id=change["before_revision_id"],
            candidate_sha256=change["candidate_sha256"],
        ),
    )


async def test_new_revision_keeps_old_labels_tasks_and_history(
    db_session, reference_world, httpx_client, super_admin, annotator
):
    db = db_session
    old, project, task, world = reference_world
    annotation = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=headers(annotator),
        json={
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert annotation.status_code == 201, annotation.text
    old_task = (task.id, task.file_path, task.assignee_id, task.status)
    old_metadata = copy.deepcopy((await db.get(DatasetItem, old["item_id"])).metadata_)
    old_ref = await db.get(DatasetReference, old["item_id"])
    old_run = old_ref.run_id
    world, run, change, payload = await candidate(db, world)
    url = f"/api/v1/dataset-registrations/{run.id}/changes/{change['path_digest']}/accept-revision"
    response = await httpx_client.post(
        url, json=payload.model_dump(mode="json"), headers=headers(super_admin)
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert (
        result["created"]
        and result["revision_number"] == 2
        and result["item_id"] != str(old["item_id"])
    )
    replay = await httpx_client.post(
        url, json=payload.model_dump(mode="json"), headers=headers(super_admin)
    )
    assert (
        replay.status_code == 200
        and not replay.json()["created"]
        and replay.json()["item_id"] == result["item_id"]
    )
    await db.refresh(task)
    assert old_task == (task.id, task.file_path, task.assignee_id, task.status)
    await db.refresh(old_ref)
    assert old_ref.run_id == old_run
    assert (await db.get(DatasetItem, old["item_id"])).metadata_ == old_metadata
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id == task.id)
        )
        == 1
    )
    tasks = list(await db.scalars(select(Task).where(Task.project_id == project.id)))
    assert len(tasks) == 2
    new_task = next(t for t in tasks if t.id != task.id)
    assert (
        new_task.dataset_item_id != task.dataset_item_id
        and new_task.assignee_id is None
        and new_task.status == "pending"
    )
    assert not await db.scalar(
        select(Annotation.id).where(Annotation.task_id == new_task.id)
    )
    assert await directory_policy.owns_media(db, old["item_id"])
    impact = await dataset_source_lifecycle.impact(db, world[5], world[2])
    assert (
        impact["item_count"] == impact["task_count"] == 2
        and impact["annotation_count"] == 1
    )
    page = await changes.page(db, run, "changed", 20, 0)
    assert page["counts"]["changed"] == 1
    assert (
        page["items"][0]["accepted_item_id"] == new_task.dataset_item_id
        and not page["items"][0]["can_accept_revision"]
    )
    listed = await httpx_client.get(
        f"/api/v1/dataset-scans/{old_run}/entries", headers=headers(super_admin)
    )
    assert listed.json()["items"][0]["registered_item_id"] == str(old["item_id"])
    old_read = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/content",
        params={"sha256": old["sha256"]},
        headers=headers(annotator),
    )
    assert old_read.status_code == 409
    new_read = await httpx_client.get(
        f"/api/v1/files/tasks/{new_task.id}/content",
        params={"sha256": result["sha256"]},
        headers=headers(super_admin),
    )
    assert new_read.status_code == 200 and new_read.content == picture("blue")
    history = await httpx_client.get(
        f"/api/v1/dataset-sources/{world[5].id}/revisions", headers=headers(super_admin)
    )
    assert history.status_code == 200 and history.json()["total"] == 2
    world = await rescan(db, world)
    next_run = await preflight(db, world)
    assert (await changes.page(db, next_run, None, 20, 0))["counts"]["unchanged"] == 1


async def test_explicit_selection_stale_generation_and_file_race(
    db_session, content_world
):
    db, world = db_session, content_world
    old = await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, change, payload = await candidate(db, world)
    actor_id, source_id, run_id = world[2].id, world[5].id, run.id
    wrong = payload.model_copy(
        update={"expected_generation": payload.expected_generation + 1}
    )
    with pytest.raises(HTTPException) as exc:
        await revisions.accept(db, run.id, change["path_digest"], wrong, world[2])
    assert exc.value.detail["code"] == "revision_preflight_required"
    await db.rollback()
    from app.db.models.user import User

    actor = await db.get(User, actor_id)
    (world[0] / "图片 空格.png").write_bytes(picture("green"))
    from app.services.content_snapshot import ContentError
    from app.services.filesystem_storage import FilesystemStorageError

    with pytest.raises((ContentError, FilesystemStorageError)):
        await revisions.accept(db, run_id, change["path_digest"], payload, actor)
    await db.rollback()
    head = await db.get(DatasetSourceItem, (source_id, change["path_digest"]))
    assert head.dataset_item_id == old["item_id"]
    assert await db.scalar(select(func.count()).select_from(DatasetSourceRevision)) == 1
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


async def test_older_comparison_cannot_advance_changed_predecessor(
    db_session, content_world
):
    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run1, change1, payload1 = await candidate(db, world)
    world = await rescan(db, world)
    run2 = await preflight(db, world)
    c2 = (await changes.page(db, run2, "changed", 20, 0))["items"][0]
    payload2 = SourceRevisionAccept(
        expected_generation=run2.generation,
        before_revision_id=c2["before_revision_id"],
        candidate_sha256=c2["candidate_sha256"],
    )
    result = await revisions.accept(db, run2.id, c2["path_digest"], payload2, world[2])
    source_id = world[5].id
    with pytest.raises(HTTPException) as exc:
        await revisions.accept(db, run1.id, change1["path_digest"], payload1, world[2])
    assert exc.value.detail["code"] == "source_revision_changed"
    await db.rollback()
    assert (
        await db.get(DatasetSourceItem, (source_id, c2["path_digest"]))
    ).dataset_item_id == result["item_id"]


async def test_copy_revision_bypasses_business_sha_dedup_and_keeps_old_media(
    db_session, content_world, object_store
):
    from tests.test_dataset_source_changes import accept_all_files
    from app.services.dataset_content import stored_content
    from app.services.content_snapshot import capture
    from app.services.storage import storage_service
    from app.db.models.dataset_asset import DatasetAsset
    from sqlalchemy.exc import IntegrityError

    db, world = db_session, content_world
    world[5].ownership_mode = "managed_copy"
    await db.commit()
    (world[0] / "other.png").write_bytes(picture("blue"))
    world = await rescan(db, world)
    old = await accept_all_files(db, world)
    old_item = await db.get(DatasetItem, old["图片 空格.png"]["item_id"])
    old_path = old_item.file_path
    old_asset = await db.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == old_item.id)
    )
    world, run, change, payload = await candidate(db, world)
    result = await revisions.accept(
        db, run.id, change["path_digest"], payload, world[2]
    )
    assert result["item_id"] not in {r["item_id"] for r in old.values()}
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 3
    snapshot = await capture(
        lambda: stored_content(
            storage_service.datasets_bucket, old_path, len(world[1])
        ),
        old["图片 空格.png"]["sha256"],
    )
    assert snapshot.file.read() == world[1]
    snapshot.close()
    assert await directory_policy.owns_media(db, old_item.id)
    async with db.begin_nested():
        with pytest.raises(IntegrityError, match="identity is immutable"):
            async with db.begin_nested():
                old_asset.checksum_value = "f" * 64
                await db.flush()


async def test_aba_and_deleted_historical_item_never_rebind_old_scan(
    db_session, content_world, httpx_client, super_admin
):
    from app.db.models.dataset_asset import DatasetAsset

    db, world = db_session, content_world
    initial_scan_id = world[6].id
    original = await register_entry(db, world[6].id, world[7].path_digest, world[2])
    old_asset_id = await db.scalar(
        select(DatasetAsset.id).where(
            DatasetAsset.dataset_item_id == original["item_id"]
        )
    )
    world, run, change, payload = await candidate(db, world)
    await revisions.accept(db, run.id, change["path_digest"], payload, world[2])
    world, run, change, payload = await candidate(db, world, "red")
    latest = await revisions.accept(
        db, run.id, change["path_digest"], payload, world[2]
    )
    assert (
        latest["sha256"] == original["sha256"]
        and latest["item_id"] != original["item_id"]
    )
    await db.delete(await db.get(DatasetItem, original["item_id"]))
    await db.commit()
    old_revision = await db.scalar(
        select(DatasetSourceRevision)
        .where(DatasetSourceRevision.item_id_at_accept == original["item_id"])
        .execution_options(populate_existing=True)
    )
    assert old_revision.dataset_item_id is None and old_revision.asset_id is None
    assert old_revision.asset_id_at_accept == old_asset_id
    listed = await httpx_client.get(
        f"/api/v1/dataset-scans/{initial_scan_id}/entries", headers=headers(super_admin)
    )
    assert (
        listed.status_code == 200
        and listed.json()["items"][0]["registered_item_id"] is None
    )


@pytest.mark.parametrize("revalidate", [False, True])
@pytest.mark.parametrize("barrier", [1, 2])
@pytest.mark.parametrize("revocation", ["actor", "source", "policy"])
async def test_io_revocation_never_publishes_new_business_revision(
    db_session, content_world, monkeypatch, barrier, revocation, revalidate
):
    from app.services import content_snapshot as snapshots
    from app.services.filesystem_storage import FilesystemStorageError
    from sqlalchemy import text

    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, change, payload = await candidate(
        db, world, "red" if revalidate else "blue"
    )
    run_id, actor, source_id = run.id, world[2], world[5].id
    original = snapshots.capture
    calls = 0

    async def capture(*args, **kwargs):
        nonlocal calls
        snapshot = await original(*args, **kwargs)
        calls += 1
        if calls == barrier:
            if revocation == "actor":
                await db.execute(
                    text("UPDATE users SET is_active=false WHERE id=:id"),
                    {"id": actor.id},
                )
            elif revocation == "source":
                await db.execute(
                    text("UPDATE dataset_sources SET enabled=false WHERE id=:id"),
                    {"id": source_id},
                )
            else:
                await db.execute(
                    text(
                        "UPDATE dataset_sources SET policy_revision=policy_revision+1 WHERE id=:id"
                    ),
                    {"id": source_id},
                )
        return snapshot

    monkeypatch.setattr(snapshots, "capture", capture)
    with pytest.raises((HTTPException, FilesystemStorageError)):
        await revisions.accept(
            db, run_id, change["path_digest"], payload, actor, revalidate=revalidate
        )
    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1
    assert await db.scalar(select(func.count()).select_from(DatasetSourceRevision)) == 1


async def test_s3_new_revision_preserves_pinned_old_version(
    db_session, s3_world, monkeypatch
):
    import io
    from app.db.models.dataset_source import DatasetScanEntry
    from app.db.models.async_job import AsyncJob
    from app.services import dataset_registration as registration
    from app.services.dataset_references import reference_snapshot
    from tests.test_s3_references import scan
    from tests.test_dataset_registration import drain

    db = db_session
    memory, actor, _, _, source = s3_world
    versions = {"v1": memory.data, "v2": picture("blue")}
    memory.version = "v1"
    original_head = memory.head_object

    def head(**kwargs):
        response = original_head(**kwargs)
        version = kwargs.get("VersionId", memory.version)
        return {
            **response,
            "VersionId": version,
            "ContentLength": len(versions[version]),
        }

    def get(**kwargs):
        memory.calls.append(("get", kwargs))
        return {
            **head(**kwargs),
            "Body": io.BytesIO(versions[kwargs.get("VersionId", memory.version)]),
        }

    monkeypatch.setattr(memory, "head_object", head)
    monkeypatch.setattr(memory, "get_object", get)
    old_scan = await scan(db, s3_world)
    entry = await db.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == old_scan.id, DatasetScanEntry.kind == "file"
        )
    )
    old = await register_entry(db, old_scan.id, entry.path_digest, actor)
    memory.version = "v2"
    memory.objects = {key: versions["v2"] for key in memory.objects}
    new_scan = await scan(db, s3_world)
    run, _ = await registration.start_preflight(db, new_scan.id, actor)
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    change = (await changes.page(db, run, "changed", 20, 0))["items"][0]
    payload = SourceRevisionAccept(
        expected_generation=run.generation,
        before_revision_id=change["before_revision_id"],
        candidate_sha256=change["candidate_sha256"],
    )
    result = await revisions.accept(db, run.id, change["path_digest"], payload, actor)
    for item, data in ((old, versions["v1"]), (result, versions["v2"])):
        snapshot = await reference_snapshot(db, item["item_id"], actor)
        assert snapshot.file.read() == data
        snapshot.close()
    assert result["item_id"] != old["item_id"]
    replay = await register_entry(db, old_scan.id, entry.path_digest, actor)
    assert replay["item_id"] == old["item_id"] and not replay["created"]
    head = await db.get(DatasetSourceItem, (source.id, entry.path_digest))
    assert head.dataset_item_id == result["item_id"]


@pytest.mark.parametrize("revalidate", [False, True])
@pytest.mark.parametrize("barrier", [1, 2])
@pytest.mark.parametrize("change_key", ["rotate", "revoke"])
async def test_original_key_proof_survives_nested_snapshot_helpers(
    db_session,
    content_world,
    httpx_client,
    super_admin,
    monkeypatch,
    barrier,
    change_key,
    revalidate,
):
    import uuid
    from datetime import datetime, timezone
    from app.db.models.api_key import ApiKey
    from app.services import content_snapshot as snapshots

    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, change, payload = await candidate(
        db, world, "red" if revalidate else "blue"
    )
    run_id = run.id
    response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "revision-test", "scopes": ["datasets:write"]},
    )
    assert response.status_code == 201
    key = await db.get(ApiKey, uuid.UUID(response.json()["id"]))
    original = snapshots.capture
    captured = []

    async def capture(*args, **kwargs):
        result = await original(*args, **kwargs)
        captured.append(result)
        if len(captured) == barrier:
            if change_key == "rotate":
                key.key_hash = "rotated-test-key"
            else:
                key.revoked_at = datetime.now(timezone.utc)
            await db.flush()
        return result

    monkeypatch.setattr(snapshots, "capture", capture)
    response = await httpx_client.post(
        f"/api/v1/dataset-registrations/{run_id}/changes/{change['path_digest']}/{'revalidate' if revalidate else 'accept-revision'}",
        json=payload.model_dump(mode="json"),
        headers={"Authorization": "Bearer " + response.json()["plaintext"]},
    )
    assert response.status_code == 403, response.text
    assert captured and all(s.file.closed for s in captured)
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1


async def test_two_changed_paths_accept_only_one_and_add_blank_tasks_in_two_projects(
    db_session, content_world
):
    from tests.factory import create_project
    from tests.test_dataset_source_changes import accept_all_files
    from app.services.dataset import DatasetService
    from app.services.dataset_source_access import capture_dataset_write

    db, world = db_session, content_world
    (world[0] / "second.png").write_bytes(picture("green"))
    world = await rescan(db, world)
    old = await accept_all_files(db, world)
    projects = [await create_project(db, owner_id=world[2].id) for _ in range(2)]
    for project in projects:
        authority = await capture_dataset_write(db, world[3].id, world[2], None)
        await DatasetService(db).link_project(
            world[3].id, project.id, directory_authority=authority
        )
    (world[0] / "second.png").write_bytes(picture("yellow"))
    world, run, change, payload = await candidate(db, world)
    assert (await changes.page(db, run, "changed", 20, 0))["total"] == 2
    result = await revisions.accept(
        db, run.id, change["path_digest"], payload, world[2]
    )
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 3
    new_tasks = list(
        await db.scalars(select(Task).where(Task.dataset_item_id == result["item_id"]))
    )
    assert {t.project_id for t in new_tasks} == {p.id for p in projects}
    assert all(t.status == "pending" and t.assignee_id is None for t in new_tasks)
    page = await changes.page(db, run, "changed", 20, 0)
    assert sum(row["can_accept_revision"] for row in page["items"]) == 1
    assert sum(row["accepted_revision_id"] is not None for row in page["items"]) == 1
    for path, original in old.items():
        if scans.path_digest(path) != change["path_digest"]:
            head = await db.get(
                DatasetSourceItem, (world[5].id, scans.path_digest(path))
            )
            assert head.dataset_item_id == original["item_id"]
