"""Real-provider contracts for authenticated full binary downloads."""

import hashlib
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAssetLocation
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import managed_original_reader as reader
from app.services import managed_original_creation as creations
from app.services.dataset_assets import register_item_asset
from app.services.storage import storage_service
from app.db.models.task import Task
from app.db.models.api_key import ApiKey
from app.db.models.storage_maintenance import StorageMaintenanceGate as Gate
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.test_verified_task_content import headers, stored_world as stored_world


def task_path(task):
    return f"/api/v1/files/tasks/{task.id}/original"


def item_path(item):
    return f"/api/v1/datasets/{item.dataset_id}/items/{item.id}/original"


@pytest.mark.parametrize(
    "raw,content_type,file_name",
    [
        (b"", "application/octet-stream", "empty.bin"),
        (b"\x00\xffbinary\x00", "application/octet-stream", "binary.bin"),
        (b"GIF89a-preserve-original-bytes", "image/gif", "animation.gif"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml", "vector.svg"),
        (b"\x00\x00\x00\x18ftypmp42-video-bytes", "video/mp4", "clip.mp4"),
    ],
)
async def test_standalone_generic_original_download_preserves_bytes_without_image_decoding(
    httpx_client,
    db_session,
    stored_world,
    project_admin,
    monkeypatch,
    raw,
    content_type,
    file_name,
):
    monkeypatch.setattr(
        "app.workers.media.generate_task_thumbnail.delay", lambda *_: None
    )
    initialized = await httpx_client.post(
        "/api/v1/files/upload-init",
        headers=headers(project_admin),
        json={
            "project_id": str(stored_world[0].project_id),
            "file_name": file_name,
            "content_type": content_type,
        },
    )
    assert initialized.status_code == 200, initialized.text
    task_id = uuid.UUID(initialized.json()["task_id"])
    task = await db_session.get(Task, task_id)
    allocation = await creations.for_task(
        db_session, task_id=task_id, object_key=task.file_path
    )
    storage_service.client.put_object(
        Bucket=allocation["staging_bucket"],
        Key=allocation["staging_key"],
        Body=raw,
        ContentType=content_type,
    )
    completed = await httpx_client.post(
        f"/api/v1/files/upload-complete/{task.id}", headers=headers(project_admin)
    )
    assert completed.status_code == 200, completed.text
    identity = await describe(httpx_client, task_path(task), project_admin)
    assert identity.status_code == 200, identity.text
    assert identity.json()["owner_kind"] == "task_original"
    result = await httpx_client.get(
        identity.json()["download_path"], headers=headers(project_admin)
    )
    assert result.status_code == 200, result.text
    assert result.content == raw
    assert result.headers["x-content-sha256"] == hashlib.sha256(raw).hexdigest()


async def describe(client, path, actor):
    return await client.get(path + "-identity", headers=headers(actor))


@pytest.fixture
async def binary_world(db_session, stored_world):
    task, _, _, _, _ = stored_world
    previous = await db_session.get(DatasetItem, task.dataset_item_id)
    raw = b"# .PCD v0.7\nFIELDS x y z\nDATA ascii\n0 1 2\n"
    item = DatasetItem(
        dataset_id=previous.dataset_id,
        file_name="frame.pcd",
        file_path="binary-acceptance/" + uuid.uuid4().hex,
        file_type="point_cloud",
        file_size=len(raw),
    )
    db_session.add(item)
    await db_session.flush()
    asset = await register_item_asset(
        db_session, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )
    task.file_type = item.file_type
    task.file_name = item.file_name
    task.file_path = item.file_path
    task.dataset_item_id = item.id
    await db_session.flush()
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket, Key=item.file_path, Body=raw
    )
    try:
        yield task, item, asset, raw
    finally:
        storage_service.client.delete_object(
            Bucket=storage_service.datasets_bucket, Key=item.file_path
        )


