import hashlib
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError

from tests.test_dataset_content import content_world as content_world, headers
from tests.factory import build_tool_bindings
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAssetLocation
from app.db.models.dataset_source import DatasetReference
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.services.dataset import DatasetService
from app.services.dataset_references import register_entry
from app.services.dataset_assets import register_item_asset, find_verified_content
from app.services.reference_access import reject_reference_scope
from app.services.reference_keys import is_reference
from app.services.storage import storage_service


@pytest.fixture
async def reference_world(db_session, content_world, annotator):
    return await create_reference_world(db_session, content_world, annotator[0])


async def create_reference_world(db_session, content_world, annotator):
    """Build the same real reference flow with either transaction ownership model."""
    root, data, actor, dataset, location, source, run, entry = content_world
    registered = await register_entry(db_session, run.id, entry.path_digest, actor)
    project = Project(
        id=uuid.uuid4(),
        display_id=f"p-{uuid.uuid4().hex[:10]}",
        name="Reference",
        owner_id=actor.id,
        type_key="image-det",
        type_label="Image",
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()
    from app.services.dataset_source_access import capture_dataset_write

    authority = await capture_dataset_write(db_session, dataset.id, actor, None)
    await DatasetService(db_session).link_project(
        dataset.id, project.id, directory_authority=authority
    )
    task = await db_session.scalar(select(Task).where(Task.project_id == project.id))
    task.assignee_id = annotator.id
    task.status = "in_progress"
    db_session.add(
        ProjectMember(project_id=project.id, user_id=annotator.id, role="annotator")
    )
    await db_session.flush()
    return registered, project, task, content_world


async def test_registration_is_idempotent_and_never_writes_originals(
    httpx_client, db_session, super_admin, content_world, monkeypatch
):
    root, data, _, dataset, _, source, run, entry = content_world
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

    def no_s3_write(**kwargs):
        pytest.fail("reference registration must not upload originals")

    monkeypatch.setattr(storage_service.client, "put_object", no_s3_write)
    url = f"/api/v1/dataset-scans/{run.id}/entries/{entry.path_digest}/register"
    first = await httpx_client.post(url, headers=headers(super_admin))
    again = await httpx_client.post(url, headers=headers(super_admin))
    assert first.status_code == again.status_code == 200, first.text
    assert first.json()["created"] and not again.json()["created"]
    assert first.json()["item_id"] == again.json()["item_id"]
    assert (
        await db_session.scalar(select(func.count()).select_from(DatasetReference)) == 1
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(DatasetAssetLocation))
        == 0
    )
    listed = await httpx_client.get(
        f"/api/v1/datasets/{dataset.id}/items", headers=headers(super_admin)
    )
    item = listed.json()["items"][0]
    assert item["file_url"] is None and item["metadata"]["storage_mode"] == "reference"
    assert item["original_identity_path"] is None
    assert is_reference(item["file_path"])
    assert before == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert first.json()["sha256"] == hashlib.sha256(data).hexdigest()


async def test_assigned_employee_reads_and_saves_reference_annotation(
    httpx_client, db_session, super_admin, annotator, reviewer, reference_world
):
    registered, _, task, world = reference_world
    root, data, *_ = world
    base = f"/api/v1/files/tasks/{task.id}"
    response = await httpx_client.get(
        base + "/content",
        params={"sha256": registered["sha256"]},
        headers=headers(annotator),
    )
    assert response.status_code == 200 and response.content == data, response.text
    assert (
        await httpx_client.get(base + "/file-url", headers=headers(annotator))
    ).status_code == 409
    assert (
        await httpx_client.get(
            base + "/content",
            params={"sha256": registered["sha256"]},
            headers=headers(reviewer),
        )
    ).status_code in (403, 404)
    dto = await httpx_client.get(f"/api/v1/tasks/{task.id}", headers=headers(annotator))
    assert dto.json()["storage_mode"] == "reference" and dto.json()["file_url"] is None
    assert dto.json()["original_identity_path"] is None
    assert dto.json()["content_sha256"] == registered["sha256"]
    payload = {
        "class_name": "car",
        "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
    }
    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations", json=payload, headers=headers(annotator)
    )
    assert created.status_code == 201, created.text
    ann_id = created.json()["id"]
    assert (
        await httpx_client.post(
            f"/api/v1/tasks/{task.id}/lock", headers=headers(annotator)
        )
    ).status_code == 200
    (root / "图片 空格.png").write_bytes(b"changed")
    for method, url, body in [
        ("POST", f"/api/v1/tasks/{task.id}/annotations", payload),
        ("PATCH", f"/api/v1/tasks/{task.id}/annotations/{ann_id}", {"z_order": 3}),
        (
            "POST",
            "/api/v1/annotations/bulk-update",
            {"ids": [ann_id], "patch": {"z_order": 3}},
        ),
        ("POST", f"/api/v1/tasks/{task.id}/submit", {}),
        ("POST", f"/api/v1/tasks/{task.id}/skip", {"reason": "empty"}),
    ]:
        denied = await httpx_client.request(
            method, url, json=body, headers=headers(annotator)
        )
        assert denied.status_code == 409, (url, denied.text)
    assert (
        await httpx_client.get(
            f"/api/v1/tasks/{task.id}/annotations", headers=headers(annotator)
        )
    ).json()[0]["id"] == ann_id
    assert (
        await httpx_client.delete(
            f"/api/v1/tasks/{task.id}/lock", headers=headers(annotator)
        )
    ).status_code == 204


