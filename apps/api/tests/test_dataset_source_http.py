"""Public source deletion and staged uploads exercise the real SQL boundary."""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.db.models.asset_storage_deletion import AssetStorageDeletion
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_source_deletion_operation import (
    DatasetSourceDeletionOperation,
)
from app.db.models.dataset_upload_intent import DatasetUploadIntent
from app.db.models.image_pyramid import ImagePyramidAsset
from app.db.models.task import Task
from app.services.storage import storage_service
from tests.factory import create_project

pytestmark = pytest.mark.asyncio


def _headers(token, key=None):
    result = {"Authorization": f"Bearer {token}"}
    if key is not None:
        result["Idempotency-Key"] = str(key)
    return result


async def _dataset(db, actor):
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-SRC-{uuid.uuid4().hex[:12]}",
        name=f"source-{uuid.uuid4().hex}",
        created_by=actor,
        data_type="image",
    )
    db.add(dataset)
    await db.flush()
    return dataset


async def _item(db, dataset):
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="source.jpg",
        file_path=f"sources/{uuid.uuid4()}.jpg",
        file_type="image",
    )
    db.add(item)
    dataset.file_count += 1
    await db.flush()
    return item


def _no_deletes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The HTTP transaction must never delete object bytes")

    monkeypatch.setattr(storage_service, "delete_prefix", forbidden)
    monkeypatch.setattr(storage_service, "delete_object", forbidden)


@pytest.mark.parametrize("item_only", [True, False])
async def test_delete_commits_empty_response_receipt_and_exact_outbox(
    httpx_client, db_session, super_admin, monkeypatch, item_only
):
    user, token = super_admin
    dataset = await _dataset(db_session, user.id)
    item = await _item(db_session, dataset)
    dataset_id, item_id = dataset.id, item.id
    asset = ImagePyramidAsset(dataset_item_id=item_id, profile_version="source-http")
    db_session.add(asset)
    await db_session.flush()
    asset_id = asset.id
    _no_deletes(monkeypatch)
    operation = uuid.uuid4()
    path = f"/api/v1/datasets/{dataset_id}" + (f"/items/{item_id}" if item_only else "")
    for _ in range(2):
        response = await httpx_client.delete(path, headers=_headers(token, operation))
        assert response.status_code == 204, response.text
        assert response.content == b""
        assert response.headers["Idempotency-Key"] == str(operation)
    assert await db_session.get(DatasetItem, item_id) is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetSourceDeletionOperation)
            .where(DatasetSourceDeletionOperation.operation_id == operation)
        )
        == 1
    )
    pending = (
        await db_session.execute(
            select(
                AssetStorageDeletion.bucket,
                AssetStorageDeletion.prefix,
                AssetStorageDeletion.completed_at,
            ).where(
                AssetStorageDeletion.operation_id == operation,
                AssetStorageDeletion.asset_id == asset_id,
            )
        )
    ).one()
    assert pending == (
        storage_service.media_cache_bucket,
        f"image-pyramids/{asset_id}/",
        None,
    )
    remaining = await db_session.get(Dataset, dataset_id, populate_existing=True)
    assert (remaining.file_count if remaining else None) == (0 if item_only else None)


