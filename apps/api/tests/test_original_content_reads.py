"""Authenticated original images use saved owners and fresh read authority."""

import hashlib
from datetime import datetime, timezone
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update

from app.db.models.api_key import ApiKey
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAssetLocation, StorageLocation
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import StorageMaintenanceGate as Gate
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_reader as reader
from app.services import managed_original_creation as creations
from app.services.dataset_assets import LEGACY_LOCATION_ID, register_item_asset
from app.services.storage import storage_service
from tests.test_verified_task_content import (
    headers,
    picture,
    stored_world as stored_world,
)


def content_url(task):
    return f"/api/v1/files/tasks/{task.id}/content"


async def content(client, actor, task, sha256):
    return await client.get(
        content_url(task), params={"sha256": sha256}, headers=headers(actor)
    )


@pytest.fixture
async def standalone_original(
    httpx_client, db_session, project_admin, stored_world, monkeypatch
):
    # Keep the real provider, creation writer, durable owner and read admission.
    monkeypatch.setattr(
        "app.workers.media.generate_task_thumbnail.delay", lambda *_: None
    )
    project_id = stored_world[0].project_id
    response = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers=headers(project_admin),
        json={
            "project_id": str(project_id),
            "file_name": "real-original.png",
            "content_type": "image/png",
        },
    )
    assert response.status_code == 200, response.text
    task = await db_session.get(Task, uuid.UUID(response.json()["task_id"]))
    allocation = await creations.for_task(
        db_session, task_id=task.id, object_key=task.file_path
    )
    raw = picture("purple")
    storage_service.client.put_object(
        Bucket=allocation["staging_bucket"],
        Key=allocation["staging_key"],
        Body=raw,
        ContentType="image/png",
    )
    response = await httpx_client.post(
        f"/api/v1/files/upload-complete/{task.id}", headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    owner = await db_session.get(TaskOriginal, task.original_id)
    assert owner.sha256 == hashlib.sha256(raw).hexdigest()
    # Independent immutable evidence and bytes live until the owned test
    # namespace is destroyed; a business rollback must not compensate-delete.
    return task, owner, raw


async def test_historical_dataset_location_is_read_despite_current_bucket_trap(
    httpx_client, db_session, project_admin, stored_world, monkeypatch
):
    task, asset, _, raw, key = stored_world
    replica = await db_session.scalar(
        select(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    assert replica.creation_id is None
    trap = storage_service.import_bucket
    storage_service.client.put_object(Bucket=trap, Key=key, Body=picture("blue"))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage_service, "datasets_bucket", trap)
            patch.setattr(storage_service, "bucket", trap)
            metadata = await httpx_client.get(
                content_url(task) + "-identity", headers=headers(project_admin)
            )
            response = await content(
                httpx_client, project_admin, task, asset.checksum_value
            )
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["owner_kind"] == "dataset_asset"
        assert metadata.json()["asset_id"] == str(asset.id)
        assert metadata.json()["task_original_id"] is None
        assert metadata.json()["original_availability"] == "unchecked"
        assert response.status_code == 200, response.text
        assert response.content == raw
    finally:
        storage_service.client.delete_object(Bucket=trap, Key=key)


@pytest.mark.parametrize(
    "case", ["missing", "unverified", "cache_role", "unknown_location"]
)
async def test_unknown_dataset_provenance_has_no_object_io(
    httpx_client, db_session, project_admin, stored_world, monkeypatch, case
):
    task, asset, _, _, _ = stored_world
    replica = await db_session.scalar(
        select(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    if case == "missing":
        await db_session.delete(replica)
    elif case == "cache_role":
        replica.role = "original_cache"
    elif case == "unknown_location":
        location = StorageLocation(
            id=uuid.uuid4(),
            name="unknown-" + uuid.uuid4().hex,
            kind="s3",
            config={"endpoint": "unknown.invalid:9000", "use_ssl": False},
        )
        db_session.add(location)
        await db_session.flush()
        replica.location_id = location.id
    else:
        old_item = await db_session.get(DatasetItem, task.dataset_item_id)
        item = DatasetItem(
            dataset_id=old_item.dataset_id,
            file_name="unverified.png",
            file_path="unverified/" + uuid.uuid4().hex,
            file_type="image",
        )
        db_session.add(item)
        await db_session.flush()
        await register_item_asset(db_session, item)
        task.dataset_item_id, task.file_path = item.id, item.file_path
    await db_session.flush()
    blocked = AsyncMock(
        side_effect=AssertionError("Unknown provenance cannot reach the object reader")
    )
    monkeypatch.setattr(reader, "capture", blocked)
    for suffix in ("-identity", ""):
        response = await httpx_client.get(
            content_url(task) + suffix,
            params={} if suffix else {"sha256": asset.checksum_value},
            headers=headers(project_admin),
        )
        assert response.status_code == 409, response.text
    blocked.assert_not_awaited()


async def test_multiple_present_managed_locations_are_ambiguous_before_io(
    httpx_client, db_session, project_admin, stored_world, monkeypatch
):
    task, asset, _, _, key = stored_world
    db_session.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket=storage_service.import_bucket,
            object_key=key,
            role="managed_original",
            reference_state="present",
        )
    )
    await db_session.flush()
    blocked = AsyncMock(
        side_effect=AssertionError("Ambiguity cannot select an arbitrary original")
    )
    monkeypatch.setattr(reader, "capture", blocked)
    response = await content(httpx_client, project_admin, task, asset.checksum_value)
    assert response.status_code == 409, response.text
    blocked.assert_not_awaited()


async def test_additional_cache_role_is_not_a_second_managed_original(
    httpx_client, db_session, project_admin, stored_world
):
    task, asset, _, raw, _ = stored_world
    db_session.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket=storage_service.media_cache_bucket,
            object_key="original-cache/unowned-" + uuid.uuid4().hex,
            role="original_cache",
            reference_state="present",
        )
    )
    await db_session.flush()
    response = await content(httpx_client, project_admin, task, asset.checksum_value)
    assert response.status_code == 200, response.text
    assert response.content == raw