async def test_reference_binding_and_task_content_are_immutable(
    db_session, reference_world
):
    _, _, task, _ = reference_world
    for model, criterion, values in [
        (
            DatasetReference,
            DatasetReference.dataset_item_id == task.dataset_item_id,
            {"source_revision": 99},
        ),
        (DatasetItem, DatasetItem.id == task.dataset_item_id, {"file_path": "other"}),
        (Task, Task.id == task.id, {"file_path": "other"}),
    ]:
        with pytest.raises(IntegrityError, match="immutable"):
            async with db_session.begin_nested():
                await db_session.execute(
                    update(model).where(criterion).values(**values)
                )


async def test_reference_is_not_legacy_dedup_or_backfill(db_session, reference_world):
    registered, _, task, world = reference_world
    dataset = world[3]
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert (
        await find_verified_content(db_session, dataset.id, registered["sha256"])
        is None
    )
    with pytest.raises(ValueError, match="Reference"):
        await register_item_asset(db_session, item)
    for action in [
        lambda: storage_service.generate_download_url(item.file_path),
        lambda: storage_service.client.head_object(
            Bucket=storage_service.datasets_bucket, Key=item.file_path
        ),
    ]:
        with pytest.raises(ValueError, match="Reference"):
            action()
    with pytest.raises(HTTPException) as exc:
        await reject_reference_scope(db_session, project_id=task.project_id)
    assert exc.value.status_code == 409


async def test_rename_pause_and_restore_preserve_reference_identity(
    httpx_client, db_session, annotator, reference_world
):
    registered, _, task, world = reference_world
    source = world[5]
    url = f"/api/v1/files/tasks/{task.id}/content?sha256={registered['sha256']}"
    source.name = "Renamed"
    source.config_revision += 1
    await db_session.flush()
    assert (await httpx_client.get(url, headers=headers(annotator))).status_code == 200
    source.enabled = False
    source.config_revision += 1
    await db_session.flush()
    assert (await httpx_client.get(url, headers=headers(annotator))).status_code == 409
    source.enabled = True
    source.config_revision += 1
    await db_session.flush()
    assert (await httpx_client.get(url, headers=headers(annotator))).status_code == 200


@pytest.mark.parametrize(
    "suffix",
    [
        "/annotation-conversions:execute",
        "/annotations/propagate-batch",
        "/predictions/accept",
    ],
)
async def test_unsupported_reference_mutations_fail_closed(
    httpx_client, super_admin, reference_world, suffix
):
    _, _, task, _ = reference_world
    result = await httpx_client.post(
        f"/api/v1/tasks/{task.id}{suffix}", json={}, headers=headers(super_admin)
    )
    assert result.status_code in (404, 409, 405), result.text