async def test_item_parent_mismatch_and_bad_key_cannot_delete(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    left, right = (
        await _dataset(db_session, user.id),
        await _dataset(db_session, user.id),
    )
    item = await _item(db_session, left)
    item_id = item.id
    _no_deletes(monkeypatch)
    response = await httpx_client.delete(
        f"/api/v1/datasets/{right.id}/items/{item_id}", headers=_headers(token)
    )
    assert response.status_code == 404, response.text
    response = await httpx_client.delete(
        f"/api/v1/datasets/{left.id}/items/{item_id}",
        headers=_headers(token, "invalid"),
    )
    assert response.status_code == 422, response.text
    assert await db_session.get(DatasetItem, item_id) is item


async def test_task_reference_returns_structured_conflict_without_partial_cleanup(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    dataset = await _dataset(db_session, user.id)
    item = await _item(db_session, dataset)
    dataset_id, item_id = dataset.id, item.id
    project = await create_project(db_session, owner_id=user.id)
    task = Task(
        project_id=project.id,
        dataset_item_id=item_id,
        display_id=f"T-SRC-{uuid.uuid4().hex[:12]}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    _no_deletes(monkeypatch)
    response = await httpx_client.delete(
        f"/api/v1/datasets/{dataset_id}/items/{item_id}", headers=_headers(token)
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_in_use"
    assert await db_session.get(DatasetItem, item_id) is not None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetSourceDeletionOperation)
            .where(DatasetSourceDeletionOperation.dataset_id == dataset_id)
        )
        == 0
    )


async def test_failed_http_commit_rolls_back_deletion_and_outbox(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    dataset = await _dataset(db_session, user.id)
    item = await _item(db_session, dataset)
    dataset_id, item_id = dataset.id, item.id
    operation = uuid.uuid4()
    await db_session.commit()
    _no_deletes(monkeypatch)
    monkeypatch.setattr(
        db_session, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        await httpx_client.delete(
            f"/api/v1/datasets/{dataset_id}/items/{item_id}",
            headers=_headers(token, operation),
        )
    await db_session.rollback()
    assert await db_session.get(DatasetItem, item_id) is not None
    assert await db_session.get(DatasetSourceDeletionOperation, operation) is None


async def _reserve(client, token, dataset_id, name="sub/source.jpg"):
    response = await client.post(
        f"/api/v1/datasets/{dataset_id}/items/upload-init",
        headers=_headers(token),
        json={"file_name": name, "content_type": "image/jpeg"},
    )
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["item_id"])


async def test_upload_staging_publication_and_duplicate_preserve_source(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    dataset = await _dataset(db_session, user.id)
    dataset_id = dataset.id
    project = await create_project(db_session, owner_id=user.id)
    db_session.add(ProjectDataset(dataset_id=dataset_id, project_id=project.id))
    await db_session.flush()
    signed, copied = [], []

    def sign(key, content_type, **kwargs):
        signed.append((key, kwargs["bucket"]))
        return "http://upload.test/opaque"

    def copy(staging, final, **kwargs):
        copied.append((staging, final, kwargs["etag"]))

    monkeypatch.setattr(storage_service, "generate_upload_url", sign)
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda *args, **kwargs: {"ETag": '"' + "a" * 32 + '"', "ContentLength": 32},
    )
    monkeypatch.setattr(
        storage_service, "read_image_dimensions", lambda *args, **kwargs: (20, 30)
    )
    monkeypatch.setattr(storage_service, "publish_dataset_upload", copy)
    _no_deletes(monkeypatch)
    first = await _reserve(httpx_client, token, dataset_id)
    second = await _reserve(httpx_client, token, dataset_id)
    assert first != second
    assert signed == [
        (f"dataset-uploads/{first}/source", storage_service.import_bucket),
        (f"dataset-uploads/{second}/source", storage_service.import_bucket),
    ]
    assert await db_session.get(DatasetItem, first) is None
    assert (
        await db_session.scalar(
            select(Dataset.file_count).where(Dataset.id == dataset_id)
        )
        == 0
    )
    path = f"/api/v1/datasets/{dataset_id}/items/upload-complete/{first}"
    response = await httpx_client.post(path, headers=_headers(token))
    assert response.status_code == 200, response.text
    assert response.json()["linked_tasks"] == 1
    item = await db_session.get(DatasetItem, first, populate_existing=True)
    assert (item.width, item.height, item.file_size) == (20, 30, 32)
    assert copied == [
        (f"dataset-uploads/{first}/source", item.file_path, '"' + "a" * 32 + '"')
    ]
    assert item.file_path.startswith(f"dataset-sources/{dataset_id}/{first}/")
    replay = await httpx_client.post(path, headers=_headers(token))
    assert replay.json() == response.json()
    assert len(copied) == 1
    duplicate = await httpx_client.post(
        f"/api/v1/datasets/{dataset_id}/items/upload-complete/{second}",
        headers=_headers(token),
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["duplicate_of"] == str(first)
    assert len(copied) == 1
    assert await db_session.get(DatasetItem, second) is None
    assert (
        await db_session.scalar(
            select(Dataset.file_count).where(Dataset.id == dataset_id)
        )
        == 1
    )


async def test_missing_and_legacy_completion_never_delete_source(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    dataset = await _dataset(db_session, user.id)
    dataset_id = dataset.id
    legacy = await _item(db_session, dataset)
    legacy_id = legacy.id
    monkeypatch.setattr(storage_service, "verify_upload", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        storage_service,
        "generate_upload_url",
        lambda *args, **kwargs: "http://upload.test/opaque",
    )
    _no_deletes(monkeypatch)
    current = await _reserve(httpx_client, token, dataset_id)
    for item_id in (current, legacy_id):
        response = await httpx_client.post(
            f"/api/v1/datasets/{dataset_id}/items/upload-complete/{item_id}",
            headers=_headers(token),
        )
        assert response.status_code == 400, response.text
    assert await db_session.get(DatasetItem, current) is None
    assert await db_session.get(DatasetItem, legacy_id) is not None
    intent = await db_session.get(DatasetUploadIntent, current, populate_existing=True)
    assert intent.state == "discarded" and intent.result == {"reason": "missing"}


async def test_upload_parent_mismatch_is_missing_and_other_actor_cannot_adopt(
    httpx_client, db_session, super_admin, annotator, monkeypatch
):
    user, token = super_admin
    _, other_token = annotator
    left, right = (
        await _dataset(db_session, user.id),
        await _dataset(db_session, user.id),
    )
    left_id, right_id = left.id, right.id
    monkeypatch.setattr(
        storage_service,
        "generate_upload_url",
        lambda *args, **kwargs: "http://upload.test/opaque",
    )
    item_id = await _reserve(httpx_client, token, left_id)

    def forbidden(*args, **kwargs):
        pytest.fail("An invalid upload scope reached object storage")

    monkeypatch.setattr(storage_service, "verify_upload", forbidden)
    mismatch = await httpx_client.post(
        f"/api/v1/datasets/{right_id}/items/upload-complete/{item_id}",
        headers=_headers(token),
    )
    assert mismatch.status_code == 404, mismatch.text
    denied = await httpx_client.post(
        f"/api/v1/datasets/{left_id}/items/upload-complete/{item_id}",
        headers=_headers(other_token),
    )
    assert denied.status_code == 403, denied.text
    intent = await db_session.get(DatasetUploadIntent, item_id, populate_existing=True)
    assert (
        intent.actor_id == user.id
        and intent.state == "pending"
        and intent.publication_id is None
    )


@pytest.mark.parametrize(
    "name",
    [
        "/absolute.jpg",
        "../escape.jpg",
        "sub/../escape.jpg",
        "sub\\bad.jpg",
        "sub//bad.jpg",
        "bad\n.jpg",
        "中" * 300 + ".jpg",
    ],
)
async def test_invalid_upload_name_is_rejected_before_storage(
    httpx_client, db_session, super_admin, monkeypatch, name
):
    user, token = super_admin
    dataset = await _dataset(db_session, user.id)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid upload input reached storage")

    monkeypatch.setattr(storage_service, "generate_upload_url", forbidden)
    response = await httpx_client.post(
        f"/api/v1/datasets/{dataset.id}/items/upload-init",
        headers=_headers(token),
        json={"file_name": name},
    )
    assert response.status_code == 422, response.text