async def test_unowned_standalone_task_does_not_adopt_the_current_object(
    httpx_client, db_session, project_admin, stored_world, monkeypatch
):
    task, asset, _, _, _ = stored_world
    task.dataset_item_id = None
    await db_session.flush()
    assert task.original_id is None
    blocked = AsyncMock(
        side_effect=AssertionError("A historical Task is not an original owner")
    )
    monkeypatch.setattr(reader, "capture", blocked)
    response = await content(httpx_client, project_admin, task, asset.checksum_value)
    assert response.status_code == 409, response.text
    blocked.assert_not_awaited()


@pytest.mark.parametrize(
    "relation", ["same", "primary_only", "conflict", "camera_only"]
)
async def test_dataset_primary_relation_is_explicit(
    httpx_client, db_session, project_admin, stored_world, monkeypatch, relation
):
    task, asset, _, raw, _ = stored_world
    item_id = task.dataset_item_id
    if relation == "conflict":
        source_item = await db_session.get(DatasetItem, item_id)
        other = DatasetItem(
            dataset_id=source_item.dataset_id,
            file_name="other.png",
            file_path="other/" + uuid.uuid4().hex,
            file_type="image",
        )
        db_session.add(other)
        await db_session.flush()
        item_id = other.id
    if relation in {"primary_only", "camera_only"}:
        task.dataset_item_id = None
    db_session.add(
        TaskDatasetItemLink(
            task_id=task.id,
            dataset_item_id=item_id,
            role="camera_front" if relation == "camera_only" else "primary_lidar",
        )
    )
    await db_session.flush()
    if relation in {"conflict", "camera_only"}:
        blocked = AsyncMock(
            side_effect=AssertionError("Ambiguous relation cannot reach storage")
        )
        monkeypatch.setattr(reader, "capture", blocked)
    response = await content(httpx_client, project_admin, task, asset.checksum_value)
    if relation in {"same", "primary_only"}:
        assert response.status_code == 200, response.text
        assert response.content == raw
    else:
        assert response.status_code == 409, response.text
        blocked.assert_not_awaited()


async def test_task_original_identity_and_fixed_content_are_independent(
    httpx_client, project_admin, standalone_original, monkeypatch
):
    task, owner, raw = standalone_original
    trap = storage_service.import_bucket
    storage_service.client.put_object(
        Bucket=trap, Key=owner.object_key, Body=picture("blue")
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage_service, "bucket", trap)
            patch.setattr(storage_service, "datasets_bucket", trap)
            response = await httpx_client.get(
                content_url(task) + "-identity", headers=headers(project_admin)
            )
            assert response.status_code == 200, response.text
            identity = response.json()
            assert identity["owner_kind"] == "task_original"
            assert identity["asset_id"] is None
            assert identity["task_original_id"] == str(owner.id)
            assert identity["sha256"] == owner.sha256
            assert identity["original_availability"] == "unchecked"
            response = await content(httpx_client, project_admin, task, owner.sha256)
        assert response.status_code == 200, response.text
        assert response.content == raw
    finally:
        storage_service.client.delete_object(Bucket=trap, Key=owner.object_key)