async def test_primary_and_item_download_use_saved_bucket_and_exact_identity(
    httpx_client, db_session, binary_world, project_admin, monkeypatch
):
    task, item, asset, raw = binary_world
    task.dataset_item_id = None
    db_session.add(
        TaskDatasetItemLink(
            task_id=task.id, dataset_item_id=item.id, role="primary_lidar"
        )
    )
    await db_session.flush()
    trap = storage_service.import_bucket
    storage_service.client.put_object(Bucket=trap, Key=item.file_path, Body=b"trap")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage_service, "bucket", trap)
            patch.setattr(storage_service, "datasets_bucket", trap)
            for path in (task_path(task), item_path(item)):
                identity = await describe(httpx_client, path, project_admin)
                assert identity.status_code == 200, identity.text
                metadata = identity.json()
                assert metadata["content_id"] == str(asset.id)
                assert metadata["media_type"] == "point_cloud"
                assert metadata["original_availability"] == "unchecked"
                assert metadata["verification_state"] == "verified"
                assert identity.headers["cache-control"] == "no-store"
                assert not {"bucket", "object_key", "endpoint_url"}.intersection(
                    metadata
                )
                response = await httpx_client.get(
                    metadata["download_path"], headers=headers(project_admin)
                )
                assert response.status_code == 200, response.text
                assert response.content == raw
                assert response.headers["content-type"] == "application/octet-stream"
                assert response.headers["content-disposition"].startswith("attachment;")
                assert response.headers["content-length"] == str(len(raw))
                assert response.headers["x-content-sha256"] == asset.checksum_value
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
    finally:
        storage_service.client.delete_object(Bucket=trap, Key=item.file_path)


async def test_identity_is_zero_physical_io_and_range_head_and_stale_identity_do_not_read(
    httpx_client, binary_world, project_admin, monkeypatch
):
    task, _, asset, _ = binary_world
    forbidden = AsyncMock(side_effect=AssertionError("physical capture forbidden"))
    monkeypatch.setattr(reader, "capture", forbidden)
    metadata = await describe(httpx_client, task_path(task), project_admin)
    assert metadata.status_code == 200, metadata.text
    path = metadata.json()["download_path"]
    response = await httpx_client.get(
        path, headers={**headers(project_admin), "Range": "bytes=0-1"}
    )
    assert response.status_code == 416
    assert (
        await httpx_client.head(path, headers=headers(project_admin))
    ).status_code == 405
    for content_id, sha in ((uuid.uuid4(), asset.checksum_value), (asset.id, "f" * 64)):
        result = await httpx_client.get(
            task_path(task),
            headers=headers(project_admin),
            params={"content_id": str(content_id), "sha256": sha},
        )
        assert result.status_code == 409, result.text
    forbidden.assert_not_awaited()


async def test_task_assignee_can_read_camera_but_not_dataset_or_foreign_link(
    httpx_client, db_session, stored_world, annotator
):
    task, asset, _, raw, _ = stored_world
    camera = TaskDatasetItemLink(
        task_id=task.id, dataset_item_id=task.dataset_item_id, role="camera_front"
    )
    db_session.add(camera)
    await db_session.flush()
    path = f"/api/v1/files/tasks/{task.id}/links/{camera.id}/original"
    metadata = await describe(httpx_client, path, annotator)
    assert metadata.status_code == 200, metadata.text
    response = await httpx_client.get(
        metadata.json()["download_path"], headers=headers(annotator)
    )
    assert response.status_code == 200 and response.content == raw
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    denied = await describe(httpx_client, item_path(item), annotator)
    assert denied.status_code in {403, 404}, denied.text
    foreign = path.replace(str(task.id), str(uuid.uuid4()))
    assert (await describe(httpx_client, foreign, annotator)).status_code == 404
    wrong_dataset = item_path(item).replace(str(item.dataset_id), str(uuid.uuid4()))
    assert (await describe(httpx_client, wrong_dataset, annotator)).status_code in {
        403,
        404,
    }
    assert metadata.json()["content_id"] == str(asset.id)


async def test_same_size_corruption_and_post_read_link_change_never_deliver(
    httpx_client, db_session, stored_world, project_admin, monkeypatch
):
    task, _, _, raw, key = stored_world
    metadata = await describe(httpx_client, task_path(task), project_admin)
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket, Key=key, Body=b"x" * len(raw)
    )
    rejected = await httpx_client.get(
        metadata.json()["download_path"], headers=headers(project_admin)
    )
    assert rejected.status_code == 409, rejected.text
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket, Key=key, Body=raw
    )
    camera = TaskDatasetItemLink(
        task_id=task.id, dataset_item_id=task.dataset_item_id, role="camera_front"
    )
    db_session.add(camera)
    await db_session.flush()
    path = f"/api/v1/files/tasks/{task.id}/links/{camera.id}/original"
    metadata = await describe(httpx_client, path, project_admin)
    stages, capture = [], reader.capture

    async def change_link(*args, **kwargs):
        stage = await capture(*args, **kwargs)
        stages.append(stage)
        await db_session.execute(
            update(TaskDatasetItemLink)
            .where(TaskDatasetItemLink.id == camera.id)
            .values(sensor_name="changed")
            .execution_options(synchronize_session=False)
        )
        return stage

    monkeypatch.setattr(reader, "capture", change_link)
    rejected = await httpx_client.get(
        metadata.json()["download_path"], headers=headers(project_admin)
    )
    assert rejected.status_code == 409, rejected.text
    assert len(stages) == 1 and stages[0].file.closed