async def test_registration_rechecks_original_key_after_verification(
    db_session, httpx_client, super_admin, content_world, monkeypatch
):
    from app.db.models.api_key import ApiKey
    from app.services import dataset_content

    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "reference", "scopes": ["datasets:write"]},
    )
    assert created.status_code == 201, created.text
    key = await db_session.get(ApiKey, uuid.UUID(created.json()["id"]))
    original = dataset_content.scan_snapshot

    async def rotate_after_snapshot(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        key.key_hash = "rotated-after-content-check"
        await db_session.flush()
        return snapshot

    monkeypatch.setattr(dataset_content, "scan_snapshot", rotate_after_snapshot)
    run, entry = content_world[6:]
    with pytest.raises(HTTPException) as exc:
        await register_entry(
            db_session, run.id, entry.path_digest, super_admin[0], api_key_id=key.id
        )
    assert exc.value.status_code == 403
    assert (
        await db_session.scalar(select(func.count()).select_from(DatasetReference)) == 0
    )


async def test_write_checks_content_after_task_lock(
    db_session, httpx_client, annotator, reference_world, monkeypatch
):
    _, _, task, world = reference_world
    original_scalar = db_session.scalar
    changed = False
    advisory_acquired = False
    from app.services.task_lock import TaskLockService

    original_scope_lock = TaskLockService._lock_task_scope

    async def tracked_scope_lock(service, task_id):
        nonlocal advisory_acquired
        await original_scope_lock(service, task_id)
        advisory_acquired = True

    monkeypatch.setattr(TaskLockService, "_lock_task_scope", tracked_scope_lock)

    async def lock_after_external_change(statement, *args, **kwargs):
        nonlocal changed
        # Simulate a source overwrite while this write is queued for Task.
        if (
            not changed
            and getattr(statement, "_for_update_arg", None) is not None
            and str(statement).startswith("SELECT tasks.")
        ):
            # Dispatch holds this scope before inserting TaskLock's Task FK.
            # Taking Task first here would form a cycle with that insert.
            assert advisory_acquired
            changed = True
            (world[0] / "图片 空格.png").write_bytes(b"replaced while waiting for lock")
        return await original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", lock_after_external_change)
    result = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=headers(annotator),
        json={
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert changed and result.status_code == 409, result.text


async def test_reference_read_uses_pinned_anchor_after_probe_ttl(
    db_session, httpx_client, annotator, reference_world
):
    from datetime import datetime, timezone, timedelta
    from app.db.models.dataset_asset import StorageLocationProbe
    from app.services.storage_location import api_node_id

    registered, _, task, world = reference_world
    probe = await db_session.get(StorageLocationProbe, (world[4].id, api_node_id()))
    probe.probed_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.flush()
    result = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/content?sha256={registered['sha256']}",
        headers=headers(annotator),
    )
    assert result.status_code == 200 and result.content == world[1]


async def test_reference_snapshot_does_not_commit_callers_work(
    db_session, annotator, reference_world, monkeypatch
):
    from app.services.dataset_references import reference_snapshot

    _, project, task, _ = reference_world
    project.description = "uncommitted mutation"

    async def no_commit():
        pytest.fail("reference snapshot must preserve caller transaction")

    monkeypatch.setattr(db_session, "commit", no_commit)
    snapshot = await reference_snapshot(
        db_session, task.dataset_item_id, annotator[0], task_id=task.id
    )
    snapshot.close()


async def test_deleting_reference_dataset_keeps_originals(
    httpx_client, super_admin, content_world, db_session
):
    root, data, actor, dataset, _, _, run, entry = content_world
    await register_entry(db_session, run.id, entry.path_digest, actor)
    response = await httpx_client.delete(
        f"/api/v1/datasets/{dataset.id}", headers=headers(super_admin)
    )
    assert response.status_code == 204, response.text
    assert (root / "图片 空格.png").read_bytes() == data


async def test_legacy_key_with_sql_wildcard_lookalike_is_not_reference(
    db_session, reference_world
):
    _, project, _, world = reference_world
    item = DatasetItem(
        dataset_id=world[3].id,
        file_name="a.png",
        file_path="12aap3reference45/a.png",
        file_type="image",
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        display_id="t-ref-lookalike",
        project_id=project.id,
        dataset_item_id=item.id,
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
    )
    db_session.add(task)
    await db_session.flush()
    assert not is_reference(item.file_path)
    await reject_reference_scope(db_session, task_ids=[task.id])
    await reject_reference_scope(db_session, item_ids=[item.id])


async def test_registration_rejects_existing_scene_dataset(db_session, content_world):
    from app.db.models.dataset import Scene

    dataset, run, entry = content_world[3], content_world[6], content_world[7]
    db_session.add(
        Scene(display_id="S-ref-test", dataset_id=dataset.id, name="sequence")
    )
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await register_entry(db_session, run.id, entry.path_digest, content_world[2])
    assert exc.value.status_code == 409


async def test_reference_submit_reuses_locked_proof(
    httpx_client, annotator, reference_world
):
    _, _, task, _ = reference_world
    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        headers=headers(annotator),
        json={
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert created.status_code == 201
    submitted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=headers(annotator), json={}
    )
    assert submitted.status_code == 200, submitted.text