async def test_task_original_corruption_never_changes_saved_receipt(
    httpx_client, db_session, project_admin, standalone_original
):
    task, owner, raw = standalone_original
    storage_service.client.put_object(
        Bucket=owner.bucket, Key=owner.object_key, Body=picture("blue")
    )
    try:
        response = await content(httpx_client, project_admin, task, owner.sha256)
        assert response.status_code == 409, response.text
        await db_session.refresh(owner)
        assert owner.sha256 == hashlib.sha256(raw).hexdigest()
    finally:
        # Restore the exact owned fixture bytes without changing its immutable
        # receipt. The candidate remains until full test-namespace destruction.
        storage_service.client.put_object(
            Bucket=owner.bucket, Key=owner.object_key, Body=raw
        )


@pytest.mark.parametrize("change", ["key", "membership", "batch", "gate"])
async def test_post_read_binding_or_authority_change_closes_stage(
    httpx_client, db_session, annotator, stored_world, monkeypatch, change
):
    task, asset, member, _, _ = stored_world
    batch = TaskBatch(
        project_id=task.project_id,
        display_id="B-" + uuid.uuid4().hex[:12],
        name="Original read",
        status="active",
        annotator_id=annotator[0].id,
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    await db_session.flush()
    original_capture, stages = reader.capture, []

    async def capture(*args, **kwargs):
        stage = await original_capture(*args, **kwargs)
        stages.append(stage)
        # SQL updates deliberately leave already loaded ORM objects stale.
        if change == "key":
            await db_session.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(file_path="changed/" + uuid.uuid4().hex)
                .execution_options(synchronize_session=False)
            )
        elif change == "membership":
            await db_session.execute(
                delete(ProjectMember)
                .where(
                    ProjectMember.project_id == member.project_id,
                    ProjectMember.user_id == member.user_id,
                )
                .execution_options(synchronize_session=False)
            )
        elif change == "batch":
            await db_session.execute(
                update(TaskBatch)
                .where(TaskBatch.id == batch.id)
                .values(status="draft")
                .execution_options(synchronize_session=False)
            )
        else:
            await db_session.execute(
                update(Gate)
                .where(Gate.location_id == LEGACY_LOCATION_ID)
                .values(revision=Gate.revision + 1)
                .execution_options(synchronize_session=False)
            )
        await db_session.flush()
        return stage

    monkeypatch.setattr(reader, "capture", capture)
    response = await content(httpx_client, annotator, task, asset.checksum_value)
    assert response.status_code in (
        {403, 404} if change in {"membership", "batch"} else {409}
    ), response.text
    assert len(stages) == 1 and stages[0].file.closed


async def test_identity_is_metadata_only_and_range_stays_unsupported(
    httpx_client, project_admin, stored_world, monkeypatch
):
    task, asset, _, _, _ = stored_world
    blocked = AsyncMock(
        side_effect=AssertionError("Metadata and rejected Range do not read objects")
    )
    monkeypatch.setattr(reader, "capture", blocked)
    response = await httpx_client.get(
        content_url(task) + "-identity", headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    assert response.json()["original_availability"] == "unchecked"
    response = await httpx_client.get(
        content_url(task),
        params={"sha256": asset.checksum_value},
        headers={**headers(project_admin), "Range": "bytes=0-10"},
    )
    assert response.status_code == 416, response.text
    blocked.assert_not_awaited()


async def test_original_request_api_key_revocation_after_io_closes_stage(
    httpx_client, db_session, project_admin, stored_world, monkeypatch
):
    task, asset, _, _, _ = stored_world
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "original-reader", "scopes": ["tasks:read"]},
    )
    assert created.status_code == 201, created.text
    key = created.json()
    stages, original_capture = [], reader.capture

    async def capture(*args, **kwargs):
        stage = await original_capture(*args, **kwargs)
        stages.append(stage)
        await db_session.execute(
            update(ApiKey)
            .where(ApiKey.id == uuid.UUID(key["id"]))
            .values(revoked_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        )
        await db_session.flush()
        return stage

    monkeypatch.setattr(reader, "capture", capture)
    response = await content(
        httpx_client, (project_admin[0], key["plaintext"]), task, asset.checksum_value
    )
    assert response.status_code == 403, response.text
    assert len(stages) == 1 and stages[0].file.closed