async def test_unverified_history_does_not_guess_current_bucket(
    httpx_client, db_session, stored_world, project_admin, monkeypatch
):
    task, asset, _, _, _ = stored_world
    replica = await db_session.scalar(
        select(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    replica.reference_state = "missing"
    await db_session.flush()
    capture = AsyncMock(side_effect=AssertionError("no fallback read"))
    monkeypatch.setattr(reader, "capture", capture)
    result = await describe(httpx_client, task_path(task), project_admin)
    assert result.status_code == 409, result.text
    capture.assert_not_awaited()


@pytest.mark.parametrize("change", ["jwt", "api_key", "gate"])
async def test_download_rechecks_original_http_credentials_and_gate_after_real_capture(
    httpx_client, db_session, stored_world, project_admin, monkeypatch, change
):
    task, _, _, _, _ = stored_world
    actor = project_admin
    if change == "api_key":
        created = await httpx_client.post(
            "/api/v1/me/api-keys",
            headers=headers(project_admin),
            json={"name": "binary-reader", "scopes": ["tasks:read"]},
        )
        assert created.status_code == 201, created.text
        key = created.json()
        actor = (project_admin[0], key["plaintext"])
    identity = await describe(httpx_client, task_path(task), actor)
    assert identity.status_code == 200, identity.text
    stages, capture = [], reader.capture

    async def revoke(*args, **kwargs):
        stage = await capture(*args, **kwargs)
        stages.append(stage)
        if change == "jwt":
            monkeypatch.setattr(
                "app.core.token_blacklist.get_user_generation",
                AsyncMock(return_value=1),
            )
        elif change == "api_key":
            await db_session.execute(
                update(ApiKey)
                .where(ApiKey.id == uuid.UUID(key["id"]))
                .values(revoked_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            )
        else:
            await db_session.execute(
                update(Gate)
                .where(Gate.location_id == LEGACY_LOCATION_ID)
                .values(revision=Gate.revision + 1)
                .execution_options(synchronize_session=False)
            )
        return stage

    monkeypatch.setattr(reader, "capture", revoke)
    rejected = await httpx_client.get(
        identity.json()["download_path"], headers=headers(actor)
    )
    assert rejected.status_code == (409 if change == "gate" else 403), rejected.text
    assert len(stages) == 1 and stages[0].file.closed


async def test_matching_etag_still_verifies_current_original_and_rejects_corruption(
    httpx_client, stored_world, project_admin, monkeypatch
):
    task, asset, _, raw, key = stored_world
    identity = await describe(httpx_client, task_path(task), project_admin)
    conditional = {
        **headers(project_admin),
        "If-None-Match": f'"sha256:{asset.checksum_value}"',
    }
    capture = AsyncMock(wraps=reader.capture)
    monkeypatch.setattr(reader, "capture", capture)
    first = await httpx_client.get(
        identity.json()["download_path"], headers=conditional
    )
    assert first.status_code == 200 and first.content == raw
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket, Key=key, Body=b"x" * len(raw)
    )
    corrupt = await httpx_client.get(
        identity.json()["download_path"], headers=conditional
    )
    assert corrupt.status_code == 409, corrupt.text
    assert capture.await_count == 2


async def test_response_constructor_failure_closes_actual_captured_stage(
    httpx_client, stored_world, project_admin, monkeypatch
):
    task, _, _, _, _ = stored_world
    identity = await describe(httpx_client, task_path(task), project_admin)
    stages, capture = [], reader.capture

    async def tracked_capture(*args, **kwargs):
        stage = await capture(*args, **kwargs)
        stages.append(stage)
        return stage

    def invalid_response(stage):
        raise ValueError("response construction failed")

    monkeypatch.setattr(reader, "capture", tracked_capture)
    monkeypatch.setattr("app.api.v1.originals.BinaryResponse", invalid_response)
    with pytest.raises(ValueError, match="response construction failed"):
        await httpx_client.get(
            identity.json()["download_path"], headers=headers(project_admin)
        )
    assert len(stages) == 1 and stages[0].file.closed
